import os
import secrets
from pathlib import Path
from datetime import datetime, date

try:
    from zoneinfo import ZoneInfo
    TIMEZONE_KOLKATA = ZoneInfo("Asia/Kolkata")
except Exception:
    from datetime import timezone, timedelta
    TIMEZONE_KOLKATA = timezone(timedelta(hours=5, minutes=30))

BASE_DIR = Path(__file__).resolve().parent.parent

data_env = os.environ.get("HHD_DATA_DIR")
DATA_DIR = Path(data_env).resolve() if data_env else BASE_DIR / "data"

backup_env = os.environ.get("HHD_BACKUP_DIR")
BACKUP_DIR = Path(backup_env).resolve() if backup_env else BASE_DIR / "backups"

LOGS_DIR = BASE_DIR / "logs"

DATA_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = (DATA_DIR / "hhd_system.db").resolve()
DATABASE_URL = f"sqlite:///{DB_PATH}"

MAX_UPLOAD_MB = int(os.environ.get("HHD_MAX_UPLOAD_MB", "512"))
DEFAULT_REPORT_DAYS = int(os.environ.get("HHD_DEFAULT_REPORT_DAYS", "30"))

def get_secret_key() -> str:
    env_secret = os.environ.get("HHD_SECRET_KEY")
    if env_secret:
        return env_secret.strip()
    key_file = DATA_DIR / ".secret_key"
    if key_file.exists():
        try:
            stored = key_file.read_text(encoding="utf-8").strip()
            if stored and len(stored) >= 32:
                return stored
        except OSError:
            pass
    new_secret = secrets.token_hex(32)
    try:
        key_file.write_text(new_secret, encoding="utf-8")
    except OSError:
        pass
    return new_secret

def rotate_secret_key() -> str:
    """Rotates application secret key securely."""
    new_secret = secrets.token_hex(32)
    key_file = DATA_DIR / ".secret_key"
    try:
        key_file.write_text(new_secret, encoding="utf-8")
    except OSError:
        pass
    return new_secret

SECRET_KEY = get_secret_key()

def now_kolkata() -> datetime:
    """Returns current naive or localized datetime in Asia/Kolkata timezone."""
    return datetime.now(TIMEZONE_KOLKATA).replace(tzinfo=None)

def today_kolkata_str() -> str:
    """Returns current calendar date string (YYYY-MM-DD) in Asia/Kolkata timezone."""
    return datetime.now(TIMEZONE_KOLKATA).strftime("%Y-%m-%d")

SERVER_HOST = os.environ.get("HHD_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("HHD_PORT", "8000"))

DEVICE_STATUSES = [
    "Available",
    "Issued",
    "Damaged",
    "Missing",
    "Under Repair"
]

DAMAGE_CATEGORIES = [
    "Blank screen",
    "Not powering on",
    "Scanner not working",
    "Broken screen",
    "Battery issue",
    "Physical damage",
    "Other"
]


