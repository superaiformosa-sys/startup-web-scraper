import hashlib
import json
import re
import time
import logging
from datetime import datetime, timezone, timedelta
import requests
import gspread
from google.oauth2.service_account import Credentials
from config import (
    GEMINI_API_KEY, GEMINI_ENDPOINT, MAX_GEMINI_PER_RUN,
    SHEETS_ID, GOOGLE_CREDENTIALS_JSON, FIT_KEYWORDS, FX,
)
from firebase_client import firestore_write

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
TAIWAN_DOMAINS = ["meet.bnext", "bnext.com", "inside.com.tw", "technews.tw", "news.google.com", "ctee.com.tw", "udn.com"]

OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_MODEL = "qwen2.5:7b"

# ── 規則集 ──

# 標題出現任一關鍵字 → 直接跳過（不送 Qwen）
RULE_SKIP_TITLE = [
    # 股市
    "股市", "大盤", "成交額", "成交量", "漲跌", "休盤", "收盤", "開盤", "指數",
    "沪深", "恒指", "道指", "納指", "标普", "北向資金", "主力資金", "南向資金",
    "半日", "午間", "日线", # 盤中報
    # 總經/政策
    "央行", "降準", "降息", "升息", "關稅", "制裁", "外匯", "匯率", "期貨",
    "cpi", "ppi", "gdp", "通膨", "通胀", "貿易戰", "外貿", "進出口",
    # 自然/雜項
    "地震", "颱風", "天氣", "氣候", "廣告", "招募", "徵才", "白皮書",
    # 大宗商品
    "原油", "黃金", "鋰礦", "稀土", "礦產", "煤炭",
    # 人物生活（非商業）
    "裸辞", "降薪跳槽", "副業", "奥德赛", "职场极端",
]

# 標題有這些 → 幾乎確定是新創（配合有金額出現 → 直接 pre-accept）
STRONG_FUNDING_KW = ["融资", "融資", "募资", "募資", "完成", "获得投资", "獲得投資",
                     "pre-a", "天使轮", "天使輪", "种子轮", "種子輪",
                     "series a", "series b", "series c", "a轮", "b轮", "c轮", "a輪", "b輪", "c輪"]

# 標題有這些 → 直接 pre-accept（不需要有金額）
STRONG_STARTUP_KW = ["新创", "新創", "startup", "创业", "創業",
                     "加速器", "孵化器", "独角兽", "獨角獸",
                     "ipo", "上市", "掛牌", "招股", "挂牌",
                     "创始人", "創辦人", "founder",
                     "早期项目", "早期專案"]

# ── Google Sheets ──

def get_sheets_client():
    creds = Credentials.from_service_account_info(json.loads(GOOGLE_CREDENTIALS_JSON), scopes=SCOPES)
    return gspread.authorize(creds)

def get_sheet(tab_name: str) -> gspread.Worksheet:
    gc = get_sheets_client()
    return gc.open_by_key(SHEETS_ID).worksheet(tab_name)


_SCORED_TAB = "scored_results"
_SCORED_HEADER = [
    "extractedAt", "companyName", "companyNameEn", "region", "stage",
    "fundingAmountRaw", "fundingAmountUSD", "industry",
    "hotaiFitScore", "fitScore", "mlScore", "ruleScore", "qwenScore", "hotaiQwenScore",
    "fitTags", "sourceUrl", "description", "summary",
]

def _get_or_create_scored_sheet(gc) -> gspread.Worksheet:
    ss = gc.open_by_key(SHEETS_ID)
    try:
        return ss.worksheet(_SCORED_TAB)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=_SCORED_TAB, rows=5000, cols=len(_SCORED_HEADER))
        ws.append_row(_SCORED_HEADER)
        ws.freeze(rows=1)
        logger.info("Created scored_results sheet tab")
        return ws

def mirror_to_sheets(result: dict) -> None:
    """Append one scored startup row to the scored_results Sheets tab for human review."""
    try:
        gc = get_sheets_client()
        ws = _get_or_create_scored_sheet(gc)
        row = [
            result.get("extractedAt", ""),
            result.get("companyName", ""),
            result.get("companyNameEn", ""),
            result.get("region", ""),
            result.get("stage", ""),
            result.get("fundingAmountRaw", ""),
            result.get("fundingAmountUSD", ""),
            ", ".join(result.get("industry") or []),
            result.get("hotaiFitScore", ""),
            result.get("fitScore", ""),
            result.get("mlScore", ""),
            result.get("ruleScore", ""),
            result.get("qwenScore", ""),
            result.get("hotaiQwenScore", ""),
            ", ".join(result.get("fitTags") or []),
            result.get("sourceUrl", ""),
            result.get("description", "")[:200],
            result.get("summary", "")[:200],
        ]
        ws.append_row(row, value_input_option="RAW")
    except Exception as e:
        logger.warning("mirror_to_sheets failed (non-fatal): %s", e)


