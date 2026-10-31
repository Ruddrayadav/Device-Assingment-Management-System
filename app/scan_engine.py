import re
from datetime import datetime, date
from typing import Optional, Dict, Any, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from app.database import SCAN_WRITE_LOCK
from app.models import (
    Device, Employee, DeviceAssignment, Transaction,
    DamageReport, User
)
from app.config import DAMAGE_CATEGORIES, now_kolkata, today_kolkata_str

class ScanException(Exception):
    def __init__(self, message: str, title: str = "Scan Rejected", code: str = None):
        super().__init__(message)
        self.message = message
        self.title = title
        self.code = code

def sanitize_code(code: str) -> str:
    if not code:
        return ""
    # Strip non-printable and control characters
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", str(code)).strip()
    return cleaned[:64]

def resolve_barcode(db: Session, code: str, prefer_type: Optional[str] = None) -> Tuple[Optional[Device], Optional[Employee]]:
    """
    Resolves barcode to Device or Employee.
    Enforces strict barcode classification:
    - Prefer exact full barcode matches.
    - Never use ambiguous suffix matching to confuse entity types.
    - If a barcode matches both employee and device records, raises explicit ambiguity error.
    - Never issue or return a device based on an uncertain match.
    """
    code_clean = sanitize_code(code)
    if not code_clean:
        return None, None

    # 1. Exact employee search
    emp = db.query(Employee).filter(func.lower(Employee.employee_id) == code_clean.lower()).first()

    # 2. Exact device search
    dev = db.query(Device).filter(func.lower(Device.serial) == code_clean.lower()).first()

    # 3. Barcode collision check: if barcode matches BOTH employee and device records
    if emp and dev:
        raise ScanException(
            f"Ambiguous Barcode Collision: '{code_clean}' matches both Employee {emp.name} ({emp.employee_id}) and Device serial {dev.serial}. "
            "Operation halted to prevent uncertain allocation. Please contact an administrator.",
            "Ambiguous Barcode",
            "AMBIGUOUS_BARCODE"
        )

    # 4. If exact employee was found, return employee only (never suffix match device)
    if emp:
        return None, emp

    # 5. If exact device was found, return device only
    if dev:
        return dev, None

    # 6. Fallback suffix matching for devices ONLY if explicitly preferred and strictly unique
    if prefer_type == "device" and len(code_clean) >= 4:
        matches = db.query(Device).filter(Device.serial.ilike(f"%{code_clean}")).limit(2).all()
        if len(matches) == 1:
            return matches[0], None

    return None, None


