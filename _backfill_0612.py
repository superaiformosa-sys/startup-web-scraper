import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
from main import step2_ai

step2_ai(tab_name="raw_2026-06-12")
print("BACKFILL_DONE")