# ── Content 解析：從 Sheets content 欄還原 title + summary ──

def parse_content_field(content: str) -> tuple[str, str]:
    """把 '標題：X\n摘要：Y\n內文：Z' 拆回 (title, summary)"""
    title, summary = "", ""
    if "標題：" in content:
        after = content.split("標題：", 1)[1]
        title = after.split("\n")[0].strip()
    if "摘要：" in content:
        after = content.split("摘要：", 1)[1]
        summary = after.split("內文：")[0].strip()[:300]
    return title, summary


# ── Stage 0：純 Python 規則分類（0ms） ──

def extract_funding_from_title(title: str) -> tuple[str, str]:
    """從標題抽出金額和輪次，回傳 (amount_raw, stage)"""
    amount = ""
    stage = ""

    amount_patterns = [
        r"([\d.]+\s*億[美台人]?幣?)",
        r"([\d.]+\s*千萬[美台人]?幣?)",
        r"([\d.]+\s*萬[美台人]?幣?)",
        r"(\$[\d.]+[MmBbKk])",
        r"(USD?\s*[\d.]+[MmBb])",
        r"([\d.]+\s*[Mm]illion)",
        r"([\d.]+亿[美人]?元?)",
        r"([\d.]+万[美人]?元?)",
    ]
    for p in amount_patterns:
        m = re.search(p, title, re.IGNORECASE)
        if m:
            amount = m.group(1)
            break

    stage_map = {
        "種子輪": ["種子輪", "seed round"],
        "天使輪": ["天使輪", "angel"],
        "Pre-A":  ["pre-a", "pre a", "prea"],
        "A輪":    ["a輪", "series a", "a round"],
        "B輪":    ["b輪", "series b", "b round"],
        "C輪":    ["c輪", "series c"],
        "D輪":    ["d輪", "series d"],
        "戰略投資": ["戰略投資", "strategic investment"],
        "IPO":    ["ipo", "上市", "掛牌"],
    }
    t = title.lower()
    for s, kws in stage_map.items():
        if any(kw in t for kw in kws):
            stage = s
            break

    return amount, stage


def rule_classify(title: str) -> str:
    """
    回傳: 'skip' | 'startup' | 'ambiguous'
    skip    → 不是新創，直接標記 processed，不送 Qwen
    startup → 確定是新創，跳過分類，直接進 Stage 2 提取
    ambiguous → 不確定，進 Stage 1 批次分類
    """
    t = title.lower()

    if any(kw in t for kw in RULE_SKIP_TITLE):
        return "skip"

    amount, _ = extract_funding_from_title(title)
    if amount and any(kw.lower() in t for kw in STRONG_FUNDING_KW):
        return "startup"

    if any(kw.lower() in t for kw in STRONG_STARTUP_KW):
        return "startup"

    return "ambiguous"


# ── Stage 1：批次標題分類（10篇/次 Qwen call） ──

def _call_ollama_raw(prompt: str, num_predict: int = 80) -> str:
    """直接回傳 Qwen 的文字輸出，不 parse JSON"""
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": num_predict},
    }
    resp = requests.post(f"{OLLAMA_BASE_URL}/api/generate", json=payload, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f"Ollama {resp.status_code}: {resp.text[:100]}")
    return resp.json().get("response", "")


def batch_classify_titles(items: list[dict]) -> list[int]:
    """
    送最多 10 篇標題給 Qwen，要它輸出哪些 index 是新創。
    items: [{"title": str, ...}]
    回傳: 是新創的 index 列表，e.g. [0, 2, 5]
    """
    lines = "\n".join(f"{i}. {item['title'][:80]}" for i, item in enumerate(items))
    prompt = (
        "Task: identify which headlines are about startups, innovative SMEs, or tech companies.\n"
        "INCLUDE: company funding, product launches, founder profiles, startup acquisitions, accelerator news.\n"
        "EXCLUDE: stock market data, government policy, macroeconomics, large public corps with no startup angle.\n"
        "Output ONLY a JSON array of 0-based indices to INCLUDE, e.g. [0,2,5]. If none, output [].\n\n"
        f"Headlines:\n{lines}\n\nOutput:"
    )
    try:
        raw = _call_ollama_raw(prompt, num_predict=60)
        m = re.search(r"\[[\d,\s]*\]", raw)
        if m:
            return json.loads(m.group(0))
    except requests.Timeout:
        # On timeout, treat all items as accepted (same conservative fallback as other exceptions).
        # Returning [] would silently mark all items as processed with nothing written to Firebase.
        logger.error("Stage1 Ollama timeout — fallback: treating all %d as startup", len(items))
        return list(range(len(items)))
    except Exception as e:
        logger.warning("batch_classify error: %s", e)
    return list(range(len(items)))  # fallback: 全部進 Stage 2


