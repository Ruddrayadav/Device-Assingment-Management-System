from datetime import datetime, date, timedelta, time as dtime
from typing import Tuple, Dict, Any, List
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, or_
from app.config import SHIFT_DEFINITIONS
from app.models import Transaction, DeviceAssignment, Device, DamageReport, ShiftLog, Employee
from app.timefmt import fmt_datetime

def get_current_shift_and_op_date(dt: datetime = None) -> Tuple[str, str]:
    """
    Returns (shift_name, operational_date_str) based on timestamp.
    Handles midnight crossing:
    Night shift: 22:00 to 06:00.
    If time is 00:00 to 05:59:59, operational date is yesterday's date.
    """
    if dt is None:
        dt = datetime.now()
    
    t = dt.time()
    cur_date = dt.date()

    # Morning shift: 06:00 - 14:00
    if dtime(6, 0) <= t < dtime(14, 0):
        return "Morning", cur_date.isoformat()
    
    # Evening shift: 14:00 - 22:00
    elif dtime(14, 0) <= t < dtime(22, 0):
        return "Evening", cur_date.isoformat()
    
    # Night shift: 22:00 - 06:00
    else:
        # If past midnight (00:00 - 05:59:59), the operational shift date started yesterday
        if t < dtime(6, 0):
            op_date = cur_date - timedelta(days=1)
        else:
            op_date = cur_date
        return "Night", op_date.isoformat()

def get_operational_date_for_shift(shift_name: str, dt: datetime = None) -> str:
    """
    Returns operational date taking into account user's active shift if specified.
    """
    if dt is None:
        dt = datetime.now()
    cur_date = dt.date()
    t = dt.time()

    if shift_name == "Night":
        # If current time is early morning before noon, it belongs to yesterday's night shift
        if t < dtime(12, 0):
            return (cur_date - timedelta(days=1)).isoformat()
        return cur_date.isoformat()
    else:
        return cur_date.isoformat()

def calculate_shift_reconciliation(db: Session, operational_date: str, shift_name: str) -> Dict[str, Any]:
    """
    Calculates detailed reconciliation metrics for a specific operational shift.
    Never relies on manual counts; computes strictly from transaction records.
    """
    # 1. Transactions in this shift
    txs = db.query(Transaction).filter(
        Transaction.operational_date == operational_date,
        Transaction.shift_name == shift_name
    ).order_by(Transaction.id.asc()).all()

    issued_txs = [t for t in txs if t.transaction_type == "Issue"]
    returned_txs = [t for t in txs if t.transaction_type == "Return"]

    total_issued = len(issued_txs)
    total_returned = len(returned_txs)

    working_returned = sum(1 for t in returned_txs if t.condition == "Working")
    damaged_returned = sum(1 for t in returned_txs if t.condition in ("Damaged", "Broken screen", "Blank screen", "Not powering on", "Scanner not working", "Battery issue", "Physical damage", "Other"))
    missing_returned = sum(1 for t in returned_txs if t.condition == "Missing")

    # Outstanding from this specific shift:
    # Find assignments created in this operational shift that are still ACTIVE
    active_assignments_from_shift = db.query(DeviceAssignment).filter(
        DeviceAssignment.issued_operational_date == operational_date,
        DeviceAssignment.issued_shift == shift_name,
        DeviceAssignment.status == "ACTIVE"
    ).all()

    outstanding_count = len(active_assignments_from_shift)
    discrepancy = total_issued - total_returned

    # Detailed list of outstanding devices from this shift
    outstanding_list = []
    for asgn in active_assignments_from_shift:
        dev = db.query(Device).filter(Device.serial == asgn.device_serial).first()
        emp = db.query(Employee).filter(Employee.employee_id == asgn.employee_id).first()
        outstanding_list.append({
            "serial": asgn.device_serial,
            "employee_id": asgn.employee_id,
            "employee_name": emp.name if emp else "Unknown",
            "department": emp.department if emp else "Unknown",
            "issued_at": fmt_datetime(asgn.issued_at),
            "issued_by": asgn.issued_by_operator_name,
            "current_status": dev.status if dev else "Unknown"
        })

    # Global fleet status snapshot
    fleet_total = db.query(Device).count()
    fleet_available = db.query(Device).filter(Device.status == "Available").count()
    fleet_issued = db.query(Device).filter(Device.status == "Issued").count()
    fleet_damaged = db.query(Device).filter(Device.status == "Damaged").count()
    fleet_missing = db.query(Device).filter(Device.status == "Missing").count()
    fleet_repair = db.query(Device).filter(Device.status == "Under Repair").count()

    # Shift log if closed
    shift_log = db.query(ShiftLog).filter(
        ShiftLog.operational_date == operational_date,
        ShiftLog.shift_name == shift_name
    ).first()

    is_closed = (shift_log is not None and shift_log.status == "CLOSED")

    return {
        "operational_date": operational_date,
        "shift_name": shift_name,
        "is_closed": is_closed,
        "closed_at": fmt_datetime(shift_log.closed_at) if (shift_log and shift_log.closed_at) else None,
        "closed_by": shift_log.closed_by_name if shift_log else None,
        "total_issued": total_issued,
        "total_returned": total_returned,
        "working_returned": working_returned,
        "damaged_returned": damaged_returned,
        "missing_returned": missing_returned,
        "outstanding_count": outstanding_count,
        "discrepancy": discrepancy,
        "outstanding_devices": outstanding_list,
        "fleet_snapshot": {
            "total": fleet_total,
            "available": fleet_available,
            "issued": fleet_issued,
            "damaged": fleet_damaged,
            "missing": fleet_missing,
            "under_repair": fleet_repair
        }
    }

