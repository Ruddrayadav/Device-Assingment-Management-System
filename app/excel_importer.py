import io
import csv
import re
from datetime import datetime, date
from typing import List, Dict, Any, Tuple, Optional
from openpyxl import load_workbook
from sqlalchemy.orm import Session

from app.models import Device, Employee, DeviceAssignment, Transaction
from app.database import SCAN_WRITE_LOCK
from app.backup import create_safe_backup

def normalize_header(header: str) -> str:
    """Normalizes header string: removes spaces, punctuation, lowercase."""
    if not header:
        return ""
    return re.sub(r"[^a-zA-Z0-9]", "", str(header).lower())

def cell_to_str(val: Any) -> str:
    """Converts cell value to clean string while strictly preserving leading zeros and text."""
    if val is None:
        return ""
    if isinstance(val, float) and val.is_integer():
        return str(int(val)).strip()
    return str(val).strip()

# ---------------------------------------------------------------------------
# Device Inventory Parsing & Validation
# ---------------------------------------------------------------------------
DEVICE_HEADER_MAP = {
    "srno": "sr_no",
    "sr": "sr_no",
    "serialno": "serial",
    "serialnumber": "serial",
    "serial": "serial",
    "hhdserial": "serial",
    "make": "make",
    "modelno": "model",
    "model": "model",
    "department": "department",
    "dept": "department",
    "remarks": "remarks",
    "remark": "remarks",
    "condition": "remarks",
    "date": "inventory_date",
    "inventorydate": "inventory_date"
}