def issue_device_atomic(
    db: Session,
    device_serial: str,
    employee_id: str,
    operator: User,
    idempotency_key: Optional[str] = None
) -> Dict[str, Any]:
    """
    Atomically issues a device to an employee with thread and database level protection.
    Guarantees zero duplicate allocations. 24/7 continuous operation.
    """
    with SCAN_WRITE_LOCK:
        now = now_kolkata()
        date_str = today_kolkata_str()

        if isinstance(operator, User):
            op_id = operator.id
            op_name = operator.full_name or operator.username
        else:
            op_id = int(operator) if isinstance(operator, int) else getattr(operator, "id", None)
            op_name = getattr(operator, "full_name", None) or getattr(operator, "username", None) or str(operator)

        # Check idempotency by key
        if idempotency_key:
            existing_tx = db.query(Transaction).filter(Transaction.idempotency_key == idempotency_key).first()
            if existing_tx:
                dev_rec = db.query(Device).filter(Device.serial == existing_tx.serial).first()
                return {
                    "action": "Issue",
                    "serial": existing_tx.serial,
                    "employee": {"id": existing_tx.employee_id, "name": existing_tx.employee_name, "dept": existing_tx.department},
                    "device": {"serial": existing_tx.serial, "model": dev_rec.model if dev_rec else "", "cover_qty": existing_tx.cover_qty},
                    "timestamp": existing_tx.transaction_time.strftime("%Y-%m-%d %I:%M:%S %p"),
                    "operator": existing_tx.operator_name,
                    "message": f"Device {existing_tx.serial} is already issued to {existing_tx.employee_name} (Idempotent retry)."
                }

        # 1. Validate employee
        emp = db.query(Employee).filter(Employee.employee_id == employee_id).first()
        if not emp:
            raise ScanException("Employee ID not found in Master records.", "Employee Not Found")
        if not emp.active:
            raise ScanException(f"{emp.name} ({emp.employee_id}) is INACTIVE. Contact Administrator.", "Employee Inactive")

        # 2. Lock & Validate device
        dev = db.query(Device).filter(Device.serial == device_serial).with_for_update().first()
        if not dev:
            raise ScanException("Device serial not found in Inventory.", "Device Not Found")

        if dev.status == "Issued":
            if dev.assigned_employee_id == emp.employee_id:
                # Idempotent re-scan of the same device for the same employee!
                held_count = db.query(Device).filter(
                    Device.assigned_employee_id == emp.employee_id,
                    Device.status == "Issued"
                ).count()
                return {
                    "action": "Issue",
                    "serial": dev.serial,
                    "idempotent_replay": True,
                    "employee": {
                        "id": emp.employee_id,
                        "name": emp.name,
                        "dept": emp.department,
                        "held_count": held_count
                    },
                    "device": {
                        "serial": dev.serial,
                        "model": dev.model,
                        "cover_qty": dev.cover_qty
                    },
                    "timestamp": now.strftime("%Y-%m-%d %I:%M:%S %p"),
                    "operator": op_name,
                    "message": f"Device {dev.serial} is already issued to {emp.name} ({emp.department})."
                }

            holder = db.query(Employee).filter(Employee.employee_id == dev.assigned_employee_id).first()
            holder_name = holder.name if holder else dev.assigned_employee_id
            raise ScanException(
                f"Device {dev.serial} is ALREADY ISSUED to {holder_name} ({dev.assigned_employee_id}). Return it first.",
                "Device Already Issued"
            )

        if dev.status != "Available":
            raise ScanException(
                f"Device {dev.serial} is marked as '{dev.status}' and cannot be issued.",
                "Device Unavailable"
            )

        # 3. Create DeviceAssignment record
        assignment = DeviceAssignment(
            device_serial=dev.serial,
            employee_id=emp.employee_id,
            issued_at=now,
            issued_date=date_str,
            issued_by_operator_id=op_id,
            issued_by_operator_name=op_name,
            status="ACTIVE"
        )
        db.add(assignment)

        # 4. Update Device state
        dev.status = "Issued"
        dev.assigned_employee_id = emp.employee_id
        dev.condition = "Working"
        dev.updated_at = now

        # 5. Insert Transaction audit record
        tx = Transaction(
            transaction_time=now,
            transaction_date=date_str,
            transaction_type="Issue",
            serial=dev.serial,
            employee_id=emp.employee_id,
            employee_name=emp.name,
            department=emp.department,
            operator_id=op_id,
            operator_name=op_name,
            condition="Working",
            cover_qty=dev.cover_qty,
            remarks="Normal Issue",
            idempotency_key=idempotency_key
        )
        db.add(tx)

        db.commit()

        # Count how many devices this employee currently holds
        held_count = db.query(Device).filter(
            Device.assigned_employee_id == emp.employee_id,
            Device.status == "Issued"
        ).count()

        return {
            "action": "Issue",
            "serial": dev.serial,
            "employee": {
                "id": emp.employee_id,
                "name": emp.name,
                "dept": emp.department,
                "held_count": held_count
            },
            "device": {
                "serial": dev.serial,
                "model": dev.model,
                "cover_qty": dev.cover_qty
            },
            "timestamp": now.strftime("%Y-%m-%d %I:%M:%S %p"),
            "operator": op_name,
            "message": f"Issued to {emp.name} ({emp.department})" + (f" · Now holds {held_count} devices" if held_count > 1 else "")
        }

