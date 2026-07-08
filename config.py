import os

BASE_URL = "https://telemetr.me"
CATALOG_URL = "/catalog"

DATA_RAW_DIR = os.path.join("data", "raw")
DATA_PROCESSED_DIR = os.path.join("data", "processed")
DATA_FINAL_DIR = os.path.join("data", "final")

LOG_PATH = "parser.log"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; study-project/0.1)",
    "Accept-Language": "ru-RU,ru;q=0.9"
}

TIMEOUT = 15
PAUSE_SEC = 2.5