def parse_device_file(file_bytes: bytes, filename: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Parses .xlsx or .csv device inventory file into raw rows.
    Returns (rows_as_dicts, column_headers_found).
    """
    filename_lower = filename.lower()
    raw_rows = []
    headers = []

    if filename_lower.endswith(".xlsx"):
        wb = load_workbook(io.BytesIO(file_bytes), data_only=True)
        # Find sheet with data (prefer 'Device Inventory' or first sheet)
        ws = wb["Device Inventory"] if "Device Inventory" in wb.sheetnames else wb.active
        all_rows = list(ws.iter_rows(values_only=True))
        if not all_rows:
            return [], []

        # Find header row (first non-empty row)
        header_row_idx = 0
        for idx, row in enumerate(all_rows):
            if any(cell_to_str(c) for c in row):
                header_row_idx = idx
                headers = [cell_to_str(c) for c in row]
                break

        for r_idx in range(header_row_idx + 1, len(all_rows)):
            row = all_rows[r_idx]
            if not any(cell_to_str(c) for c in row):
                continue  # Skip empty rows
            row_dict = {}
            for h_idx, h_name in enumerate(headers):
                if h_idx < len(row):
                    row_dict[h_name] = cell_to_str(row[h_idx])
            row_dict["__line_no__"] = r_idx + 1
            raw_rows.append(row_dict)

    elif filename_lower.endswith(".csv"):
        text_stream = io.StringIO(file_bytes.decode("utf-8-sig", errors="replace"))
        reader = csv.reader(text_stream)
        all_rows = list(reader)
        if not all_rows:
            return [], []

        headers = [cell_to_str(c) for c in all_rows[0]]
        for r_idx in range(1, len(all_rows)):
            row = all_rows[r_idx]
            if not any(cell_to_str(c) for c in row):
                continue
            row_dict = {}
            for h_idx, h_name in enumerate(headers):
                if h_idx < len(row):
                    row_dict[h_name] = cell_to_str(row[h_idx])
            row_dict["__line_no__"] = r_idx + 1
            raw_rows.append(row_dict)
    else:
        raise ValueError("Unsupported file format. Please upload .xlsx or .csv.")

    return raw_rows, headers

def validate_device_import(db: Session, file_bytes: bytes, filename: str) -> Dict[str, Any]:
    """
    Validates uploaded Device Inventory file.
    Detects headers, validates serials as TEXT, checks for duplicates,
    and returns preview statistics and row-level rejection details.
    """
    raw_rows, headers = parse_device_file(file_bytes, filename)
    if not headers:
        return {
            "ok": False,
            "error": "The uploaded file is empty or missing headers.",
            "total_rows": 0,
            "valid_rows": [],
            "rejected_rows": []
        }

    # Map headers to canonical fields
    field_mapping = {}
    for h in headers:
        norm = normalize_header(h)
        if norm in DEVICE_HEADER_MAP:
            field_mapping[DEVICE_HEADER_MAP[norm]] = h

    if "serial" not in field_mapping:
        return {
            "ok": False,
            "error": f"Required column 'Serial Number' was not found in headers: {', '.join(headers)}",
            "headers": headers,
            "total_rows": len(raw_rows),
            "valid_rows": [],
            "rejected_rows": []
        }

    # Query existing serials in DB for NEW vs UPDATE detection
    existing_devices = {d.serial.lower(): d for d in db.query(Device).all()}

    valid_rows = []
    rejected_rows = []
    seen_serials_in_file = set()

    for row in raw_rows:
        line_no = row.get("__line_no__", 0)
        serial_val = row.get(field_mapping["serial"], "").strip()
        make_val = row.get(field_mapping.get("make", ""), "").strip() or "CHAINWAY"
        model_val = row.get(field_mapping.get("model", ""), "").strip() or "HHD"
        dept_val = row.get(field_mapping.get("department", ""), "").strip() or "INV MTS"
        remarks_val = row.get(field_mapping.get("remarks", ""), "").strip() or "Working"
        date_val = row.get(field_mapping.get("inventory_date", ""), "").strip()
        sr_no_val = row.get(field_mapping.get("sr_no", ""), "").strip()

        # Validation rules:
        # 1. Serial must not be empty
        if not serial_val:
            rejected_rows.append({
                "row_number": line_no,
                "serial": "",
                "make": make_val,
                "model": model_val,
                "department": dept_val,
                "reason": "Serial Number cannot be empty."
            })
            continue

        # 2. Check for duplicate serial within the same spreadsheet
        serial_key = serial_val.lower()
        if serial_key in seen_serials_in_file:
            rejected_rows.append({
                "row_number": line_no,
                "serial": serial_val,
                "make": make_val,
                "model": model_val,
                "department": dept_val,
                "reason": f"Duplicate Serial Number '{serial_val}' appears multiple times in file."
            })
            continue
        seen_serials_in_file.add(serial_key)

        # 3. Classify action (NEW vs UPDATE)
        action_type = "UPDATE" if serial_key in existing_devices else "NEW"
        current_status = existing_devices[serial_key].status if action_type == "UPDATE" else "Available"

        # Determine condition from remarks
        cond_lower = remarks_val.lower()
        if "damage" in cond_lower:
            parsed_condition = "Damaged"
        elif "repair" in cond_lower:
            parsed_condition = "Under Repair"
        else:
            parsed_condition = "Working"

        valid_rows.append({
            "row_number": line_no,
            "sr_no": sr_no_val,
            "serial": serial_val,
            "make": make_val,
            "model": model_val,
            "department": dept_val,
            "remarks": remarks_val,
            "condition": parsed_condition,
            "inventory_date": date_val or date.today().isoformat(),
            "action": action_type,
            "current_status": current_status
        })

    new_count = sum(1 for r in valid_rows if r["action"] == "NEW")
    update_count = sum(1 for r in valid_rows if r["action"] == "UPDATE")

    return {
        "ok": True,
        "filename": filename,
        "type": "device",
        "total_rows": len(raw_rows),
        "valid_count": len(valid_rows),
        "rejected_count": len(rejected_rows),
        "new_count": new_count,
        "update_count": update_count,
        "valid_rows": valid_rows,
        "rejected_rows": rejected_rows
    }

def commit_device_import(db: Session, valid_rows: List[Dict[str, Any]], mode: str = "UPSERT") -> Dict[str, Any]:
    """
    Commits validated device rows to database in a single atomic transaction.
    Supports 'UPSERT' and 'REPLACE'.
    """
    with SCAN_WRITE_LOCK:
        if mode == "REPLACE":
            # Safety checks before REPLACE:
            # 1. Take safety backup
            create_safe_backup("pre_replace_device_inventory")

            # 2. Check if any device is currently ISSUED
            active_count = db.query(Device).filter(Device.status == "Issued").count()
            if active_count > 0:
                raise ValueError(
                    f"Cannot execute REPLACE mode: {active_count} device(s) are currently ISSUED to employees. "
                    "Return all devices first or choose UPSERT mode to preserve operational continuity."
                )

            # 3. Check for devices not in the new file
            new_serials_lower = set(r["serial"].lower() for r in valid_rows)
            existing_devs = db.query(Device).all()
            for d in existing_devs:
                if d.serial.lower() not in new_serials_lower:
                    # Check if device has historical transactions
                    has_tx = db.query(Transaction).filter(Transaction.serial == d.serial).first() is not None
                    if has_tx:
                        # Preserve record for audit history, mark as Missing / Decommissioned
                        d.status = "Missing"
                        d.notes = (d.notes or "") + " [Decommissioned by inventory master replace]"
                    else:
                        db.delete(d)
            db.flush()

        # Apply records
        now = datetime.now()
        inserted_count = 0
        updated_count = 0

        for r in valid_rows:
            dev = db.query(Device).filter(Device.serial == r["serial"]).first()
            if not dev:
                # Insert NEW
                status_to_set = "Available"
                if r["condition"] == "Damaged":
                    status_to_set = "Damaged"
                elif r["condition"] == "Under Repair":
                    status_to_set = "Under Repair"

                dev = Device(
                    serial=r["serial"],
                    make=r["make"],
                    model=r["model"],
                    department=r["department"],
                    inventory_date=r["inventory_date"],
                    cover_qty=0,
                    status=status_to_set,
                    condition=r["condition"],
                    notes=r["remarks"] or "Imported via Excel Master",
                    created_at=now,
                    updated_at=now
                )
                db.add(dev)
                inserted_count += 1
            else:
                # Update existing
                dev.make = r["make"]
                dev.model = r["model"]
                dev.department = r["department"]
                dev.inventory_date = r["inventory_date"]
                if r["remarks"]:
                    dev.notes = r["remarks"]

                # IMPORTANT: Never reset an Issued device to Available!
                if dev.status != "Issued":
                    if r["condition"] == "Damaged":
                        dev.status = "Damaged"
                    elif r["condition"] == "Under Repair":
                        dev.status = "Under Repair"
                    elif dev.status in ("Damaged", "Under Repair") and r["condition"] == "Working":
                        dev.status = "Available"
                    dev.condition = r["condition"]

                dev.updated_at = now
                updated_count += 1

        db.commit()

        return {
            "ok": True,
            "mode": mode,
            "total_imported": len(valid_rows),
            "inserted": inserted_count,
            "updated": updated_count
        }

# ---------------------------------------------------------------------------
# Employee Master Parsing & Validation
# ---------------------------------------------------------------------------
EMPLOYEE_HEADER_MAP = {
    "srno": "sr_no",
    "sr": "sr_no",
    "employeeid": "employee_id",
    "empid": "employee_id",
    "id": "employee_id",
    "badgeid": "employee_id",
    "employeename": "name",
    "empname": "name",
    "name": "name",
    "department": "department",
    "dept": "department",
    "status": "status",
    "active": "status"
}

def parse_employee_file(file_bytes: bytes, filename: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Parses .xlsx or .csv employee master file into raw rows."""
    filename_lower = filename.lower()
    raw_rows = []
    headers = []

    if filename_lower.endswith(".xlsx"):
        wb = load_workbook(io.BytesIO(file_bytes), data_only=True)
        ws = wb["Employee Master"] if "Employee Master" in wb.sheetnames else wb.active
        all_rows = list(ws.iter_rows(values_only=True))
        if not all_rows:
            return [], []

        header_row_idx = 0
        for idx, row in enumerate(all_rows):
            if any(cell_to_str(c) for c in row):
                header_row_idx = idx
                headers = [cell_to_str(c) for c in row]
                break

        for r_idx in range(header_row_idx + 1, len(all_rows)):
            row = all_rows[r_idx]
            if not any(cell_to_str(c) for c in row):
                continue
            row_dict = {}
            for h_idx, h_name in enumerate(headers):
                if h_idx < len(row):
                    row_dict[h_name] = cell_to_str(row[h_idx])
            row_dict["__line_no__"] = r_idx + 1
            raw_rows.append(row_dict)

    elif filename_lower.endswith(".csv"):
        text_stream = io.StringIO(file_bytes.decode("utf-8-sig", errors="replace"))
        reader = csv.reader(text_stream)
        all_rows = list(reader)
        if not all_rows:
            return [], []

        headers = [cell_to_str(c) for c in all_rows[0]]
        for r_idx in range(1, len(all_rows)):
            row = all_rows[r_idx]
            if not any(cell_to_str(c) for c in row):
                continue
            row_dict = {}
            for h_idx, h_name in enumerate(headers):
                if h_idx < len(row):
                    row_dict[h_name] = cell_to_str(row[h_idx])
            row_dict["__line_no__"] = r_idx + 1
            raw_rows.append(row_dict)
    else:
        raise ValueError("Unsupported file format. Please upload .xlsx or .csv.")

    return raw_rows, headers

def validate_employee_import(db: Session, file_bytes: bytes, filename: str) -> Dict[str, Any]:
    """
    Validates uploaded Employee Master file.
    Preserves Employee IDs strictly as TEXT (leading zeros and letters),
    checks duplicates, and returns preview statistics.
    """
    raw_rows, headers = parse_employee_file(file_bytes, filename)
    if not headers:
        return {
            "ok": False,
            "error": "The uploaded file is empty or missing headers.",
            "total_rows": 0,
            "valid_rows": [],
            "rejected_rows": []
        }

    field_mapping = {}
    for h in headers:
        norm = normalize_header(h)
        if norm in EMPLOYEE_HEADER_MAP:
            field_mapping[EMPLOYEE_HEADER_MAP[norm]] = h

    if "employee_id" not in field_mapping:
        return {
            "ok": False,
            "error": f"Required column 'Employee ID' was not found in headers: {', '.join(headers)}",
            "headers": headers,
            "total_rows": len(raw_rows),
            "valid_rows": [],
            "rejected_rows": []
        }

    existing_employees = {e.employee_id.lower(): e for e in db.query(Employee).all()}

    valid_rows = []
    rejected_rows = []
    seen_ids_in_file = set()

    for row in raw_rows:
        line_no = row.get("__line_no__", 0)
        emp_id_val = row.get(field_mapping["employee_id"], "").strip()
        name_val = row.get(field_mapping.get("name", ""), "").strip()
        dept_val = row.get(field_mapping.get("department", ""), "").strip() or "Warehouse"
        status_raw = row.get(field_mapping.get("status", ""), "").strip()
        sr_no_val = row.get(field_mapping.get("sr_no", ""), "").strip()

        # Validation rules:
        if not emp_id_val:
            rejected_rows.append({
                "row_number": line_no,
                "employee_id": "",
                "name": name_val,
                "department": dept_val,
                "reason": "Employee ID cannot be empty."
            })
            continue

        if not name_val:
            rejected_rows.append({
                "row_number": line_no,
                "employee_id": emp_id_val,
                "name": "",
                "department": dept_val,
                "reason": "Employee Name cannot be empty."
            })
            continue

        id_key = emp_id_val.lower()
        if id_key in seen_ids_in_file:
            rejected_rows.append({
                "row_number": line_no,
                "employee_id": emp_id_val,
                "name": name_val,
                "department": dept_val,
                "reason": f"Duplicate Employee ID '{emp_id_val}' appears multiple times in file."
            })
            continue
        seen_ids_in_file.add(id_key)

        # Parse active status
        status_lower = status_raw.lower()
        is_active = not (status_lower in ("inactive", "0", "false", "disabled", "no"))

        action_type = "UPDATE" if id_key in existing_employees else "NEW"

        valid_rows.append({
            "row_number": line_no,
            "sr_no": sr_no_val,
            "employee_id": emp_id_val,
            "name": name_val,
            "department": dept_val,
            "active": is_active,
            "status_str": "Active" if is_active else "Inactive",
            "action": action_type
        })

    new_count = sum(1 for r in valid_rows if r["action"] == "NEW")
    update_count = sum(1 for r in valid_rows if r["action"] == "UPDATE")

    return {
        "ok": True,
        "filename": filename,
        "type": "employee",
        "total_rows": len(raw_rows),
        "valid_count": len(valid_rows),
        "rejected_count": len(rejected_rows),
        "new_count": new_count,
        "update_count": update_count,
        "valid_rows": valid_rows,
        "rejected_rows": rejected_rows
    }

def commit_employee_import(db: Session, valid_rows: List[Dict[str, Any]], mode: str = "UPSERT") -> Dict[str, Any]:
    """
    Commits validated employee rows to database in a single atomic transaction.
    Supports 'UPSERT' and 'REPLACE'.
    """
    with SCAN_WRITE_LOCK:
        if mode == "REPLACE":
            create_safe_backup("pre_replace_employee_master")

            # Check if any employee currently holds devices
            held_count = db.query(Device).filter(Device.status == "Issued", Device.assigned_employee_id.isnot(None)).count()
            if held_count > 0:
                raise ValueError(
                    f"Cannot execute REPLACE mode: {held_count} employee(s) currently hold active devices. "
                    "Return all devices first or choose UPSERT mode to preserve device assignments."
                )

            new_ids_lower = set(r["employee_id"].lower() for r in valid_rows)
            existing_emps = db.query(Employee).all()
            for emp in existing_emps:
                if emp.employee_id.lower() not in new_ids_lower:
                    # Check if referenced in historical transactions
                    has_tx = db.query(Transaction).filter(Transaction.employee_id == emp.employee_id).first() is not None
                    if has_tx:
                        emp.active = False
                    else:
                        db.delete(emp)
            db.flush()

        now = datetime.now()
        inserted_count = 0
        updated_count = 0

        for r in valid_rows:
            emp = db.query(Employee).filter(Employee.employee_id == r["employee_id"]).first()
            if not emp:
                emp = Employee(
                    employee_id=r["employee_id"],
                    name=r["name"],
                    department=r["department"],
                    active=r["active"],
                    created_at=now,
                    updated_at=now
                )
                db.add(emp)
                inserted_count += 1
            else:
                emp.name = r["name"]
                emp.department = r["department"]
                emp.active = r["active"]
                emp.updated_at = now
                updated_count += 1

        db.commit()

        return {
            "ok": True,
            "mode": mode,
            "total_imported": len(valid_rows),
            "inserted": inserted_count,
            "updated": updated_count
        }