# ── Stage 2：單篇欄位提取（只送 title + 短摘要） ──

# Qwen 常見的「我不知道」佔位符，都視為無效
_PLACEHOLDER_NAMES = {
    "空", "empty", "n/a", "na", "none", "unknown", "未知",
    "中文名", "英文名", "公司名", "公司名稱", "english", "english or empty",
    "companyname", "company name", "名称", "名稱",
    "<actual company name>", "actual company name",
}

def _is_valid_company_name(name: str) -> bool:
    if not name or len(name.strip()) < 2:
        return False
    return name.strip().lower() not in _PLACEHOLDER_NAMES


_HOTAI_CONTEXT = (
    "Hotai Group (和泰集團) is Taiwan's largest automotive conglomerate with 13 business verticals: "
    "① Auto retail — Toyota/Lexus/Hino exclusive distributor (38.6% market share), "
    "  dealerships: 國都/北都/桃苗/中部/南都/高都/蘭揚; "
    "② Commercial vehicles — 長源汽車 Taiwan HINO trucks & buses; "
    "③ Japan commercial vehicles — 南關東日野/北海道日野/東北海道/宮城日野/福島日野; "
    "④ EV charging & energy — EVRun (起而行綠能/旭電馳/充壹/和潤電能), U-POWER investment, MIRAI hydrogen; "
    "⑤ Finance — 和潤企業 (auto loans & leasing, largest non-bank financier in Taiwan), 和勁企業; "
    "⑥ Car rental — 和運租車 (6,000+ vehicle fleet, iRent car-sharing 10K vehicles); "
    "⑦ China auto retail — 和通汽車投資 + mainland dealerships; "
    "⑧ Auto products — 車美仕Carmax / 興聯科技 / 凱美士 (automotive electronics & accessories); "
    "⑨ Vehicle body — 和泰車體製造/銷售, 和泰巴士銷售 (bus body manufacturing); "
    "⑩ P&C Insurance — 和泰產險, 和安保險; "
    "⑪ Industrial machinery & warehouse robotics — 和泰豐田物料運搬TMHT (Toyota Material Handling Taiwan); "
    "⑫ MaaS — yoxi ride-hailing, iRent, 和泰聯網, 去趣 travel app (3.5M downloads), "
    "   和泰Pay + 和泰Points (3B+ points, 4M members); "
    "⑬ HVAC — 和泰興業 (Daikin exclusive distributor in Taiwan). "
    "Strategic priorities 2026: AI First (和泰AI中台), MaaS ecosystem expansion, "
    "EV/hydrogen transition, InsurTech digitization, warehouse automation, smart tourism."
)


def build_extract_prompt(title: str, summary: str, url: str, prefilled: dict) -> str:
    hints = []
    if prefilled.get("stage"):
        hints.append(f"Funding stage: {prefilled['stage']}")
    if prefilled.get("fundingAmountRaw"):
        hints.append(f"Funding amount: {prefilled['fundingAmountRaw']}")
    hint_text = ("Known info: " + ", ".join(hints) + "\n") if hints else ""

    is_chinese = any("一" <= c <= "鿿" for c in title)
    summary_ex = "公司本週完成Pre-A融資，專注AI數位員工平台" if is_chinese else "company raised $5M for enterprise AI platform"

    return (
        f"{hint_text}"
        "Extract the main company from this startup/tech article. Output JSON only (no markdown, no explanation).\n"
        "If you cannot identify a specific real company name, output: {}\n\n"
        "Example output:\n"
        '{"companyName":"未來式智能","companyNameEn":"MindOS",'
        '"description":"AI-powered digital employee platform for enterprise automation.",'
        f'"summary":"{summary_ex}",'
        '"industry":["AI","SaaS"],'
        '"stage":"Pre-A","fundingAmountRaw":"數百萬美元",'
        '"investors":["紅杉中國"],"founded":"2023","website":"",'
        '"relevanceScore":7,"hotaiFitScore":6}\n\n'
        "relevanceScore scale (0-10) — news quality:\n"
        "  9-10: confirmed funding round with amount and investors named\n"
        "  7-8:  confirmed funding round or product launch, some details\n"
        "  5-6:  startup mentioned but funding/product unclear\n"
        "  3-4:  tangentially related (industry report, large corp with startup angle)\n"
        "  0-2:  irrelevant (macro, stock market, policy, large public corp)\n\n"
        f"hotaiFitScore scale (0-10) — strategic fit for {_HOTAI_CONTEXT}\n"
        "  9-10: core fit — directly addresses Hotai's main businesses "
        "(EV/charging, MaaS/mobility, ADAS/connected-vehicle, auto InsurTech, auto finance)\n"
        "  7-8:  strong fit — AI/data platform, loyalty/payment ecosystem, smart tourism, fleet management\n"
        "  5-6:  adjacent fit — general InsurTech, FinTech, logistics, mobility-adjacent tech\n"
        "  3-4:  weak fit — general SaaS/consumer tech with possible Hotai synergies\n"
        "  0-2:  no clear strategic fit for Hotai\n\n"
        "Now extract from:\n"
        f"Title: {title}\nSummary: {summary[:250]}"
    )


