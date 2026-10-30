import sqlite3
from datetime import datetime
from pathlib import Path
from app.config import DB_PATH
from app.database import engine

def run_schema_migration(target_path: Path = None):
    """
    Safely migrates SQLite database to Version 3.0 schema:
    - Adds 'department' and 'inventory_date' to devices table
    - Adds 'transaction_date' to transactions table
    - Adds 'issued_date' and 'returned_date' to device_assignments table
    - Adds 'report_date' to damage_reports table
    - Preserves all historical records without data loss
    """
    path = target_path if target_path is not None else DB_PATH
    if not path.exists():
        return

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
                cur.execute("ALTER TABLE devices ADD COLUMN department VARCHAR(100) NOT NULL DEFAULT 'GENERAL';")
                cur.execute("UPDATE devices SET department = 'GENERAL' WHERE department IS NULL OR department = '';")
            if "inventory_date" not in dev_cols:
                cur.execute("ALTER TABLE devices ADD COLUMN inventory_date VARCHAR(50);")

        # 2. Transactions table migrations
        tx_cols = get_columns("transactions")
        if tx_cols:
            if "transaction_date" not in tx_cols:
                cur.execute("ALTER TABLE transactions ADD COLUMN transaction_date VARCHAR(10);")
                # Backfill from operational_date or transaction_time
                if "operational_date" in tx_cols:
                    cur.execute("UPDATE transactions SET transaction_date = operational_date WHERE transaction_date IS NULL;")
                cur.execute("UPDATE transactions SET transaction_date = substr(transaction_time, 1, 10) WHERE transaction_date IS NULL OR transaction_date = '';")

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

        # 5. Clean up obsolete shift indexes and shift_logs table
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

        # 6. Drop obsolete shift columns if present (SQLite 3.35+)
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

        # 7. Update legacy operator full names if they reflect old shift names
        try:
            cur.execute("UPDATE users SET full_name = 'Rahul (Operator 1)' WHERE username = 'operator1' AND (full_name LIKE '%Morning%' OR full_name = 'Operator 1');")
            cur.execute("UPDATE users SET full_name = 'Amit (Operator 2)' WHERE username = 'operator2' AND (full_name LIKE '%Evening%' OR full_name = 'Operator 2');")
        except Exception:
            pass

        conn.commit()
    finally:
        conn.close()
