import os

DATA_DIR = os.environ.get("SBD_DATA_DIR", "/tmp/sbd")
DB_PATH = os.path.join(DATA_DIR, "sbd.sqlite3")

# Optional URL of a Selenium grid (e.g. http://selenium:4444). Without it, councils that need a
# real browser are reported as unsupported instead of failing slowly.
SELENIUM_URL = os.environ.get("SELENIUM_URL", "").strip()

# Optional shared token. When set, every endpoint except "/" and "/health" needs header X-SBD-Token.
API_TOKEN = os.environ.get("SBD_API_TOKEN", "").strip()

FRESH_TTL = int(os.environ.get("SBD_FRESH_TTL", 24 * 3600))              # plain HTTP scrapers
FRESH_TTL_BROWSER = int(os.environ.get("SBD_FRESH_TTL_BROWSER", 72 * 3600))  # Selenium scrapers
STALE_MAX = int(os.environ.get("SBD_STALE_MAX", 14 * 24 * 3600))         # serve stale up to this age
SUBSCRIPTION_DAYS = int(os.environ.get("SBD_SUBSCRIPTION_DAYS", 30))     # stop refreshing if nobody asked
MODULE_GAP = float(os.environ.get("SBD_MODULE_GAP", 5))                  # min seconds between scrapes of one council
MAX_PARALLEL = int(os.environ.get("SBD_MAX_PARALLEL", 2))                # simultaneous scrapes
SCRAPE_TIMEOUT = int(os.environ.get("SBD_SCRAPE_TIMEOUT", 120))
SYNC_WAIT = int(os.environ.get("SBD_SYNC_WAIT", 100))                    # how long a visitor request may wait for a slot
SCHEDULER_TICK = int(os.environ.get("SBD_SCHEDULER_TICK", 600))
REFRESH_BATCH = int(os.environ.get("SBD_REFRESH_BATCH", 5))
MAX_FAILS = int(os.environ.get("SBD_MAX_FAILS", 8))
ADDRESS_TTL = int(os.environ.get("SBD_ADDRESS_TTL", 30 * 24 * 3600))
POSTCODE_TTL = int(os.environ.get("SBD_POSTCODE_TTL", 90 * 24 * 3600))
RATE_LIMIT_PER_MIN = int(os.environ.get("SBD_RATE_LIMIT", 300))
ENABLE_UPRN_UK = os.environ.get("SBD_ENABLE_UPRN_UK", "") == "1"         # blocked from most cloud IPs