def _call_ollama_with_retry(prompt: str, max_retries: int = 3) -> dict | None:
    """call_ollama with exponential backoff on Timeout."""
    for attempt in range(1, max_retries + 1):
        try:
            return call_ollama(prompt)
        except requests.Timeout:
            wait = 2 ** attempt
            logger.warning("Ollama timeout (attempt %d/%d) — retry in %ds", attempt, max_retries, wait)
            if attempt < max_retries:
                time.sleep(wait)
        except Exception as e:
            logger.error("Ollama error: %s", e)
            return None
    logger.error("Ollama failed after %d retries", max_retries)
    return None


def call_ollama(prompt: str) -> dict | None:
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "format": "json",   # 強制 Ollama 輸出合法 JSON，大幅減少 parse 失敗
        "options": {
            "temperature": 0.05,
            "num_predict": 600,   # 350 容易截斷含 relevanceScore 的完整輸出
            "top_p": 0.9,
            "repeat_penalty": 1.1,
        },
    }
    resp = requests.post(f"{OLLAMA_BASE_URL}/api/generate", json=payload, timeout=180)
    if resp.status_code != 200:
        raise RuntimeError(f"Ollama {resp.status_code}: {resp.text[:100]}")
    return parse_response(resp.json().get("response", ""))


_VALID_INDUSTRIES = {
    "AI", "SaaS", "FinTech", "醫療", "物流", "電商", "Mobility",
    "InsurTech", "GreenTech", "EdTech", "生技", "半導體", "機器人",
    "CyberSecurity", "區塊鏈", "硬體", "其他",
}

# Qwen 常見輸出變體 → 標準標籤
_INDUSTRY_ALIAS: dict[str, str] = {
    # AI
    "artificial intelligence": "AI", "人工智能": "AI", "机器学习": "AI",
    "deep learning": "AI", "llm": "AI", "大模型": "AI", "ai软件": "AI",
    # SaaS
    "cloud software": "SaaS", "enterprise software": "SaaS", "雲端軟體": "SaaS",
    "b2b software": "SaaS", "企業軟體": "SaaS",
    # FinTech
    "financial technology": "FinTech", "金融科技": "FinTech", "支付": "FinTech",
    # 醫療
    "healthcare": "醫療", "medical": "醫療", "health": "醫療", "medtech": "醫療",
    # 生技
    "biotech": "生技", "biotechnology": "生技", "life sciences": "生技",
    # 電商
    "ecommerce": "電商", "e-commerce": "電商", "retail": "電商",
    # 物流
    "logistics": "物流", "supply chain": "物流",
    # Mobility
    "autonomous": "Mobility", "electric vehicle": "Mobility", "ev": "Mobility",
    "transportation": "Mobility",
    # 機器人
    "robotics": "機器人", "automation": "機器人",
    # 半導體
    "semiconductor": "半導體", "chip": "半導體", "晶片": "半導體",
    # GreenTech
    "clean energy": "GreenTech", "renewable": "GreenTech", "cleantech": "GreenTech",
    # EdTech
    "education": "EdTech", "e-learning": "EdTech",
    # CyberSecurity
    "cybersecurity": "CyberSecurity", "security": "CyberSecurity",
    # 其他（defense / hardware etc）
    "defense": "其他", "defence": "其他", "hardware": "硬體",
}

_STAGE_PLACEHOLDERS = {
    "<a輪/b輪/種子輪/ipo/etc, or blank>", "a輪/b輪/種子輪/ipo/etc",
    "ipo/etc", "輪次", "blank", "empty", "etc",
}

# 常見 stage 別名 → 標準輪次
_STAGE_ALIAS: dict[str, str] = {
    "seed": "種子輪", "seed round": "種子輪", "种子轮": "種子輪",
    "angel": "天使輪", "天使轮": "天使輪",
    "pre-a": "Pre-A", "pre a": "Pre-A", "prea": "Pre-A",
    "series a": "A輪", "a轮": "A輪", "a round": "A輪",
    "series b": "B輪", "b轮": "B輪",
    "series c": "C輪", "c轮": "C輪",
    "series d": "D輪", "d轮": "D輪",
    "strategic": "戰略投資", "战略投资": "戰略投資",
    "ipo": "IPO", "initial public offering": "IPO",
    # 模糊 → 清空
    "early stage": "", "early": "", "late stage": "", "growth": "",
    "venture": "", "unknown": "",
}

