import sys
import logging
import datetime
from scraper import run_all_scrapers
from ai_processor import process_raw_articles_by_region, get_sheet

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

REGIONS = ["台灣", "中國", "東南亞", "全球"]

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
    if tab_name is None:
        tab_name = "raw_" + datetime.datetime.now().strftime("%Y-%m-%d")
    for region in REGIONS:
        try:
            logger.info("Processing region: %s", region)
            process_raw_articles_by_region(region, tab_name)
        except Exception as e:
            logger.error("region %s error: %s", region, e)
    duration = (datetime.datetime.now() - start).total_seconds()
    logger.info("Step2 DONE: %.1fs", duration)

def daily_run():
    start = datetime.datetime.now()
    logger.info("dailyRun START: %s", start.isoformat())
    # Step 1: Scraper -> Google Sheets only
    tab_name = step1_scrape()
    logger.info("--- Step 1 done, tab: %s ---", tab_name)
    # Step 2: AI -> Firebase (can be run separately)
    step2_ai(tab_name)
    duration = (datetime.datetime.now() - start).total_seconds()
    logger.info("dailyRun DONE: %.1fs", duration)

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    tab = sys.argv[2] if len(sys.argv) > 2 else None
    if mode == "--step1":
        step1_scrape(tab)
    elif mode == "--step2":
        step2_ai(tab)
    else:
        daily_run()
