import os
import re
import time
import secrets
import sqlite3
import threading
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
    if "_auto" in filename:
        return "Automatic"
    elif filename.startswith("uploaded_"):
        return "Uploaded"
    elif "_pre_" in filename or filename.startswith("pre_"):
        return "Safety copy"
    elif filename.startswith("hhd_backup_"):
        return "Snapshot"
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

def apply_backup_retention(backup_dir: Optional[Path] = None) -> Dict[str, Any]:
    """
    Applies backup retention policy:
    1. 'auto' snapshots (e.g. hhd_backup_*_auto.db):
       - Keep newest 14 snapshots.
       - Plus up to 4 weekly historical snapshots from distinct ISO weeks beyond the newest 14.
       - Delete older/excess 'auto' snapshots.
    2. 'pre_*' safety copies (e.g. *_pre_* or pre_* including pre_migration_upgrade, pre_restore, pre_replace, pre_initial_data_reset):
       - Keep newest 5.
       - Delete older/excess pre_* copies.
    3. NEVER delete:
       - uploaded_* files
       - admin manual snapshots (custom tags or manual backups created without _auto / _pre_)
    """
    target_dir = Path(backup_dir) if backup_dir else BACKUP_DIR
    if not target_dir.exists():
        return {"deleted": [], "deleted_auto": 0, "deleted_pre": 0}

    deleted_files = []
    deleted_auto_count = 0
    deleted_pre_count = 0
    all_files = [f for f in target_dir.glob("*.db") if f.is_file()]

    auto_files = []
    pre_files = []

    for f in all_files:
        name = f.name.lower()
        if name.startswith("uploaded_"):
            # Never delete uploaded files
            continue
        if "_auto" in name:
            auto_files.append(f)
        elif "_pre_" in name or name.startswith("pre_"):
            pre_files.append(f)
        # Any manual snapshot without _auto or _pre_ is strictly preserved

    auto_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    pre_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)

    # Keep newest 5 pre_* copies
    for f in pre_files[5:]:
        try:
            f.unlink()
            deleted_files.append(f.name)
            deleted_pre_count += 1
        except Exception:
            pass

    # Keep newest 14 auto snapshots + 4 weekly ones from previous distinct ISO weeks
    auto_to_keep = set(auto_files[:14])
    older_auto = auto_files[14:]

    weekly_kept = set()
    weekly_weeks_seen = set()
    for f in older_auto:
        mtime = datetime.fromtimestamp(f.stat().st_mtime)
        year, week_num, _ = mtime.isocalendar()
        week_key = (year, week_num)
        if week_key not in weekly_weeks_seen and len(weekly_kept) < 4:
            weekly_weeks_seen.add(week_key)
            weekly_kept.add(f)

    keep_auto_all = auto_to_keep.union(weekly_kept)
    for f in older_auto:
        if f not in keep_auto_all:
            try:
                f.unlink()
                deleted_files.append(f.name)
                deleted_auto_count += 1
            except Exception:
                pass

    return {
        "deleted": deleted_files,
        "deleted_auto": deleted_auto_count,
        "deleted_pre": deleted_pre_count
    }

def get_last_auto_backup_info(backup_dir: Optional[Path] = None) -> Dict[str, Any]:
    """
    Finds the most recent automatic backup and calculates whether it is older than 26 hours.
    """
    target_dir = Path(backup_dir) if backup_dir else BACKUP_DIR
    if not target_dir.exists():
        return {"last_backup_time_str": "Never", "last_backup_dt": None, "is_stale_26h": True, "filename": None, "age_hours": 999.0}

    auto_files = [f for f in target_dir.glob("*.db") if f.is_file() and "_auto" in f.name.lower()]
    if not auto_files:
        return {"last_backup_time_str": "Never", "last_backup_dt": None, "is_stale_26h": True, "filename": None, "age_hours": 999.0}

    auto_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    newest = auto_files[0]
    mtime = datetime.fromtimestamp(newest.stat().st_mtime)
    now = datetime.now()
    age_hours = (now - mtime).total_seconds() / 3600.0

    return {
        "last_backup_time_str": fmt_datetime(mtime),
        "last_backup_dt": mtime,
        "is_stale_26h": age_hours > 26.0,
        "filename": newest.name,
        "age_hours": round(age_hours, 1)
    }

class BackupScheduler:
    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_run_time: Optional[datetime] = None
        self._last_daily_date: Optional[str] = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="BackupSchedulerThread")
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def trigger_auto_backup(self) -> Optional[Dict[str, Any]]:
        """Creates an auto snapshot, applies retention, and logs audit."""
        from app.database import DB_PATH, SessionLocal
        from app.models import AuditLog, SystemSetting
        from app.config import now_kolkata

        if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
            return None

        db = SessionLocal()
        try:
            res = create_safe_backup("auto")
            apply_backup_retention()

            now = now_kolkata()
            setting = db.query(SystemSetting).filter(SystemSetting.key == "last_auto_backup_time").first()
            if not setting:
                setting = SystemSetting(key="last_auto_backup_time", value=now.isoformat(), description="Last Automatic Backup Timestamp")
                db.add(setting)
            else:
                setting.value = now.isoformat()
                setting.updated_at = now

            audit = AuditLog(
                timestamp=now,
                user_id=None,
                username="SYSTEM",
                role="system",
                action="AUTOMATIC_BACKUP",
                target_type="BACKUP",
                target_id=res["filename"],
                details=f"Automatic snapshot created: {res['filename']} ({res['size_str']}). Retention applied."
            )
            db.add(audit)
            db.commit()
            return res
        except Exception:
            db.rollback()
            return None
        finally:
            db.close()

    def _run_loop(self):
        from app.database import SessionLocal
        from app.models import SystemSetting
        from app.config import now_kolkata

        # Initial startup delay (10 seconds)
        self._stop_event.wait(10.0)

        while not self._stop_event.is_set():
            try:
                now = now_kolkata()
                today_str = now.strftime("%Y-%m-%d")

                # Get configured backup_time (default "02:00")
                backup_time_str = "02:00"
                try:
                    db = SessionLocal()
                    setting = db.query(SystemSetting).filter(SystemSetting.key == "backup_time").first()
                    if setting and setting.value:
                        backup_time_str = setting.value.strip()
                    db.close()
                except Exception:
                    pass

                should_run = False

                # 1. Daily scheduled backup at backup_time
                current_hm = now.strftime("%H:%M")
                if current_hm >= backup_time_str and self._last_daily_date != today_str:
                    should_run = True
                    self._last_daily_date = today_str

                # 2. Every 6 hours while app runs
                if self._last_run_time is None:
                    last_info = get_last_auto_backup_info()
                    if last_info["age_hours"] >= 6.0:
                        should_run = True
                elif (now - self._last_run_time).total_seconds() >= 6 * 3600:
                    should_run = True

                if should_run:
                    self.trigger_auto_backup()
                    self._last_run_time = now

            except Exception:
                pass

            self._stop_event.wait(30.0)

backup_scheduler = BackupScheduler()