def _normalize_result(d: dict) -> dict:
    """清理 Qwen 輸出中常見的格式問題"""
    # 拆分 "AI/SaaS" → ["AI", "SaaS"]，並解析別名
    raw_ind = d.get("industry", [])
    if isinstance(raw_ind, list):
        cleaned = []
        for item in raw_ind:
            for part in re.split(r"[/、,，]", str(item)):
                part = part.strip()
                part_lower = part.lower()
                if part in _VALID_INDUSTRIES:
                    cleaned.append(part)
                elif part_lower in _INDUSTRY_ALIAS:
                    cleaned.append(_INDUSTRY_ALIAS[part_lower])
                elif part:
                    matched = next((v for k, v in _INDUSTRY_ALIAS.items() if k in part_lower), None)
                    cleaned.append(matched if matched else "其他")
        industry = list(dict.fromkeys(cleaned))
    else:
        industry = []

    # 若 Qwen 沒輸出 industry，用 description + companyName 做 keyword 推導
    if not industry or industry == ["其他"]:
        combined = " ".join([
            d.get("description", ""), d.get("companyName", ""),
            d.get("companyNameEn", ""), d.get("summary", ""),
        ]).lower()
        derived = []
        for cat, kws in FIT_KEYWORDS.items():
            if any(kw.lower() in combined for kw in kws):
                # FIT_KEYWORDS 分類 → 映射到 industry 標籤
                ind_map = {
                    "Mobility": "Mobility", "InsurTech": "InsurTech", "FinTech": "FinTech",
                    "Healthcare": "醫療", "Logistics": "物流", "AI": "AI",
                    "SaaS": "SaaS", "Ecommerce": "電商",
                }
                if cat in ind_map:
                    derived.append(ind_map[cat])
        industry = list(dict.fromkeys(derived)) or ["其他"]

    d["industry"] = industry[:3]  # 最多 3 個

    # 正規化 stage
    stage = str(d.get("stage", "") or "")
    stage_lower = stage.lower()
    if stage_lower in _STAGE_PLACEHOLDERS or stage.startswith("<") or stage_lower in ("none", "null", "undefined"):
        d["stage"] = ""
    elif stage_lower in _STAGE_ALIAS:
        d["stage"] = _STAGE_ALIAS[stage_lower]
    elif stage not in {"種子輪", "天使輪", "Pre-A", "A輪", "B輪", "C輪", "D輪", "戰略投資", "IPO"}:
        # 嘗試部分匹配
        matched = next((v for k, v in _STAGE_ALIAS.items() if k in stage_lower), None)
        d["stage"] = matched if matched is not None else ""

    # 清除 fundingAmountRaw 佔位符
    amt = str(d.get("fundingAmountRaw", "") or "")
    if amt.lower() in ("none", "null", "empty", "blank", "undefined", ""):
        d["fundingAmountRaw"] = ""

    # investors 確保是 list
    inv = d.get("investors", [])
    if isinstance(inv, str):
        d["investors"] = [inv] if inv else []

    return d


def parse_response(text: str) -> dict | None:
    try:
        clean = re.sub(r"```json\n?|```\n?", "", text).strip()
        try:
            d = json.loads(clean)
            return _normalize_result(d) if isinstance(d, dict) else None
        except json.JSONDecodeError:
            pass
        # 找第一個 { 到最後一個 }，避免 non-greedy *? 遇到 nested dict 就截斷
        start = clean.find("{")
        end = clean.rfind("}")
        if start != -1 and end > start:
            d = json.loads(clean[start:end + 1])
            return _normalize_result(d) if isinstance(d, dict) else None
    except Exception:
        pass
    return None


# ── Gemini（備用） ──

