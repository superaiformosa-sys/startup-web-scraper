import os
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY       = os.environ["GEMINI_API_KEY"]
FIREBASE_PROJECT_ID  = os.environ["FIREBASE_PROJECT_ID"]
SHEETS_ID            = os.environ["SHEETS_ID"]
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")  # Service Account JSON 字串

FIRESTORE_BASE = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/(default)/documents"

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent"

MAX_ARTICLES_PER_SOURCE = 30
MAX_GEMINI_PER_RUN      = 20

SOURCES = [
    # ── 台灣 ──
    {"id": "bnext",      "name": "數位時代",          "rss": "https://www.bnext.com.tw/rss",                                                 "region": "台灣", "enabled": True},
    {"id": "meet",       "name": "Meet 創業小聚",      "rss": "https://meet.bnext.com.tw/rss",                                               "region": "台灣", "enabled": True},
    {"id": "tc_tw",      "name": "TechCrunch TW RSS",  "rss": "https://news.google.com/rss/search?q=新創+募資+台灣&hl=zh-TW&gl=TW&ceid=TW:zh-Hant", "region": "台灣", "enabled": True},
    # ── 中國 ──
    {"id": "36kr",       "name": "36氪",              "rss": "https://36kr.com/feed",                                                        "region": "中國", "enabled": True},
    {"id": "lieyunwang", "name": "獵雲網",             "rss": "https://www.lieyunwang.com/rss.xml",                                          "region": "中國", "enabled": True},
    {"id": "kr_asia",    "name": "KrASIA",             "rss": "https://kr-asia.com/feed",                                                     "region": "中國", "enabled": True},
    {"id": "cn_google",  "name": "中國新創 Google News","rss": "https://news.google.com/rss/search?q=中国+创业+融资+startup&hl=zh-CN&gl=CN&ceid=CN:zh-Hans", "region": "中國", "enabled": True},
    {"id": "cn_google2", "name": "中國科技 Google News","rss": "https://news.google.com/rss/search?q=中国+科技+独角兽+IPO&hl=zh-CN&gl=CN&ceid=CN:zh-Hans", "region": "中國", "enabled": True},
    # ── 東南亞 ──
    {"id": "e27_gn",     "name": "e27 Google News",   "rss": "https://news.google.com/rss/search?q=e27+startup+funding+southeast+asia&hl=en&gl=SG&ceid=SG:en", "region": "東南亞", "enabled": True},
    {"id": "dealstreet", "name": "DealStreetAsia",     "rss": "https://www.dealstreetasia.com/feed/",                                         "region": "東南亞", "enabled": True},
    {"id": "techinasia", "name": "Tech in Asia",       "rss": "https://www.techinasia.com/feed",                                              "region": "東南亞", "enabled": True},
    {"id": "sea_google", "name": "SEA新創 Google News", "rss": "https://news.google.com/rss/search?q=startup+funding+series+southeast+asia+2025&hl=en&gl=SG&ceid=SG:en", "region": "東南亞", "enabled": True},
    {"id": "sea_google2","name": "SEA科技 Google News", "rss": "https://news.google.com/rss/search?q=Indonesia+Vietnam+Thailand+startup+raised&hl=en&gl=SG&ceid=SG:en", "region": "東南亞", "enabled": True},
    # ── 全球 ──
    {"id": "tc_startup", "name": "TechCrunch Startups","rss": "https://techcrunch.com/category/startups/feed/",                               "region": "全球", "enabled": True},
    {"id": "venturebeat","name": "VentureBeat",        "rss": "https://venturebeat.com/category/business/feed/",                              "region": "全球", "enabled": True},
    {"id": "crunchbase", "name": "Crunchbase News",    "rss": "https://news.crunchbase.com/feed/",                                            "region": "全球", "enabled": True},
    {"id": "global_gn",  "name": "全球新創 Google News","rss": "https://news.google.com/rss/search?q=startup+funding+series+A+B+2025&hl=en&gl=US&ceid=US:en", "region": "全球", "enabled": True},
    {"id": "global_gn2", "name": "全球科技 Google News","rss": "https://news.google.com/rss/search?q=unicorn+IPO+venture+capital+2025&hl=en&gl=US&ceid=US:en", "region": "全球", "enabled": True},
]

REGION_MAP = {s["id"]: s["region"] for s in SOURCES}

FIT_KEYWORDS = {
    "Mobility":    ["自駕", "電動車", "EV", "MaaS", "車隊", "停車", "充電", "Fleet", "共乘", "車聯網", "ADAS", "autonomous"],
    "InsurTech":   ["保險", "insurtech", "核保", "理賠", "再保", "保費", "UBI", "insurance"],
    "FinTech":     ["支付", "fintech", "借貸", "區塊鏈", "數位銀行", "理財", "信用", "payment", "lending", "crypto"],
    "Healthcare":  ["醫療", "健康", "medtech", "遠距", "生技", "基因", "藥物", "診斷", "health", "biotech", "pharma"],
    "Logistics":   ["物流", "供應鏈", "倉儲", "配送", "冷鏈", "運輸", "logistics", "supply chain", "delivery"],
    "AI":          ["人工智慧", "機器學習", "深度學習", "大模型", "LLM", "AI", "artificial intelligence", "machine learning"],
    "SaaS":        ["SaaS", "雲端", "訂閱", "企業軟體", "ERP", "CRM", "cloud", "B2B software"],
    "Ecommerce":   ["電商", "電子商務", "零售", "marketplace", "ecommerce", "retail"],
}


SKIP_KEYWORDS = ["廣告","sponsor","特別報導","白皮書","webinar","招募","徵才"]
FX = {"TWD": 32, "CNY": 7.2, "JPY": 155, "SGD": 0.74, "MYR": 4.7}
