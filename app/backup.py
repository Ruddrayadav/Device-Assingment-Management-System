import os
import shutil
import sqlite3
from datetime import datetime
from typing import List, Dict, Any, Optional
from app.config import BACKUP_DIR, DB_PATH
from app.database import engine, SCAN_WRITE_LOCK

def create_safe_backup(custom_tag: str = "") -> Dict[str, Any]:
    """
    Creates an atomic, consistent SQLite backup using the official SQLite online backup API.
    Safe to run even while operators are actively scanning.
    """
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag_part = f"_{custom_tag}" if custom_tag else ""
    filename = f"hhd_backup_{timestamp}{tag_part}.db"
    dest_path = BACKUP_DIR / filename

    with SCAN_WRITE_LOCK:
        source_con = sqlite3.connect(str(DB_PATH), timeout=30.0)
        try:
            dest_con = sqlite3.connect(str(dest_path))
            try:
                # SQLite Online Backup API step-by-step
                source_con.backup(dest_con, pages=-1)
            finally:
                dest_con.close()
        finally:
            source_con.close()

    size_bytes = os.path.getsize(dest_path)
    return {
        "filename": filename,
        "path": str(dest_path),
        "size_bytes": size_bytes,
        "size_kb": round(size_bytes / 1024, 1),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

def list_backups() -> List[Dict[str, Any]]:
    """
    Returns list of existing backup files sorted by creation date descending.
    """
    os.makedirs(BACKUP_DIR, exist_ok=True)
    results = []
    for item in BACKUP_DIR.glob("*.db"):
        if item.is_file():
            stat = item.stat()
            results.append({
                "filename": item.name,
                "size_kb": round(stat.st_size / 1024, 1),
                "created_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "timestamp": stat.st_mtime
            })
    results.sort(key=lambda x: x["timestamp"], reverse=True)
    return results

def verify_backup_integrity(backup_path: str) -> bool:
    """
    Runs PRAGMA integrity_check on the backup file to ensure it is not corrupt.
    """
    try:
        con = sqlite3.connect(backup_path, timeout=10.0)
        cur = con.cursor()
        res = cur.execute("PRAGMA integrity_check").fetchone()
        con.close()
        return bool(res and res[0] == "ok")
    except Exception:
        return False

def restore_backup(filename: str) -> bool:
    """
    Restores the database from a verified backup file.
    """
    backup_file = BACKUP_DIR / filename
    if not backup_file.exists():
        raise FileNotFoundError(f"Backup file {filename} does not exist.")

    if not verify_backup_integrity(str(backup_file)):
        raise ValueError(f"Backup file {filename} failed integrity check.")

    with SCAN_WRITE_LOCK:
        # Dispose engine connections
        engine.dispose()

        # Create a pre-restore safety copy of current db if it exists
        if DB_PATH.exists():
            pre_restore_copy = BACKUP_DIR / f"pre_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
            shutil.copy2(DB_PATH, pre_restore_copy)

        # Restore from backup file
        dest_con = sqlite3.connect(str(DB_PATH), timeout=30.0)
        source_con = sqlite3.connect(str(backup_file))
        try:
            source_con.backup(dest_con, pages=-1)
        finally:
            source_con.close()
            dest_con.close()

    return True