def call_gemini(prompt: str) -> dict | None:
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 600},
    }
    resp = requests.post(f"{GEMINI_ENDPOINT}?key={GEMINI_API_KEY}", json=payload, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Gemini {resp.status_code}: {resp.text[:150]}")
    try:
        raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        return None
    return parse_response(raw)


# ── Scoring ──

def calc_ml_score(result: dict) -> tuple[float, list[str]]:
    """
    Feature-based ML score (0-10).

    配分：
      keyword 類別命中  0-4 pt  (每類最多 1pt，8 類上限 4pt)
      融資金額信號      0-2 pt
      輪次明確性        0-1 pt
      投資人資料        0-1 pt
      描述品質          0-1 pt
      資料完整度        0-1 pt
    """
    combined = " ".join([
        result.get("description", ""),
        result.get("summary", ""),
        " ".join(result.get("industry", [])),
        result.get("companyName", ""),
        result.get("companyNameEn", ""),
    ]).lower()

    score = 0.0
    tags: list[str] = []

    # 1. Keyword category match (up to 4 pts)
    kw_score = 0.0
    for cat, keywords in FIT_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw.lower() in combined)
        if hits:
            kw_score += min(hits * 0.5, 1.0)
            tags.append(cat)
    score += min(kw_score, 4.0)

    # 2. Funding amount (up to 2 pts)
    funding_usd = result.get("fundingAmountUSD") or 0
    funding_raw = result.get("fundingAmountRaw") or ""
    if funding_usd >= 50_000_000:
        score += 2.0
    elif funding_usd >= 10_000_000:
        score += 1.5
    elif funding_usd >= 1_000_000:
        score += 1.0
    elif funding_raw:
        score += 0.5

    # 3. Stage specificity (up to 1 pt)
    stage_weights = {
        "IPO": 1.0, "D輪": 0.9, "C輪": 0.8, "B輪": 0.7,
        "A輪": 0.6, "Pre-A": 0.5, "天使輪": 0.4, "種子輪": 0.3,
        "戰略投資": 0.5,
    }
    score += stage_weights.get(result.get("stage", ""), 0)

    # 4. Investor data (up to 1 pt)
    investors = result.get("investors") or []
    if len(investors) >= 3:
        score += 1.0
    elif len(investors) >= 1:
        score += 0.5

    # 5. Description quality (up to 1 pt)
    desc = result.get("description") or ""
    if len(desc) >= 80:
        score += 1.0
    elif len(desc) >= 40:
        score += 0.5

    return min(round(score, 1), 10.0), tags


_PREFERRED_STAGES   = {"A輪", "B輪", "C輪", "Pre-A", "戰略投資"}  # 和泰較可能投的輪次
_PREFERRED_REGIONS  = {"台灣", "東南亞"}                          # 地理偏好加分

def _business_rule_score(result: dict) -> float:
    """
    業務規則加分（0-10），作為第三個評分維度：
      地區加分：台灣/東南亞各 +2；中國 +1
      輪次偏好：A/B/C/Pre-A/戰略 +2；天使/種子 +1；IPO/D輪 +0.5
      業務名直接命中：companyName 含和泰子公司相關業務詞 +2
    """
    score = 0.0
    region = result.get("region", "")
    if region in _PREFERRED_REGIONS:
        score += 2.0
    elif region == "中國":
        score += 1.0

    stage = result.get("stage", "")
    if stage in _PREFERRED_STAGES:
        score += 2.0
    elif stage in {"天使輪", "種子輪"}:
        score += 1.0
    elif stage in {"IPO", "D輪"}:
        score += 0.5

    # 直接命中和泰核心業務詞
    combined = " ".join([
        result.get("companyName", ""), result.get("description", ""),
        " ".join(result.get("industry", [])),
    ]).lower()
    core_hits = ["mobility", "maas", "insurtech", "telematics", "forklift", "agv",
                 "ev charging", "充電", "車險", "租車", "叉車", "倉儲", "daikin", "大金",
                 "bus body", "巴士", "auto finance", "車貸"]
    if any(h in combined for h in core_hits):
        score += 2.0
    return min(score, 10.0)


def calc_fit_score(result: dict) -> dict:
    """
    三分量混合評分：
      fitScore     = 25% ML + 50% Qwen relevanceScore + 25% 業務規則（新聞品質 × 和泰業務）
      hotaiFitScore = 25% ML + 50% Qwen hotaiFitScore + 25% 業務規則（和泰策略適配）
    若 Qwen 未回傳對應欄位，補 ML score 填充。
    """
    ml_score, tags = calc_ml_score(result)
    rule_score = _business_rule_score(result)

    def _parse_qwen(field: str) -> float | None:
        try:
            raw = result.get(field)
            if raw is not None:
                return max(0.0, min(10.0, float(raw)))
        except (TypeError, ValueError):
            pass
        return None

    qwen_score = _parse_qwen("relevanceScore")
    hotai_qwen = _parse_qwen("hotaiFitScore")

    if qwen_score is not None:
        final = round(0.25 * ml_score + 0.50 * qwen_score + 0.25 * rule_score, 1)
    else:
        final = round(0.50 * ml_score + 0.50 * rule_score, 1)

    if hotai_qwen is not None:
        hotai_final = round(0.25 * ml_score + 0.50 * hotai_qwen + 0.25 * rule_score, 1)
    else:
        hotai_final = round(0.50 * ml_score + 0.50 * rule_score, 1)

    return {
        "fitScore":       min(round(final, 1), 10.0),
        "fitTags":        tags,
        "mlScore":        ml_score,
        "ruleScore":      rule_score,
        "qwenScore":      qwen_score,
        "hotaiFitScore":  min(round(hotai_final, 1), 10.0),
        "hotaiQwenScore": hotai_qwen,
    }


