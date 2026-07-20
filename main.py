import sys
import time
import logging
import datetime
import subprocess
import requests
from scraper import run_all_scrapers
from ai_processor import process_raw_articles_by_region, OLLAMA_MODEL, get_sheet
from weekly_report import load_all_tabs, analyze_rows, render_html
from email_sender import send_weekly_report
from config import OLLAMA_BASE_URL as OLLAMA_URL, REGIONS, LOOPED_REGIONS, REGION_WEEKLY_CAP

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

def ensure_ollama_running(timeout: int = 30) -> bool:
    try:
        requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        logger.info("Ollama already running")
        return True
    except Exception:
        pass

    logger.info("Ollama not running — starting ollama serve ...")
    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        logger.error("Ollama binary not found — Ollama is not installed; skipping AI processing")
        return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2)
        try:
            requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
            logger.info("Ollama started successfully")
            return True
        except Exception:
            pass

    logger.error("Ollama failed to start within %ds", timeout)
    return False

def warm_up_ollama_model(timeout: int = 280) -> None:
    """Force the model into memory with a tiny throwaway generate call.
    Without this, the first real classify+extract call pays both the
    model-load cost AND inference cost inside the same request timeout —
    that's what caused the timeouts in the 2026-06-08 run."""
    try:
        start = time.time()
        requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": "Hi", "stream": False,
                  "think": False, "options": {"num_predict": 1}},
            timeout=timeout,
        )
        logger.info("Ollama model warmed up (%.1fs)", time.time() - start)
    except Exception as e:
        logger.warning("Ollama warm-up failed (will proceed anyway): %s", e)

def step1_scrape(tab_name=None):
    start = datetime.datetime.now()
    logger.info("Step1 START: %s", start.isoformat())
    tab_name = run_all_scrapers(tab_name)
    duration = (datetime.datetime.now() - start).total_seconds()
    logger.info("Step1 DONE: %.1fs, tab: %s", duration, tab_name)
    return tab_name

def step2_ai(tab_name=None):
    start = datetime.datetime.now()
    logger.info("Step2 START (AI+Firebase)")
    # Ollama/Qwen 只是 Gemini 額度用完時的本機備援，不是前提條件 —— GitHub Actions
    # 這種沒裝 Ollama 的環境也該能單靠 Gemini 跑完整個 Step2，而不是直接放棄。
    if ensure_ollama_running():
        warm_up_ollama_model()
    else:
        logger.warning("Ollama unavailable — proceeding with Gemini only this run (no local fallback)")
    if tab_name is None:
        tab_name = "raw_" + datetime.datetime.now().strftime("%Y-%m-%d")
    total_saved = total_errors = 0
    failed_regions = []
    for region in REGIONS:
        try:
            if region in LOOPED_REGIONS:
                logger.info("Processing region: %s (weekly cap=%d)", region, REGION_WEEKLY_CAP)
                result = process_raw_articles_by_region(region, tab_name, limit=REGION_WEEKLY_CAP)
                total_saved  += result.get("saved", 0)
                total_errors += result.get("errors", 0)
                if result.get("remaining", 0) > 0:
                    logger.warning(
                        "⚠️  [%s] weekly cap (%d) reached — %d articles left unprocessed this run "
                        "(will NOT be retried — next week's tab starts fresh)",
                        region, REGION_WEEKLY_CAP, result["remaining"],
                    )
            else:
                logger.info("Processing region: %s", region)
                result = process_raw_articles_by_region(region, tab_name)
                total_saved  += result.get("saved", 0)
                total_errors += result.get("errors", 0)
        except Exception as e:
            logger.error("region %s FAILED: %s", region, e)
            failed_regions.append(region)
    duration = (datetime.datetime.now() - start).total_seconds()
    logger.info("Step2 DONE: %.1fs | saved=%d errors=%d", duration, total_saved, total_errors)
    if failed_regions or total_errors:
        logger.error("⚠️  Step2 had failures — regions_failed=%s row_errors=%d",
                     failed_regions, total_errors)

