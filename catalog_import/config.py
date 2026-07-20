import os
import sys
from pathlib import Path

APP_NAME = "Gestionnaire de catalogue"
APP_VERSION = "0.6.1"


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def _user_data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME


if _is_frozen():
    BASE_DIR = Path(sys.executable).resolve().parent
    DATA_DIR = _user_data_dir()
else:
    BASE_DIR = Path(__file__).resolve().parent.parent
    DATA_DIR = BASE_DIR

SESSION_DIR = DATA_DIR / ".session"
OUTPUT_DIR = DATA_DIR / "output"

if _is_frozen():
    ASSETS_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR)) / "assets"
else:
    ASSETS_DIR = BASE_DIR / "assets"

ICON_PNG = ASSETS_DIR / "app_icon.png"
ICON_ICNS = ASSETS_DIR / "app_icon.icns"

PFS_LOGIN_PAGE_URL = "https://parisfashionshops.com/fr/loginform"
PFS_OAUTH_URL = "https://client.parisfashionshops.com/api/v1/oauth/login?lang=fr"
PFS_API_BASE_URL = "https://wholesaler-api.parisfashionshops.com/api/v1"
PFS_LIST_PRODUCTS_URL = f"{PFS_API_BASE_URL}/catalog/listProducts"
PFS_LIST_VARIANTS_URL = f"{PFS_API_BASE_URL}/catalog/listVariants"
PFS_PRODUCT_URL = f"{PFS_API_BASE_URL}/catalog/products/{{product_id}}"
PFS_PRODUCT_VARIANTS_URL = f"{PFS_API_BASE_URL}/catalog/products/{{product_id}}/variants"
PFS_SITE_ORIGIN = "https://parisfashionshops.com"

EFASHION_API_URL = "https://wapi.efashion-paris.com/graphql"
EFASHION_REST_BASE_URL = "https://wapi.efashion-paris.com"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
BROWSER_IMPERSONATE = "chrome131"

DEFAULT_PER_PAGE = 100
DEFAULT_PRODUCT_STATUS = "ACTIVE"
DEFAULT_VARIANTS_STATUS = "READY_FOR_SALE"
DEFAULT_ENRICH_CONCURRENCY = 3
DEFAULT_FETCH_RETRIES = 4
DEFAULT_FETCH_RETRY_DELAY = 1.0