def return_device_atomic(
    db: Session,
    device_serial: str,
    operator: User,
    condition: str = "Working",
    damage_type: Optional[str] = None,
    remarks: str = "",
    idempotency_key: Optional[str] = None
) -> Dict[str, Any]:
    """
    Atomically returns a device from whichever worker currently holds it.
    Zero need to scan employee ID again. 24/7 continuous operation.
    """
    with SCAN_WRITE_LOCK:
        now = now_kolkata()
        date_str = today_kolkata_str()

        if isinstance(operator, User):
            op_id = operator.id
            op_name = operator.full_name or operator.username
        else:
            op_id = int(operator) if isinstance(operator, int) else getattr(operator, "id", None)
            op_name = getattr(operator, "full_name", None) or getattr(operator, "username", None) or str(operator)

        # Check idempotency by key
        if idempotency_key:
            existing_tx = db.query(Transaction).filter(Transaction.idempotency_key == idempotency_key).first()
            if existing_tx:
                return {
                    "action": "Return",
                    "serial": existing_tx.serial,
                    "condition": existing_tx.condition,
                    "idempotent_replay": True,
                    "employee": {"id": existing_tx.employee_id, "name": existing_tx.employee_name, "dept": existing_tx.department},
                    "device": {"serial": existing_tx.serial, "status": "Available", "condition": existing_tx.condition},
                    "timestamp": existing_tx.transaction_time.strftime("%Y-%m-%d %I:%M:%S %p"),
                    "operator": existing_tx.operator_name,
                    "message": f"Device {existing_tx.serial} was already returned (Idempotent retry)."
                }

        dev = db.query(Device).filter(Device.serial == device_serial).with_for_update().first()
        if not dev:
            raise ScanException("Device serial not found in Inventory.", "Device Not Found")

        if dev.status != "Issued":
            # Check if this device was returned in the immediately preceding transaction
            last_tx = db.query(Transaction).filter(
                Transaction.serial == dev.serial
            ).order_by(Transaction.id.desc()).first()
            if last_tx and last_tx.transaction_type == "Return":
                return {
                    "action": "Return",
                    "serial": dev.serial,
                    "condition": dev.condition,
                    "idempotent_replay": True,
                    "employee": {
                        "id": last_tx.employee_id,
                        "name": last_tx.employee_name,
                        "dept": last_tx.department
                    },
                    "device": {
                        "serial": dev.serial,
                        "status": dev.status,
                        "condition": dev.condition
                    },
                    "timestamp": last_tx.transaction_time.strftime("%Y-%m-%d %I:%M:%S %p"),
                    "operator": last_tx.operator_name,
                    "message": f"Device {dev.serial} was already returned from {last_tx.employee_name} (Idempotent retry)."
                }

            raise ScanException(
                f"Device {dev.serial} is NOT ISSUED (Current status: {dev.status}). Nothing to return.",
                "Device Not Issued"
            )

        emp = db.query(Employee).filter(Employee.employee_id == dev.assigned_employee_id).first()
        emp_name = emp.name if emp else "Unknown Worker"
        emp_dept = emp.department if emp else "Warehouse"
        emp_id = emp.employee_id if emp else (dev.assigned_employee_id or "UNKNOWN")

        # 1. Update active DeviceAssignment
        asgn = db.query(DeviceAssignment).filter(
            DeviceAssignment.device_serial == dev.serial,
            DeviceAssignment.status == "ACTIVE"
        ).order_by(DeviceAssignment.id.desc()).first()

        final_condition = condition
        if damage_type:
            final_condition = damage_type

        if asgn:
            asgn.returned_at = now
            asgn.returned_date = date_str
            asgn.returned_by_operator_id = op_id
            asgn.returned_by_operator_name = op_name
            asgn.status = "RETURNED"
            asgn.condition_on_return = final_condition
            asgn.remarks = remarks

        # 2. Update Device status directly (atomic transition: never becomes Available if damaged)
        if final_condition == "Working":
            dev.status = "Available"
            dev.condition = "Working"
        else:
            dev.status = "Damaged" if final_condition in DAMAGE_CATEGORIES or final_condition == "Damaged" else final_condition
            dev.condition = final_condition

        dev.assigned_employee_id = None
        dev.updated_at = now

        # 3. Record DamageReport if damaged
        if final_condition != "Working":
            dmg = DamageReport(
                report_time=now,
                report_date=date_str,
                serial=dev.serial,
                employee_id=emp_id,
                employee_name=emp_name,
                department=emp_dept,
                fault_type=damage_type or final_condition,
                details=remarks or "Reported during return",
                reported_by_id=op_id,
                reported_by_name=op_name,
                review_status="Open"
            )
            db.add(dmg)

        # 4. Record Transaction
        tx = Transaction(
            transaction_time=now,
            transaction_date=date_str,
            transaction_type="Return",
            serial=dev.serial,
            employee_id=emp_id,
            employee_name=emp_name,
            department=emp_dept,
            operator_id=op_id,
            operator_name=op_name,
            condition=final_condition,
            cover_qty=dev.cover_qty,
            remarks=remarks or (f"Returned condition: {final_condition}"),
            idempotency_key=idempotency_key
        )
        db.add(tx)

        db.commit()

        return {
            "action": "Return",
            "serial": dev.serial,
            "condition": final_condition,
            "employee": {
                "id": emp_id,
                "name": emp_name,
                "dept": emp_dept
            },
            "device": {
                "serial": dev.serial,
                "status": dev.status,
                "condition": dev.condition
            },
            "timestamp": now.strftime("%Y-%m-%d %I:%M:%S %p"),
            "operator": op_name,
            "message": f"Returned from {emp_name} ({emp_dept})" + ("" if final_condition == "Working" else f" · Marked {final_condition}")
        }


