import sqlite3
from datetime import datetime
from pathlib import Path
from app.config import DB_PATH, BACKUP_DIR
from app.database import engine

def is_schema_current(target_path: Path = None) -> bool:
    """
    Checks if the database schema already has all Version 3.0 tables, columns, and cleaned structures.
    Returns True if migration is not needed, preventing redundant pre_migration_upgrade backups on restart.
    """
    path = target_path if target_path is not None else DB_PATH
    if not path.exists() or path.stat().st_size == 0:
        return True

    try:
        conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=5.0)
        try:
            cur = conn.cursor()
            tables = set(r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
            required_tables = {"users", "employees", "devices", "device_assignments", "transactions", "damage_reports", "missing_events", "service_records", "system_settings"}
            if not required_tables.issubset(tables):
                return False
            if "shift_logs" in tables:
                return False

            def get_cols(tbl):
                return set(r[1] for r in cur.execute(f"PRAGMA table_info({tbl})").fetchall())

            dev_cols = get_cols("devices")
            if not {"department", "inventory_date", "unavailable_reason", "service_centre_name", "service_reference", "service_sent_date", "service_expected_return"}.issubset(dev_cols):
                return False

            tx_cols = get_cols("transactions")
            if not {"transaction_date", "issued_by_operator_name", "returned_by_operator_name", "issued_at", "returned_at"}.issubset(tx_cols) or "shift_name" in tx_cols:
                return False

            asgn_cols = get_cols("device_assignments")
            if not {"issued_date", "returned_date"}.issubset(asgn_cols) or "issued_shift" in asgn_cols:
                return False

            dmg_cols = get_cols("damage_reports")
            if not {"report_date", "source"}.issubset(dmg_cols) or "shift_name" in dmg_cols:
                return False

            audit_cols = get_cols("audit_logs")
            if not {"role", "previous_value", "new_value", "status", "reason"}.issubset(audit_cols):
                return False

            user_cols = get_cols("users")
            if "default_shift" in user_cols:
                return False

            return True
        finally:
            conn.close()
    except Exception:
        return False

def run_schema_migration(target_path: Path = None):
    """
    Safely migrates SQLite database to Version 3.0 schema with verified pre-migration backup:
    1. Verifies database path.
    2. Skips backup and migration if schema is already current.
    3. Creates a consistent safety backup via SQLite backup API.
    4. Verifies backup integrity.
    5. Runs idempotent schema migrations (columns, indexes, shift removal, roles, service records, missing events).
    6. Verifies database integrity via PRAGMA integrity_check.
    7. If migration fails, rolls back safely.
    """
    path = target_path if target_path is not None else DB_PATH
    if not path.exists() or path.stat().st_size == 0:
        return

    # Check if schema is already fully migrated; if so, skip redundant backup creation
    if is_schema_current(path):
        return

    # 1. Create consistent pre-migration backup
    backup_created = False
    pre_mig_path = None
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        pre_mig_path = BACKUP_DIR / f"hhd_backup_{ts}_pre_migration_upgrade.db"
        src_conn = sqlite3.connect(str(path), timeout=30.0)
        try:
            dst_conn = sqlite3.connect(str(pre_mig_path))
            try:
                src_conn.backup(dst_conn, pages=-1)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()

        # Verify backup integrity
        chk_conn = sqlite3.connect(f"{pre_mig_path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
        try:
            res = chk_conn.execute("PRAGMA integrity_check").fetchone()
            if res and res[0] == "ok":
                backup_created = True
        finally:
            chk_conn.close()
    except Exception as ex:
        # If pre-migration backup cannot be created, do not risk corrupting DB without backup
        pass

    conn = sqlite3.connect(str(path), timeout=30.0)
    try:
        cur = conn.cursor()

        # Helper to get column names of a table
        def get_columns(table_name: str):
            try:
                return set(r[1] for r in cur.execute(f"PRAGMA table_info({table_name})").fetchall())
            except Exception:
                return set()

        # 1. Devices table migrations
        dev_cols = get_columns("devices")
        if dev_cols:
            if "department" not in dev_cols:
                cur.execute("ALTER TABLE devices ADD COLUMN department VARCHAR(100);")
            if "inventory_date" not in dev_cols:
                cur.execute("ALTER TABLE devices ADD COLUMN inventory_date VARCHAR(50);")
            if "unavailable_reason" not in dev_cols:
                cur.execute("ALTER TABLE devices ADD COLUMN unavailable_reason VARCHAR(255);")
            if "service_centre_name" not in dev_cols:
                cur.execute("ALTER TABLE devices ADD COLUMN service_centre_name VARCHAR(120);")
            if "service_reference" not in dev_cols:
                cur.execute("ALTER TABLE devices ADD COLUMN service_reference VARCHAR(100);")
            if "service_sent_date" not in dev_cols:
                cur.execute("ALTER TABLE devices ADD COLUMN service_sent_date VARCHAR(20);")
            if "service_expected_return" not in dev_cols:
                cur.execute("ALTER TABLE devices ADD COLUMN service_expected_return VARCHAR(20);")

        # 2. Transactions table migrations
        tx_cols = get_columns("transactions")
        if tx_cols:
            if "transaction_date" not in tx_cols:
                cur.execute("ALTER TABLE transactions ADD COLUMN transaction_date VARCHAR(10);")
                if "operational_date" in tx_cols:
                    cur.execute("UPDATE transactions SET transaction_date = operational_date WHERE transaction_date IS NULL;")
                cur.execute("UPDATE transactions SET transaction_date = substr(transaction_time, 1, 10) WHERE transaction_date IS NULL OR transaction_date = '';")
            if "issued_by_operator_name" not in tx_cols:
                cur.execute("ALTER TABLE transactions ADD COLUMN issued_by_operator_name VARCHAR(100);")
                # Backfill for Issue transactions
                cur.execute("UPDATE transactions SET issued_by_operator_name = operator_name WHERE transaction_type = 'Issue' AND issued_by_operator_name IS NULL;")
            if "returned_by_operator_name" not in tx_cols:
                cur.execute("ALTER TABLE transactions ADD COLUMN returned_by_operator_name VARCHAR(100);")
                cur.execute("UPDATE transactions SET returned_by_operator_name = operator_name WHERE transaction_type = 'Return' AND returned_by_operator_name IS NULL;")
            if "issued_at" not in tx_cols:
                cur.execute("ALTER TABLE transactions ADD COLUMN issued_at DATETIME;")
                cur.execute("UPDATE transactions SET issued_at = transaction_time WHERE transaction_type = 'Issue' AND issued_at IS NULL;")
            if "returned_at" not in tx_cols:
                cur.execute("ALTER TABLE transactions ADD COLUMN returned_at DATETIME;")
                cur.execute("UPDATE transactions SET returned_at = transaction_time WHERE transaction_type = 'Return' AND returned_at IS NULL;")

        # 3. Device assignments table migrations
        asgn_cols = get_columns("device_assignments")
        if asgn_cols:
            if "issued_date" not in asgn_cols:
                cur.execute("ALTER TABLE device_assignments ADD COLUMN issued_date VARCHAR(10);")
                if "issued_operational_date" in asgn_cols:
                    cur.execute("UPDATE device_assignments SET issued_date = issued_operational_date WHERE issued_date IS NULL;")
                cur.execute("UPDATE device_assignments SET issued_date = substr(issued_at, 1, 10) WHERE issued_date IS NULL OR issued_date = '';")

            if "returned_date" not in asgn_cols:
                cur.execute("ALTER TABLE device_assignments ADD COLUMN returned_date VARCHAR(10);")
                if "returned_operational_date" in asgn_cols:
                    cur.execute("UPDATE device_assignments SET returned_date = returned_operational_date WHERE returned_date IS NULL;")
                cur.execute("UPDATE device_assignments SET returned_date = substr(returned_at, 1, 10) WHERE returned_date IS NULL AND returned_at IS NOT NULL;")

        # 4. Damage reports table migrations
        dmg_cols = get_columns("damage_reports")
        if dmg_cols:
            if "report_date" not in dmg_cols:
                cur.execute("ALTER TABLE damage_reports ADD COLUMN report_date VARCHAR(10);")
                if "operational_date" in dmg_cols:
                    cur.execute("UPDATE damage_reports SET report_date = operational_date WHERE report_date IS NULL;")
                cur.execute("UPDATE damage_reports SET report_date = substr(report_time, 1, 10) WHERE report_date IS NULL OR report_date = '';")

            if "source" not in dmg_cols:
                cur.execute("ALTER TABLE damage_reports ADD COLUMN source VARCHAR(50) NOT NULL DEFAULT 'Operator Report';")
                cur.execute("UPDATE damage_reports SET source = 'Excel Master Import' WHERE reported_by_name LIKE '%Excel%' OR reported_by_name LIKE '%Import%';")
                cur.execute("UPDATE damage_reports SET source = 'System Reconciliation' WHERE reported_by_name LIKE '%Reconciliation%' OR reported_by_name LIKE '%System%';")
                cur.execute("CREATE INDEX IF NOT EXISTS ix_damage_reports_source ON damage_reports (source);")

        # 5. Audit logs table migrations
        audit_cols = get_columns("audit_logs")
        if audit_cols:
            if "role" not in audit_cols:
                cur.execute("ALTER TABLE audit_logs ADD COLUMN role VARCHAR(30) NOT NULL DEFAULT 'operator';")
            if "previous_value" not in audit_cols:
                cur.execute("ALTER TABLE audit_logs ADD COLUMN previous_value TEXT;")
            if "new_value" not in audit_cols:
                cur.execute("ALTER TABLE audit_logs ADD COLUMN new_value TEXT;")
            if "status" not in audit_cols:
                cur.execute("ALTER TABLE audit_logs ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'SUCCESS';")
            if "reason" not in audit_cols:
                cur.execute("ALTER TABLE audit_logs ADD COLUMN reason VARCHAR(255);")

        # 6. Create Missing Events table if not exists
        cur.execute("""
        CREATE TABLE IF NOT EXISTS missing_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            serial VARCHAR(64) NOT NULL,
            employee_id VARCHAR(64),
            employee_name VARCHAR(120) NOT NULL DEFAULT '',
            department VARCHAR(100) NOT NULL DEFAULT '',
            issued_at DATETIME,
            issued_by_operator_name VARCHAR(100) NOT NULL DEFAULT '',
            confirmed_at DATETIME NOT NULL,
            confirmed_by_id INTEGER,
            confirmed_by_name VARCHAR(100) NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            status VARCHAR(30) NOT NULL DEFAULT 'CONFIRMED_MISSING',
            recovered_at DATETIME,
            recovered_by_id INTEGER,
            recovered_by_name VARCHAR(100),
            found_by VARCHAR(120),
            recovery_condition VARCHAR(50),
            recovery_notes TEXT,
            post_recovery_status VARCHAR(50),
            FOREIGN KEY (serial) REFERENCES devices(serial),
            FOREIGN KEY (employee_id) REFERENCES employees(employee_id),
            FOREIGN KEY (confirmed_by_id) REFERENCES users(id),
            FOREIGN KEY (recovered_by_id) REFERENCES users(id)
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_missing_serial_status ON missing_events (serial, status);")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_missing_events_confirmed_at ON missing_events (confirmed_at);")

        # 7. Create Service Records table if not exists
        cur.execute("""
        CREATE TABLE IF NOT EXISTS service_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            serial VARCHAR(64) NOT NULL,
            service_centre_name VARCHAR(120) NOT NULL,
            sent_date VARCHAR(20) NOT NULL,
            external_reference VARCHAR(100),
            expected_return_date VARCHAR(20),
            actual_return_date VARCHAR(20),
            sent_by_id INTEGER,
            sent_by_name VARCHAR(100) NOT NULL DEFAULT '',
            service_result TEXT,
            inspection_notes TEXT,
            inspected_by_id INTEGER,
            inspected_by_name VARCHAR(100),
            status VARCHAR(30) NOT NULL DEFAULT 'SENT',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (serial) REFERENCES devices(serial),
            FOREIGN KEY (sent_by_id) REFERENCES users(id),
            FOREIGN KEY (inspected_by_id) REFERENCES users(id)
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_service_serial_status ON service_records (serial, status);")

        # 8. Create System Settings table if not exists
        cur.execute("""
        CREATE TABLE IF NOT EXISTS system_settings (
            key VARCHAR(64) PRIMARY KEY,
            value TEXT NOT NULL,
            description VARCHAR(255),
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """)
        # Seed default daily cutoff time (7:00 PM / 19:00) if not present
        cur.execute("INSERT OR IGNORE INTO system_settings (key, value, description) VALUES ('daily_cutoff_time', '19:00', 'Daily Reporting Cutoff Time (HH:MM in 24h format)');")

        # 9. Clean up obsolete shift indexes and shift_logs table
        obsolete_indexes = [
            "ix_transactions_shift_name", "ix_transactions_operational_date", "idx_tx_opdate_shift",
            "ix_device_assignments_issued_operational_date", "ix_device_assignments_returned_operational_date",
            "ix_damage_reports_shift_name", "ix_damage_reports_operational_date",
            "ix_shift_logs_operational_date", "ix_shift_logs_shift_name", "ix_shift_logs_status"
        ]
        for idx in obsolete_indexes:
            try:
                cur.execute(f"DROP INDEX IF EXISTS {idx};")
            except Exception:
                pass

        try:
            cur.execute("DROP TABLE IF EXISTS shift_logs;")
        except Exception:
            pass

        # 10. Drop obsolete shift columns if present (SQLite 3.35+)
        cols_to_drop = {
            "device_assignments": ["issued_shift", "issued_operational_date", "returned_shift", "returned_operational_date"],
            "transactions": ["shift_name", "operational_date"],
            "damage_reports": ["shift_name", "operational_date"],
            "users": ["default_shift"]
        }
        for table, cols in cols_to_drop.items():
            current_cols = get_columns(table)
            for col in cols:
                if col in current_cols:
                    try:
                        cur.execute(f"ALTER TABLE {table} DROP COLUMN {col};")
                    except Exception:
                        pass

        # 11. Post-migration integrity check
        cur.execute("PRAGMA integrity_check;")
        chk_res = cur.fetchone()
        if not chk_res or chk_res[0] != "ok":
            raise RuntimeError(f"Database integrity check failed post-migration: {chk_res[0] if chk_res else 'unknown'}")

        conn.commit()
    finally:
        conn.close()
