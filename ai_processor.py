import json
import re
import time
import logging
from datetime import datetime, timezone
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


# ── Google Sheets client ──

def get_sheets_client():
    creds = Credentials.from_service_account_info(json.loads(GOOGLE_CREDENTIALS_JSON), scopes=SCOPES)
    return gspread.authorize(creds)


def get_sheet(tab_name: str) -> gspread.Worksheet:
    gc = get_sheets_client()
    return gc.open_by_key(SHEETS_ID).worksheet(tab_name)


# ── Prompts ──

def build_prompt_strict(text: str, url: str) -> str:
    tw_hint = ""
    if any(d in url for d in TAIWAN_DOMAINS):
        tw_hint = (
            "SPECIAL INSTRUCTION: This is a Taiwanese media source. Be EXTREMELY generous. "
            "Accept ANY article that has a company doing something in tech, innovation, or business in Taiwan. "
            "If there is ANY doubt, choose ACCEPT over REJECT.\n\n"
        )
    return (
        "You are a startup analyst building an investment database.\n" + tw_hint +
        "Extract the main company featured in this article.\n\n"
        "REJECT → {\"isStartup\":false} ONLY if:\n"
        "- Article mentions no identifiable company by name, OR\n"
        "- ALL companies are large public corporations (TSMC/台積電/Apple/Samsung/鴻海) with zero startup or innovation angle, OR\n"
        "- Purely stock price, commodity price, or natural disaster news\n\n"
        "ACCEPT for any company that is: a startup, SME, scale-up, spin-off, or innovative business receiving coverage for:\n"
        "funding/investment • product launch • founder story • award • accelerator • partnership • technology breakthrough\n\n"
        "Return valid JSON only, no markdown.\n\n"
        'ACCEPT schema: {"isStartup":true,"companyName":"公司名稱（繁體中文）","companyNameEn":"English or empty",'
        '"description":"2 sentences 60-120 chars about what company does","summary":"繁體中文50-100字摘要：這篇報導的重點是什麼？",'
        '"stage":"種子輪/天使輪/Pre-A/A輪/B輪/C輪/D輪/戰略投資 or empty","fundingAmountRaw":"amount or empty",'
        '"investors":["names or empty"],"industry":["2-4: AI/SaaS/FinTech/醫療/物流/電商/Mobility/InsurTech/GreenTech/EdTech/CyberSecurity/生技/半導體/硬體/機器人/區塊鏈/其他"],'
        '"founded":"year or empty","website":"URL or empty"}\n\n'
        f"Article URL: {url}\nArticle: {text[:4000]}"
    )


def build_prompt_broad(text: str, url: str) -> str:
    return (
        "You are collecting startup company profiles for an investment database.\n"
        "REJECT → {\"isStartup\":false} ONLY if:\n"
        "- Article is purely about government policy, geopolitics, stock market, or commodity prices\n"
        "- The subject is clearly a large public corporation with no startup/innovation angle\n"
        "- Article has no identifiable company as its subject\n\n"
        "ACCEPT any article that features a startup or innovative company, including:\n"
        "- Company founding stories or profiles\n"
        "- Product or technology introductions\n"
        "- Funding announcements (any size)\n"
        "- Founder interviews\n"
        "- Accelerator/incubator participants\n"
        "- Award winners or competition participants\n\n"
        "Base response ONLY on provided text. Return valid JSON only, no markdown.\n\n"
        'ACCEPT schema: {"isStartup":true,"companyName":"公司名稱（中文或英文）","companyNameEn":"English or empty",'
        '"description":"2-3 sentences, 80-150 chars","summary":"繁體中文摘要，50-100字",'
        '"stage":"種子輪/天使輪/Pre-A/A輪/B輪/C輪/D輪/戰略投資 or empty","fundingAmountRaw":"amount if mentioned or empty",'
        '"investors":["names if mentioned, else empty array"],"industry":["2-4: AI/SaaS/FinTech/醫療/物流/電商/Mobility/InsurTech/GreenTech/EdTech/CyberSecurity/生技/半導體/機器人/其他"],'
        '"founded":"year or empty","website":"URL or empty"}\n\n'
        f"Article URL: {url}\nArticle: {text[:4000]}"
    )


# ── Gemini call ──