def normalize_funding(raw: str) -> int:
    if not raw:
        return 0
    t = raw.lower()
    m = re.search(r"[\d.]+", t)
    n = float(m.group(0)) if m else 0
    if "億" in t or "亿" in t:                     n *= 100_000_000
    elif "千萬" in t:                               n *= 10_000_000
    elif "萬" in t or "万" in t:                   n *= 10_000
    elif "billion" in t or re.search(r"\db\b", t): n *= 1_000_000_000
    elif "m" in t or "百萬" in t:                  n *= 1_000_000
    elif "k" in t or "千" in t:                    n *= 1_000
    if "twd" in t or "台幣" in t:      n /= FX["TWD"]
    elif "cny" in t or "人民幣" in t:  n /= FX["CNY"]
    elif "sgd" in t:                   n /= FX["SGD"]
    return round(n / 100) * 100


def today_collection() -> str:
    return "startups_" + datetime.now().strftime("%Y-%m-%d")


def collection_for_tab(tab_name: str) -> str:
    """Derive Firebase collection name from the Sheets tab name.
    'raw_2026-05-20' → 'startups_2026-05-20'
    Falls back to today if tab_name has no date suffix.
    """
    if tab_name and tab_name.startswith("raw_"):
        return "startups_" + tab_name[4:]
    return today_collection()


# ── Main processor（三階段流程） ──

BATCH_SIZE = 10  # Stage 1 每批多少篇

