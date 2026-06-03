# scheduler.py
#
# Runs the PhilGEPS scraper automatically every Monday–Friday at 9:00 AM.
#
# Setup:
#   pip install schedule
#   python scheduler.py          ← keep this terminal open (or run in background)
#
# To run once immediately for testing:
#   python philgeps_scrape.py --auto --headless

import schedule
import time
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent / "philgeps_scrape.py"
PYTHON = sys.executable  # uses the same venv/interpreter


def run_scraper():
    print("\n[Scheduler] Starting PhilGEPS scrape...")
    result = subprocess.run(
        [PYTHON, str(SCRIPT), "--auto", "--headless"]
    )
    print(f"[Scheduler] Scrape finished (exit code {result.returncode}).\n")


# Schedule Mon–Fri at 09:00
for day in [
    schedule.every().monday,
    schedule.every().tuesday,
    schedule.every().wednesday,
    schedule.every().thursday,
    schedule.every().friday,
]:
    day.at("09:00").do(run_scraper)

print("PhilGEPS scheduler is running.")
print("Scrapes will fire Monday–Friday at 9:00 AM.")
print("Keep this terminal open. Press Ctrl+C to stop.\n")

while True:
    schedule.run_pending()
    time.sleep(30)
