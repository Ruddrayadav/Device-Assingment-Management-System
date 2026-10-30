import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

data_env = os.environ.get("HHD_DATA_DIR")
DATA_DIR = Path(data_env).resolve() if data_env else BASE_DIR / "data"

backup_env = os.environ.get("HHD_BACKUP_DIR")
BACKUP_DIR = Path(backup_env).resolve() if backup_env else BASE_DIR / "backups"

LOGS_DIR = BASE_DIR / "logs"

DATA_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "hhd_system.db"
DATABASE_URL = f"sqlite:///{DB_PATH}"

MAX_UPLOAD_MB = int(os.environ.get("HHD_MAX_UPLOAD_MB", "512"))

def get_secret_key() -> str:
    env_secret = os.environ.get("HHD_SECRET_KEY")
    if env_secret:
        return env_secret.strip()
    key_file = DATA_DIR / ".secret_key"
    if key_file.exists():
        try:
            stored = key_file.read_text(encoding="utf-8").strip()
            if stored:
                return stored
        except OSError:
            pass
    new_secret = secrets.token_hex(32)
    try:
        key_file.write_text(new_secret, encoding="utf-8")
    except OSError:
        pass
    return new_secret

SECRET_KEY = get_secret_key()

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