def process_raw_articles_by_region(region: str, tab_name: str) -> dict:
    ws = get_sheet(tab_name)
    rows = ws.get_all_values()
    if len(rows) <= 1:
        logger.info("No articles in %s", tab_name)
        return {"saved": 0, "remaining": 0}

    unprocessed = []
    for i, row in enumerate(rows[1:], start=2):
        row_region    = row[4].strip() if len(row) > 4 else ""
        row_processed = row[6].strip().lower() if len(row) > 6 else "false"
        if row_region == region and row_processed in ("false", ""):
            unprocessed.append({"row": i, "data": row})

    logger.info("process [%s]: %d unprocessed", region, len(unprocessed))
    if not unprocessed:
        return {"saved": 0, "remaining": 0}

    region_limit = 30 if region == "台灣" else MAX_GEMINI_PER_RUN
    batch = unprocessed[:region_limit]
    col_ref = collection_for_tab(tab_name)  # Bug1 fix: derive from tab_name, not today's date

    # ── Stage 0：規則分類 ──
    to_skip, to_extract, to_classify = [], [], []

    NINETY_DAYS_AGO = datetime.now(timezone.utc) - timedelta(days=90)

    for item in batch:
        row_data = item["data"]
        title_raw    = row_data[1] if len(row_data) > 1 else ""
        content      = row_data[2] if len(row_data) > 2 else ""
        published_at = row_data[7] if len(row_data) > 7 else ""  # col 8 added by scraper
        title, summary = parse_content_field(content)
        if not title:
            title = title_raw

        # Date filter: skip articles older than 90 days (Google News sometimes recirculates old articles)
        if published_at:
            try:
                from email.utils import parsedate_to_datetime
                pub_dt = parsedate_to_datetime(published_at)
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                if pub_dt < NINETY_DAYS_AGO:
                    logger.info("   📅 Skip old article (%s): %s", published_at[:16], title[:50])
                    item["title"]     = title
                    item["summary"]   = summary
                    item["prefilled"] = {}
                    to_skip.append(item)
                    continue
            except Exception:
                pass  # unparseable date → proceed normally

        amount, stage = extract_funding_from_title(title)
        item["title"]   = title
        item["summary"] = summary
        item["prefilled"] = {"fundingAmountRaw": amount, "stage": stage}

        verdict = rule_classify(title)
        if verdict == "skip":
            to_skip.append(item)
        elif verdict == "startup":
            to_extract.append(item)
        else:
            to_classify.append(item)

    logger.info(
        "Stage0 [%s]: skip=%d startup=%d ambiguous=%d",
        region, len(to_skip), len(to_extract), len(to_classify)
    )

    # 跳過的直接標 processed（batch update 減少 Sheets API 呼叫數）
    if to_skip:
        ws.batch_update([
            {"range": f"G{item['row']}", "values": [["true"]]}
            for item in to_skip
        ])
        logger.info("   ⏭️  Rule-skip: %d articles", len(to_skip))
        time.sleep(1)

    # ── Stage 1：批次分類 ambiguous ──
    if to_classify:
        logger.info("Stage1: batch-classify %d ambiguous articles", len(to_classify))
        for batch_start in range(0, len(to_classify), BATCH_SIZE):
            chunk = to_classify[batch_start:batch_start + BATCH_SIZE]
            try:
                accepted_idx = batch_classify_titles(chunk)
                for idx, item in enumerate(chunk):
                    if idx in accepted_idx:
                        to_extract.append(item)
                    else:
                        ws.update_cell(item["row"], 7, "true")
                logger.info(
                    "   Stage1 batch %d-%d: %d/%d accepted",
                    batch_start, batch_start + len(chunk) - 1,
                    len(accepted_idx), len(chunk)
                )
            except Exception as e:
                logger.error("Stage1 batch error: %s — fallback to extract all", e)
                to_extract.extend(chunk)
            time.sleep(3)

    # ── Stage 2：個別提取 ──
    logger.info("Stage2: extract fields for %d confirmed startups", len(to_extract))
    saved = skipped = errors = 0

    for j, item in enumerate(to_extract):
        row_data = item["data"]
        url      = row_data[0] if len(row_data) > 0 else ""
        source   = row_data[3] if len(row_data) > 3 else ""
        title    = item["title"]
        summary  = item["summary"]
        prefilled = item["prefilled"]

        # Input validation: skip rows with no URL (scraper output integrity check)
        if not url:
            logger.warning("   ⚠️  Row %d has no URL — marking skipped", item["row"])
            ws.update_cell(item["row"], 7, "skipped")
            skipped += 1
            continue

        try:
            logger.info("🔍 [%d/%d] %s", j + 1, len(to_extract), title[:60])
            prompt = build_extract_prompt(title, summary, url, prefilled)
            result = _call_ollama_with_retry(prompt)  # retry wrapper with backoff

            if result is None:
                # Ollama returned unparseable response after retries — flag for investigation
                logger.warning("   ⚠️  Ollama parse failure for: %s", title[:60])
                ws.update_cell(item["row"], 7, "error")  # Bug3 fix: "error" not "true"
                errors += 1
                time.sleep(2)
                continue

            company = result.get("companyName", "")
            if _is_valid_company_name(company):
                # 補回 prefilled 欄位（Qwen 可能漏掉）
                if not result.get("stage") and prefilled.get("stage"):
                    result["stage"] = prefilled["stage"]
                if not result.get("fundingAmountRaw") and prefilled.get("fundingAmountRaw"):
                    result["fundingAmountRaw"] = prefilled["fundingAmountRaw"]

                result["isStartup"]        = True
                result["region"]           = region
                result["sourceId"]         = source
                result["sourceUrl"]        = url
                result["extractedAt"]      = datetime.now(timezone.utc).isoformat()
                result["status"]           = "new"
                result["fundingAmountUSD"] = normalize_funding(result.get("fundingAmountRaw", ""))
                fd = calc_fit_score(result)

                def _clamp(v: float) -> float:
                    return max(0.0, min(10.0, v))

                result["fitScore"]       = _clamp(fd["fitScore"])
                result["fitTags"]        = fd["fitTags"]
                result["mlScore"]        = _clamp(fd["mlScore"])
                result["ruleScore"]      = _clamp(fd["ruleScore"])
                result["hotaiFitScore"]  = _clamp(fd["hotaiFitScore"])
                if fd["qwenScore"] is not None:
                    result["qwenScore"] = _clamp(fd["qwenScore"])
                if fd["hotaiQwenScore"] is not None:
                    result["hotaiQwenScore"] = _clamp(fd["hotaiQwenScore"])

                # Idempotent write: URL hash as doc_id prevents duplicates on re-run
                doc_id = hashlib.md5(url.encode()).hexdigest()[:16]
                firestore_write(col_ref, result, doc_id=doc_id)
                mirror_to_sheets(result)   # mirror scores to Sheets for human review
                ws.update_cell(item["row"], 7, "true")
                saved += 1
                logger.info("   ✅ %s [fit:%.1f hotai:%.1f]", company, result["fitScore"], result["hotaiFitScore"])
            else:
                # Valid JSON from Qwen but no identifiable company — expected for non-startups
                reason = f"placeholder name: '{company}'" if company else "no companyName"
                logger.info("   ⚠️  Skip: %s", reason)
                ws.update_cell(item["row"], 7, "skipped")  # Bug3 fix: "skipped" not "true"
                skipped += 1

            time.sleep(3)

        except Exception as e:
            logger.error("   ❌ row %d: %s", item["row"], e)
            ws.update_cell(item["row"], 7, "error")
            errors += 1
            time.sleep(2)

    remaining = max(0, len(unprocessed) - region_limit)
    logger.info("📊 [%s] saved=%d skipped=%d errors=%d remaining=%d",
                region, saved, skipped, errors, remaining)
    if errors:
        logger.error("⚠️  %d rows marked 'error' in [%s] — review and retry manually", errors, region)
    return {"saved": saved, "remaining": remaining, "errors": errors}
