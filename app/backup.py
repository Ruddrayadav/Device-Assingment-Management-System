import os
import re
import time
import secrets
import sqlite3
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple

from app.config import BACKUP_DIR, DB_PATH, MAX_UPLOAD_MB
from app.database import engine, SCAN_WRITE_LOCK, Base
import app.models  # Ensure Base.metadata has all model definitions
from app.timefmt import fmt_datetime

CORE_TABLES = ["users", "employees", "devices", "device_assignments", "transactions"]

def format_bytes(size_bytes: int) -> str:
    """Formats bytes into a readable string (B, KB, MB)."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{round(size_bytes / 1024, 1):,} KB"
    else:
        return f"{round(size_bytes / (1024 * 1024), 2):,} MB"

def safe_backup_path(filename: str) -> Path:
    """
    Validates that filename is a plain filename ending in .db inside BACKUP_DIR.
    Rejects directory traversal ('..', slashes) and hidden files.
    """
    if not filename or not isinstance(filename, str):
        raise ValueError("Filename must be a non-empty string.")
    
    # Check for slashes, '..', and hidden files
    if "/" in filename or "\\" in filename or ".." in filename:
        raise ValueError("Directory traversal characters are not allowed.")
    if filename.startswith("."):
        raise ValueError("Hidden files are not allowed.")
    if not filename.lower().endswith(".db"):
        raise ValueError("Only .db files are permitted.")
    
    clean_name = os.path.basename(filename)
    if clean_name != filename:
        raise ValueError("Filename contains path separators.")
        
    target_path = (BACKUP_DIR / clean_name).resolve()
    # Ensure target_path resides directly within BACKUP_DIR
    if target_path.parent != BACKUP_DIR.resolve():
        raise ValueError("File must reside within the backup directory.")
        
    return target_path

def cleanup_old_part_uploads(max_age_seconds: int = 3600) -> int:
    """
    Removes leftover .upload_*.part files in BACKUP_DIR older than max_age_seconds (default 1 hour).
    """
    removed = 0
    now = time.time()
    if not BACKUP_DIR.exists():
        return 0
    for part_file in BACKUP_DIR.glob(".upload_*.part"):
        try:
            if part_file.is_file() and (now - part_file.stat().st_mtime > max_age_seconds):
                part_file.unlink()
                removed += 1
        except Exception:
            pass
    return removed

def inspect_db_file(path: Path) -> Dict[str, Any]:
    """
    Inspects a SQLite database file using mode=ro&immutable=1 URI.
    Verifies header magic number, runs PRAGMA integrity_check,
    verifies core tables and expected columns against Base.metadata,
    and returns record counts and last scan time.
    """
    if not path.exists():
        return {"valid": False, "error": "File does not exist.", "counts": {}, "last_scan_time": "-"}

    size = path.stat().st_size
    if size < 16:
        return {"valid": False, "error": "File too small for SQLite database.", "counts": {}, "last_scan_time": "-"}

    try:
        with open(path, "rb") as f:
            header = f.read(16)
            if header != b"SQLite format 3\x00":
                return {"valid": False, "error": "Invalid SQLite header.", "counts": {}, "last_scan_time": "-"}
    except Exception as ex:
        return {"valid": False, "error": f"Failed reading header: {str(ex)}", "counts": {}, "last_scan_time": "-"}

    uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=5.0)
        try:
            cur = con.cursor()
            # 1. Integrity check
            row = cur.execute("PRAGMA integrity_check").fetchone()
            if not row or row[0] != "ok":
                return {"valid": False, "error": f"Integrity check failed: {row[0] if row else 'unknown'}", "counts": {}, "last_scan_time": "-"}

            # 2. Schema check: verify core tables and columns
            existing_tables = set(r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
            for tbl in CORE_TABLES:
                if tbl not in existing_tables:
                    return {"valid": False, "error": f"Missing required table: {tbl}", "counts": {}, "last_scan_time": "-"}

                # Check columns
                meta_table = Base.metadata.tables.get(tbl)
                if meta_table is not None:
                    expected_cols = set(meta_table.columns.keys())
                    actual_cols = set(r[1] for r in cur.execute(f"PRAGMA table_info({tbl})").fetchall())
                    missing_cols = expected_cols - actual_cols
                    if missing_cols:
                        return {"valid": False, "error": f"Table '{tbl}' missing columns: {', '.join(sorted(missing_cols))}", "counts": {}, "last_scan_time": "-"}

            # 3. Counts and last scan time
            emp_count = cur.execute("SELECT COUNT(*) FROM employees").fetchone()[0]
            dev_count = cur.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
            tx_count = cur.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
            user_count = cur.execute("SELECT COUNT(*) FROM users").fetchone()[0]

            last_scan_row = cur.execute("SELECT MAX(transaction_time) FROM transactions").fetchone()
            last_scan_raw = last_scan_row[0] if last_scan_row and last_scan_row[0] else None
            last_scan_formatted = fmt_datetime(last_scan_raw) if last_scan_raw else "Never"

            return {
                "valid": True,
                "error": None,
                "counts": {
                    "employees": emp_count,
                    "devices": dev_count,
                    "transactions": tx_count,
                    "users": user_count
                },
                "last_scan_time": last_scan_formatted
            }
        finally:
            con.close()
    except Exception as ex:
        return {"valid": False, "error": f"SQLite error: {str(ex)}", "counts": {}, "last_scan_time": "-"}

def verify_backup_integrity(backup_path: str) -> bool:
    """Runs PRAGMA integrity_check on the backup file to ensure it is not corrupt."""
    p = Path(backup_path)
    info = inspect_db_file(p)
    return info.get("valid", False)

def get_backup_type(filename: str) -> str:
    """Returns badge type based on naming convention."""
    if filename.startswith("hhd_backup_"):
        return "Snapshot"
    elif filename.startswith("uploaded_"):
        return "Uploaded"
    elif filename.startswith("pre_restore_"):
        return "Safety copy"
    return "Other"

def get_live_db_stats() -> Dict[str, Any]:
    """Returns live database file metadata and record counts."""
    if not DB_PATH.exists():
        return {
            "filename": DB_PATH.name,
            "exists": False,
            "size_str": "0 B",
            "counts": {"employees": 0, "devices": 0, "transactions": 0, "users": 0},
            "last_scan_time": "Never"
        }

    db_size = DB_PATH.stat().st_size
    wal_path = Path(str(DB_PATH) + "-wal")
    wal_size = wal_path.stat().st_size if wal_path.exists() else 0

    if wal_size > 0:
        size_display = f"{format_bytes(db_size)} (+ {format_bytes(wal_size)} WAL)"
    else:
        size_display = format_bytes(db_size)

    from app.database import SessionLocal
    from app.models import Employee, Device, Transaction, User
    from sqlalchemy import func

    db = SessionLocal()
    try:
        emp_count = db.query(func.count(Employee.employee_id)).scalar() or 0
        dev_count = db.query(func.count(Device.serial)).scalar() or 0
        tx_count = db.query(func.count(Transaction.id)).scalar() or 0
        user_count = db.query(func.count(User.id)).scalar() or 0
        last_tx = db.query(func.max(Transaction.transaction_time)).scalar()
        last_scan = fmt_datetime(last_tx) if last_tx else "No scans yet"
    except Exception:
        emp_count = dev_count = tx_count = user_count = 0
        last_scan = "Unknown"
    finally:
        db.close()

    return {
        "filename": DB_PATH.name,
        "exists": True,
        "size_str": size_display,
        "counts": {
            "employees": emp_count,
            "devices": dev_count,
            "transactions": tx_count,
            "users": user_count
        },
        "last_scan_time": last_scan
    }

def create_safe_backup(custom_tag: str = "") -> Dict[str, Any]:
    """
    Creates an atomic, consistent SQLite backup using the official SQLite online backup API.
    Safe to run even while operators are actively scanning.
    """
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    sanitized_tag = re.sub(r'[^a-zA-Z0-9_\-]', '_', custom_tag.strip()) if custom_tag else ""
    tag_part = f"_{sanitized_tag}" if sanitized_tag else ""
    filename = f"hhd_backup_{timestamp}{tag_part}.db"
    dest_path = BACKUP_DIR / filename

    with SCAN_WRITE_LOCK:
        source_con = sqlite3.connect(str(DB_PATH), timeout=30.0)
        try:
            dest_con = sqlite3.connect(str(dest_path))
            try:
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
        "size_str": format_bytes(size_bytes),
        "size_kb": round(size_bytes / 1024, 1),
        "created_at": fmt_datetime(datetime.now())
    }

def list_backups() -> List[Dict[str, Any]]:
    """
    Returns list of all .db backup files in BACKUP_DIR, inspected for validity,
    content counts, size, type badge, and modification timestamp.
    """
    os.makedirs(BACKUP_DIR, exist_ok=True)
    cleanup_old_part_uploads()
    results = []
    for item in BACKUP_DIR.glob("*.db"):
        if item.is_file():
            stat = item.stat()
            btype = get_backup_type(item.name)
            info = inspect_db_file(item)
            results.append({
                "filename": item.name,
                "type": btype,
                "size_bytes": stat.st_size,
                "size_str": format_bytes(stat.st_size),
                "size_kb": round(stat.st_size / 1024, 1),
                "saved_at": fmt_datetime(datetime.fromtimestamp(stat.st_mtime)),
                "created_at": fmt_datetime(datetime.fromtimestamp(stat.st_mtime)),
                "timestamp": stat.st_mtime,
                "valid": info.get("valid", False),
                "error": info.get("error"),
                "counts": info.get("counts", {}),
                "last_scan_time": info.get("last_scan_time", "-")
            })
    results.sort(key=lambda x: x["timestamp"], reverse=True)
    return results

def restore_backup(filename: str) -> bool:
    """
    Restores database from a verified backup file.
    Creates a pre-restore safety copy using the SQLite backup API first,
    restores the target backup, and runs Base.metadata.create_all.
    """
    target_file = safe_backup_path(filename)
    if not target_file.exists():
        raise FileNotFoundError(f"Backup file {filename} does not exist.")

    info = inspect_db_file(target_file)
    if not info.get("valid"):
        raise ValueError(f"Backup file {filename} failed validation: {info.get('error')}")

    with SCAN_WRITE_LOCK:
        # Dispose engine pool connections
        engine.dispose()

        # Create pre-restore safety copy of current db if it exists using SQLite backup API
        if DB_PATH.exists():
            pre_restore_filename = f"pre_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
            pre_restore_path = BACKUP_DIR / pre_restore_filename
            src_safety = sqlite3.connect(str(DB_PATH), timeout=30.0)
            try:
                dst_safety = sqlite3.connect(str(pre_restore_path))
                try:
                    src_safety.backup(dst_safety, pages=-1)
                finally:
                    dst_safety.close()
            finally:
                src_safety.close()

        # Restore from backup file into DB_PATH using SQLite backup API
        dest_con = sqlite3.connect(str(DB_PATH), timeout=30.0)
        source_con = sqlite3.connect(str(target_file))
        try:
            source_con.backup(dest_con, pages=-1)
        finally:
            source_con.close()
            dest_con.close()

        # Run Base.metadata.create_all after restoring
        Base.metadata.create_all(bind=engine)

    return True

def delete_backup(filename: str) -> bool:
    """Deletes a backup file safely using safe_backup_path."""
    target_file = safe_backup_path(filename)
    if not target_file.exists():
        raise FileNotFoundError(f"Backup file {filename} does not exist.")
    target_file.unlink()
    return True

def validate_uploaded_db(temp_path: Path) -> Tuple[bool, Optional[str]]:
    """
    Validates an uploaded database file:
    1. Size > 0
    2. Header begins with b"SQLite format 3\x00"
    3. PRAGMA integrity_check == "ok"
    4. Core tables and columns match Base.metadata
    """
    if not temp_path.exists():
        return False, "Temporary upload file is missing."

    size = temp_path.stat().st_size
    if size == 0:
        return False, "Uploaded file is empty (0 bytes)."

    if size < 16:
        return False, "File is too small to be a valid SQLite database."

    with open(temp_path, "rb") as f:
        header = f.read(16)
        if header != b"SQLite format 3\x00":
            return False, "File is not a valid SQLite database (missing SQLite header)."

    info = inspect_db_file(temp_path)
    if not info.get("valid"):
        return False, info.get("error", "Database validation failed.")

    return True, None