def step3_report(tab_name: str | None = None, send_email: bool = False):
    start = datetime.datetime.now()
    logger.info("Step3 START (weekly report)")
    try:
        rows = load_all_tabs()
        if not rows and tab_name:
            from weekly_report import load_single_tab
            rows = load_single_tab(tab_name)
    except Exception as e:
        logger.error("Step3 load error: %s", e)
        rows = []

    if not rows:
        logger.warning("Step3: no rows found — skipping report")
        return

    stats = analyze_rows(rows)
    from weekly_report import load_scored_map
    from ai_processor import collection_for_tab
    scored_map = load_scored_map(collection_for_tab(tab_name or ""))
    stats["hotai_top"] = sorted(
        [d for d in scored_map.values() if d.get("hotaiFitScore") is not None],
        key=lambda d: d["hotaiFitScore"], reverse=True,
    )[:10]
    html = render_html(tab_name or "all_tabs", rows, stats, scored_map=scored_map)

    if send_email:
        ok = send_weekly_report(html)
        if not ok:
            logger.error("Step3: email failed — report saved to local file")
    else:
        filename = f"weekly_report_{datetime.date.today()}.html"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Step3: report saved to %s (use --email to send)", filename)

    duration = (datetime.datetime.now() - start).total_seconds()
    logger.info("Step3 DONE: %.1fs", duration)


def step4_dashboard(out_path: str = "dashboard.html"):
    start = datetime.datetime.now()
    logger.info("Step4 START (dashboard)")
    from dashboard import load_all_startups, render_dashboard
    records = load_all_startups()
    if not records:
        logger.warning("Step4: no startup records in Firebase — skipping dashboard regen")
        return
    html = render_dashboard(records)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    duration = (datetime.datetime.now() - start).total_seconds()
    logger.info("Step4 DONE: %.1fs | %s (%d records) — commit & push to publish",
                duration, out_path, len(records))


def _has_unprocessed(tab_name: str) -> bool:
    ws = get_sheet(tab_name)
    rows = ws.get_all_values()
    return any(len(r) <= 6 or r[6].strip().lower() in ("false", "") for r in rows[1:])


def _count_error_rows(tab_name: str) -> int:
    ws = get_sheet(tab_name)
    rows = ws.get_all_values()
    return sum(1 for r in rows[1:] if len(r) > 6 and r[6].strip().lower() == "error")


# Gemini 額度用完的旗標 (_gemini_exhausted，見 ai_processor.py) 是 process 內的全域變數，
# 一旦 429 過一次，同一個 process 裡後面全部改用 Qwen —— 這在本機沒差（Qwen 一直都在），
# 但 CI 環境沒有 Ollama，同一個 process 裡重試沒有意義。額度通常幾分鐘內會回補，所以
# 用「開一個全新 process」的方式重試，讓 Gemini 額度旗標重置，而不是在原 process 裡空轉。
STEP2_MAX_ATTEMPTS = 6
STEP2_RETRY_WAIT_S = 90

def daily_run(send_email: bool = True):
    start = datetime.datetime.now()
    logger.info("dailyRun START: %s", start.isoformat())
    tab_name = step1_scrape()
    logger.info("--- Step 1 done, tab: %s ---", tab_name)

    for attempt in range(1, STEP2_MAX_ATTEMPTS + 1):
        if attempt == 1:
            step2_ai(tab_name)
        else:
            logger.warning("Step2 attempt %d/%d: rows still unprocessed (likely Gemini quota) — "
                           "retrying in a fresh process so the quota flag resets",
                           attempt, STEP2_MAX_ATTEMPTS)
            subprocess.run([sys.executable, __file__, "--step2", tab_name], check=False)
        if not _has_unprocessed(tab_name):
            logger.info("Step2: all rows processed after %d attempt(s)", attempt)
            break
        if attempt < STEP2_MAX_ATTEMPTS:
            time.sleep(STEP2_RETRY_WAIT_S)
    else:
        logger.warning("Step2: exhausted %d attempts — some rows may remain unprocessed this week",
                       STEP2_MAX_ATTEMPTS)

    error_count = _count_error_rows(tab_name)
    if error_count:
        logger.error(
            "⚠️  Step2: %d article(s) marked 'error' this week (tab: %s) — "
            "skipped, NOT scored, NOT saved to Firebase. Needs manual review/rerun.",
            error_count, tab_name,
        )

    step3_report(tab_name, send_email=send_email)
    step4_dashboard()
    duration = (datetime.datetime.now() - start).total_seconds()
    logger.info("dailyRun DONE: %.1fs", duration)

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    tab  = sys.argv[2] if len(sys.argv) > 2 else None
    if mode == "--step1":
        step1_scrape(tab)
    elif mode == "--step2":
        step2_ai(tab)
    elif mode == "--step3":
        step3_report(tab, send_email="--email" in sys.argv)
    elif mode == "--step4":
        step4_dashboard()
    else:
        daily_run(send_email="--email" in sys.argv)