def process_scan_pipeline(
    db: Session,
    code: str,
    mode: str,
    pending: Optional[Dict[str, str]],
    operator: User,
    idempotency_key: Optional[str] = None
) -> Dict[str, Any]:
    """
    High-speed, zero-enter unified scan pipeline.
    Resolves code, manages pending state, executes atomic actions without page reload.
    Supports both Employee-first and Device-first Issue workflows.
    24/7 continuous operation.
    """
    code = sanitize_code(code)
    if not code:
        return {
            "ok": False,
            "state": "error",
            "title": "Empty Scan",
            "message": "No barcode detected.",
            "pending": pending
        }

    # Fast Idempotency Check by key
    if idempotency_key:
        existing_tx = db.query(Transaction).filter(Transaction.idempotency_key == idempotency_key).first()
        if existing_tx:
            dev_rec = db.query(Device).filter(Device.serial == existing_tx.serial).first()
            return {
                "ok": True,
                "state": "done",
                "title": f"Already Processed · {existing_tx.serial[-6:]}",
                "message": f"Device {existing_tx.serial} is already {existing_tx.transaction_type.lower()}ed to/by {existing_tx.employee_name} (Idempotent retry).",
                "action": existing_tx.transaction_type,
                "device": {"serial": existing_tx.serial, "model": dev_rec.model if dev_rec else "", "cover_qty": existing_tx.cover_qty},
                "employee": {"id": existing_tx.employee_id, "name": existing_tx.employee_name, "dept": existing_tx.department},
                "pending": None,
                "idempotent_replay": True
            }

    p_type = pending.get("type") if pending else None
    p_code = sanitize_code(pending.get("code")) if pending else None

    # Resolve barcode identity
    prefer = "device" if p_type == "employee" else ("employee" if p_type == "device" else None)
    dev, emp = resolve_barcode(db, code, prefer_type=prefer)

    # Unknown barcode check
    if not dev and not emp:
        return {
            "ok": False,
            "state": "unknown",
            "title": "Unknown Barcode",
            "message": f"'{code}' is not recognized as a registered device or employee ID.",
            "code": code,
            "pending": pending,
            "can_add": operator.role == "admin"
        }

    # -------------------------------------------------------------
    # RETURN MODE WORKFLOW: One-scan instant return
    # -------------------------------------------------------------
    if mode == "Return":
        if emp and not dev:
            return {
                "ok": False,
                "state": "error",
                "title": "Worker Scanned in Return Mode",
                "message": f"Scanned employee '{emp.name}'. In Return mode, scan ONLY the DEVICE barcode.",
                "pending": None
            }
        # dev is present
        try:
            res = return_device_atomic(
                db=db,
                device_serial=dev.serial,
                operator=operator,
                condition="Working",
                idempotency_key=idempotency_key
            )
            return {
                "ok": True,
                "state": "done",
                "title": f"Device Returned · {dev.serial[-6:]}",
                "message": res["message"],
                "action": "Return",
                "device": res["device"],
                "employee": res["employee"],
                "can_flag": True,
                "idempotent_replay": res.get("idempotent_replay", False),
                "pending": None
            }
        except ScanException as ex:
            return {
                "ok": False,
                "state": "error",
                "title": ex.title,
                "message": ex.message,
                "pending": None
            }

    # -------------------------------------------------------------
    # ISSUE MODE WORKFLOW: Automatic Employee-Device Linking
    # -------------------------------------------------------------
    try:
        # Case 1: Device scanned
        if dev:
            # If an employee was already pending, check for retried idempotent scan
            if p_type == "employee" and p_code:
                if dev.status == "Issued" and dev.assigned_employee_id == p_code:
                    emp_holder = db.query(Employee).filter(Employee.employee_id == p_code).first()
                    emp_hname = emp_holder.name if emp_holder else p_code
                    return {
                        "ok": True,
                        "state": "done",
                        "title": f"Already Issued · {dev.serial[-6:]}",
                        "message": f"Device {dev.serial} is already issued to {emp_hname}.",
                        "action": "Issue",
                        "device": {"serial": dev.serial, "model": dev.model, "cover_qty": dev.cover_qty},
                        "employee": {"id": p_code, "name": emp_hname, "dept": emp_holder.department if emp_holder else ""},
                        "pending": None,
                        "idempotent_replay": True
                    }

                res = issue_device_atomic(
                    db=db,
                    device_serial=dev.serial,
                    employee_id=p_code,
                    operator=operator,
                    idempotency_key=idempotency_key
                )
                return {
                    "ok": True,
                    "state": "done",
                    "title": f"Issued · {dev.serial[-6:]}",
                    "message": res["message"],
                    "action": "Issue",
                    "device": res["device"],
                    "employee": res["employee"],
                    "pending": None
                }

            if dev.status == "Issued":
                holder = db.query(Employee).filter(Employee.employee_id == dev.assigned_employee_id).first()
                holder_name = holder.name if holder else dev.assigned_employee_id
                return {
                    "ok": False,
                    "state": "error",
                    "title": "Already Issued",
                    "message": f"Device {dev.serial} is already with {holder_name}. Switch to Return mode to take it back.",
                    "pending": pending
                }

            if dev.status != "Available":
                return {
                    "ok": False,
                    "state": "error",
                    "title": f"Device {dev.status}",
                    "message": f"Device {dev.serial} is marked as '{dev.status}' and cannot be issued.",
                    "pending": pending
                }

            # Store device as pending, wait for employee scan
            return {
                "ok": True,
                "state": "pending",
                "title": f"Device Ready · {dev.serial[-6:]}",
                "message": f"Device {dev.serial} ready ({dev.model}). Now scan Worker ID barcode.",
                "action": "PendingDevice",
                "pending": {
                    "type": "device",
                    "code": dev.serial,
                    "label": f"Device: {dev.serial}"
                }
            }

        # Case 2: Employee scanned
        if emp:
            if not emp.active:
                return {
                    "ok": False,
                    "state": "error",
                    "title": "Employee Inactive",
                    "message": f"{emp.name} ({emp.employee_id}) is marked INACTIVE. Contact Administrator.",
                    "pending": pending
                }

            # If a device was already pending, check for retried idempotent scan
            if p_type == "device" and p_code:
                p_dev = db.query(Device).filter(Device.serial == p_code).first()
                if p_dev and p_dev.status == "Issued" and p_dev.assigned_employee_id == emp.employee_id:
                    return {
                        "ok": True,
                        "state": "done",
                        "title": f"Already Issued · {p_code[-6:]}",
                        "message": f"Device {p_code} is already issued to {emp.name}.",
                        "action": "Issue",
                        "device": {"serial": p_code, "model": p_dev.model, "cover_qty": p_dev.cover_qty},
                        "employee": {"id": emp.employee_id, "name": emp.name, "dept": emp.department},
                        "pending": None,
                        "idempotent_replay": True
                    }

                res = issue_device_atomic(
                    db=db,
                    device_serial=p_code,
                    employee_id=emp.employee_id,
                    operator=operator,
                    idempotency_key=idempotency_key
                )
                return {
                    "ok": True,
                    "state": "done",
                    "title": f"Issued · {p_code[-6:]}",
                    "message": res["message"],
                    "action": "Issue",
                    "device": res["device"],
                    "employee": res["employee"],
                    "pending": None
                }
            else:
                # Count current held devices
                held = db.query(Device).filter(
                    Device.assigned_employee_id == emp.employee_id,
                    Device.status == "Issued"
                ).count()
                held_str = f" · Currently holds {held} device{'s' if held != 1 else ''}" if held > 0 else " · Holds 0 devices"
                return {
                    "ok": True,
                    "state": "pending",
                    "title": f"Worker: {emp.name}",
                    "message": f"{emp.name} · {emp.department}{held_str}. Now scan Device barcode.",
                    "action": "PendingEmployee",
                    "pending": {
                        "type": "employee",
                        "code": emp.employee_id,
                        "label": f"{emp.name} ({emp.department})"
                    }
                }


    except ScanException as ex:
        return {
            "ok": False,
            "state": "error",
            "title": ex.title,
            "message": ex.message,
            "pending": pending
        }