def call_gemini(prompt: str) -> dict | None:
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 800},
    }
    resp = requests.post(
        f"{GEMINI_ENDPOINT}?key={GEMINI_API_KEY}",
        json=payload, timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Gemini {resp.status_code}: {resp.text[:150]}")

    data = resp.json()
    try:
        raw = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        return None
    return parse_response(raw)


def parse_response(text: str) -> dict | None:
    try:
        clean = re.sub(r"```json\n?", "", text)
        clean = re.sub(r"```\n?", "", clean).strip()
        try:
            return json.loads(clean)
        except json.JSONDecodeError:
            pass
        m = re.search(r"\{[\s\S]*\}", clean)
        if m:
            return json.loads(m.group(0))
    except Exception:
        pass
    return None


def extract_with_strategy(text: str, url: str, strategy: str) -> dict | None:
    if not text or len(text.strip()) < 50:
        return None
    prompt = build_prompt_strict(text, url) if strategy == "strict" else build_prompt_broad(text, url)
    return call_gemini(prompt)


# ── Scoring ──

def calc_fit_score(s: dict) -> dict:
    combined = " ".join([
        s.get("description", ""),
        " ".join(s.get("industry", [])),
        " ".join(s.get("fitTags", [])),
        s.get("companyName", ""),
    ]).lower()
    score, tags = 0, []
    for cat, keywords in FIT_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw.lower() in combined)
        if hits:
            score += min(hits, 2)
            tags.append(cat)
    return {"fitScore": round(min(score / 2, 10) * 10) / 10, "fitTags": tags}


def normalize_funding(raw: str) -> int:
    if not raw:
        return 0
    t = raw.lower()
    m = re.search(r"[\d.]+", t)
    n = float(m.group(0)) if m else 0
    if "億" in t:         n *= 100_000_000
    elif "千萬" in t:     n *= 10_000_000
    elif "萬" in t:       n *= 10_000
    elif "m" in t or "百萬" in t: n *= 1_000_000
    elif "k" in t or "千" in t:   n *= 1_000
    if "twd" in t or "台幣" in t:      n /= FX["TWD"]
    elif "cny" in t or "人民幣" in t:  n /= FX["CNY"]
    elif "sgd" in t:                   n /= FX["SGD"]
    return round(n / 100) * 100


def today_collection() -> str:
    return "startups_" + datetime.now().strftime("%Y-%m-%d")


# ── Main processor ──

def process_raw_articles_by_region(region: str, tab_name: str) -> dict:
    ws = get_sheet(tab_name)
    rows = ws.get_all_values()
    if len(rows) <= 1:
        logger.info("No articles in %s", tab_name)
        return {"saved": 0, "remaining": 0}

    headers = rows[0]
    unprocessed = []
    for i, row in enumerate(rows[1:], start=2):
        row_region    = row[4].strip() if len(row) > 4 else ""
        row_processed = row[6].strip().lower() if len(row) > 6 else "false"
        if row_region == region and row_processed in ("false", ""):
            unprocessed.append({"row": i, "data": row})

    logger.info("process [%s]: %d unprocessed", region, len(unprocessed))
    if not unprocessed:
        return {"saved": 0, "remaining": 0}

    region_limit = max(MAX_GEMINI_PER_RUN, 20) if region == "台灣" else MAX_GEMINI_PER_RUN
    limit = min(len(unprocessed), region_limit)
    saved = skipped = 0
    col_ref = today_collection()

    for j, item in enumerate(unprocessed[:limit]):
        row_data = item["data"]
        url     = row_data[0] if len(row_data) > 0 else ""
        content = row_data[2] if len(row_data) > 2 else ""
        source  = row_data[3] if len(row_data) > 3 else ""

        try:
            logger.info("🔍 [%d/%d] %s", j + 1, limit, (row_data[1] if len(row_data) > 1 else "")[:55])

            result = extract_with_strategy(content, url, "strict")
            if not result or result.get("isStartup") is False:
                time.sleep(8)
                result = extract_with_strategy(content, url, "broad")

            if result and result.get("isStartup") is not False:
                result["region"]       = region
                result["sourceId"]     = source
                result["sourceUrl"]    = url
                result["extractedAt"]  = datetime.now(timezone.utc).isoformat()
                result["status"]       = "new"
                result["fundingAmountUSD"] = normalize_funding(result.get("fundingAmountRaw", ""))
                fd = calc_fit_score(result)
                result["fitScore"] = fd["fitScore"]
                result["fitTags"]  = fd["fitTags"]
                firestore_write(col_ref, result)
                saved += 1
                logger.info("   ✅ %s [score:%s]", result.get("companyName", "?"), result["fitScore"])
            else:
                skipped += 1
                logger.info("   ⏭️  Not startup")

            ws.update_cell(item["row"], 7, "true")
            time.sleep(12)

        except Exception as e:
            logger.error("   ❌ %s", e)
            ws.update_cell(item["row"], 7, "error")
            time.sleep(6)

    remaining = max(0, len(unprocessed) - limit)
    logger.info("📊 [%s] saved=%d skipped=%d remaining=%d", region, saved, skipped, remaining)
    return {"saved": saved, "remaining": remaining}
