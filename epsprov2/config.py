import os
import re


DB_FILE_NAME = "stocks.db"
STOCKS_FILE_NAME = "stocks.json"
CACHE_DIR_NAME = "yfinance_cache"

GRADE_A_MIN_SCORE = 7
SNIPER_MIN_MOS = 10

API_MAX_WORKERS = int(os.environ.get("MAX_FETCH_WORKERS", "12"))

TICKER_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")

SET100_SOURCE_PAGE_URL = "https://www.settrade.com/th/equities/market-data/overview?category=Index&index=SET100"
SET100_SOURCE_API_URL = "https://www.settrade.com/api/set/index/SET100/composition"
SET100_AUTO_SYNC_INTERVAL_SECONDS = int(os.environ.get("SET100_AUTO_SYNC_INTERVAL_SECONDS", "43200"))
SET100_MIN_EXPECTED_SYMBOLS = 80
REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0"}

SET100_TICKER_ALIASES = {
    "BANPUU": "BANPU",
}

THAIFIN_TICKER_ALIASES = {
    "BANPU": "BANPUU",
}

EPS_TREND_YEARS = 10
DIV_TREND_YEARS = 10

ENABLE_TRANSLATION = os.environ.get("ENABLE_TRANSLATION", "1") == "1"
TRANSLATION_MAX_CHARS = int(os.environ.get("TRANSLATION_MAX_CHARS", "4500"))


def get_cache_dir(is_vercel, cache_dir_name):
    if is_vercel:
        return os.path.join("/tmp", cache_dir_name)
    return cache_dir_name

