from datetime import datetime, date
from typing import Dict, Any, Optional
from fastapi import APIRouter, Depends, Request, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import get_db
from app.models import User, Device, Employee, Transaction, DamageReport, DeviceAssignment
from app.auth import require_user, require_admin, get_current_user_optional
from app.scan_engine import (
    process_scan_pipeline, return_device_atomic, issue_device_atomic,
    sanitize_code, ScanException
)
from app.shifts import get_current_shift_and_op_date, get_operational_date_for_shift

router = APIRouter(prefix="/api")

def get_live_board_data(db: Session) -> Dict[str, Any]:
    today_str = date.today().isoformat()
    t0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    devices = db.query(Device).all()
    avail, allot, unavail, not_today = [], [], [], []

    for d in devices:
        emp = db.query(Employee).filter(Employee.employee_id == d.assigned_employee_id).first() if d.assigned_employee_id else None
        last_tx = db.query(Transaction).filter(
            Transaction.serial == d.serial,
            Transaction.transaction_type == "Issue"
        ).order_by(Transaction.id.desc()).first()
        last_time_str = last_tx.transaction_time.strftime("%Y-%m-%d %H:%M:%S") if last_tx else None

        it = {
            "serial": d.serial,
            "make": d.make,
            "model": d.model,
            "status": d.status,
            "condition": d.condition,
            "eid": emp.employee_id if emp else "",
            "ename": emp.name if emp else "",
            "dept": emp.department if emp else "",
            "last_ho": last_time_str
        }

        if d.status == "Available":
            avail.append(it)
        elif d.status == "Issued":
            allot.append(it)
        else:
            unavail.append(it)

        # Devices not issued today
        if d.status in ("Available", "Issued"):
            if not last_tx or last_tx.transaction_time < t0:
                not_today.append(it)

    # Sort
    allot.sort(key=lambda x: x["last_ho"] or "", reverse=True)
    not_today.sort(key=lambda x: (x["status"] != "Issued", x["last_ho"] or ""))

    # Recent 15 transactions
    recent_txs = db.query(Transaction).order_by(Transaction.id.desc()).limit(15).all()
    recent = [{
        "time": t.transaction_time.strftime("%H:%M:%S"),
        "full_time": t.transaction_time.strftime("%Y-%m-%d %H:%M:%S"),
        "type": t.transaction_type,
        "serial": t.serial,
        "name": t.employee_name,
        "dept": t.department,
        "cond": t.condition,
        "by": t.operator_name,
        "shift": t.shift_name
    } for t in recent_txs]

    open_issues = db.query(DamageReport).filter(DamageReport.review_status == "Open").count()
    last_tx_id = db.query(func.coalesce(func.max(Transaction.id), 0)).scalar() or 0
    version = f"{last_tx_id}|{len(devices)}|{open_issues}|{today_str}"

    out_today = db.query(Transaction).filter(Transaction.transaction_type == "Issue", Transaction.transaction_time >= t0).count()
    back_today = db.query(Transaction).filter(Transaction.transaction_type == "Return", Transaction.transaction_time >= t0).count()

    return {
        "version": version,
        "server_now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "today": today_str,
        "available": avail,
        "allotted": allot,
        "unavailable": unavail,
        "not_today": not_today,
        "recent": recent,
        "counts": {
            "total": len(devices),
            "available": len(avail),
            "allotted": len(allot),
            "unavailable": len(unavail),
            "not_today": len(not_today),
            "not_today_held": sum(1 for i in not_today if i["status"] == "Issued"),
            "issues": open_issues,
            "out_today": out_today,
            "back_today": back_today
        }
    }

class ScanPayload(BaseModel):
    code: str
    mode: str = "Issue"
    pending: Optional[Dict[str, Any]] = None
    idempotency_key: Optional[str] = None

@router.post("/scan")
async def handle_scan(
    payload: ScanPayload,
    user: User = Depends(require_user),
    db: Session = Depends(get_db)
):
    active_shift = getattr(user, "active_shift", user.default_shift)
    result = process_scan_pipeline(
        db=db,
        code=payload.code,
        mode=payload.mode,
        pending=payload.pending,
        operator=user,
        active_shift=active_shift,
        idempotency_key=payload.idempotency_key
    )

    # Include live board data and shift stats
    board_data = get_live_board_data(db)
    result["board"] = board_data

    # Current operator shift stats
    _, op_date = get_current_shift_and_op_date()
    op_issued = db.query(Transaction).filter(
        Transaction.operator_id == user.id,
        Transaction.operational_date == op_date,
        Transaction.shift_name == active_shift,
        Transaction.transaction_type == "Issue"
    ).count()
    op_returned = db.query(Transaction).filter(
        Transaction.operator_id == user.id,
        Transaction.operational_date == op_date,
        Transaction.shift_name == active_shift,
        Transaction.transaction_type == "Return"
    ).count()

    result["operator_stats"] = {
        "operator_name": user.full_name or user.username,
        "shift": active_shift,
        "issued_count": op_issued,
        "returned_count": op_returned,
        "total_transactions": op_issued + op_returned
    }

    return result

class ReturnPayload(BaseModel):
    serial: str
    condition: str = "Working"
    damage_type: Optional[str] = None
    remarks: Optional[str] = ""
    idempotency_key: Optional[str] = None

@router.post("/return")
async def handle_detailed_return(
    payload: ReturnPayload,
    user: User = Depends(require_user),
    db: Session = Depends(get_db)
):
    active_shift = getattr(user, "active_shift", user.default_shift)
    try:
        res = return_device_atomic(
            db=db,
            device_serial=payload.serial,
            operator=user,
            condition=payload.condition,
            damage_type=payload.damage_type,
            remarks=payload.remarks or "",
            active_shift=active_shift,
            idempotency_key=payload.idempotency_key
        )
        return {
            "ok": True,
            "state": "done",
            "title": f"Returned · {payload.serial[-6:]}",
            "message": res["message"],
            "action": "Return",
            "device": res["device"],
            "employee": res["employee"],
            "board": get_live_board_data(db)
        }
    except ScanException as ex:
        return {
            "ok": False,
            "state": "error",
            "title": ex.title,
            "message": ex.message
        }

class DamageReportPayload(BaseModel):
    serial: str
    fault_type: str
    details: str = ""
    employee_id: Optional[str] = None

@router.post("/damage/report")
async def handle_damage_report(
    payload: DamageReportPayload,
    user: User = Depends(require_user),
    db: Session = Depends(get_db)
):
    clean_serial = sanitize_code(payload.serial)
    dev = db.query(Device).filter(Device.serial == clean_serial).first()
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found.")

    emp = None
    if payload.employee_id:
        emp = db.query(Employee).filter(Employee.employee_id == payload.employee_id).first()
    elif dev.assigned_employee_id:
        emp = db.query(Employee).filter(Employee.employee_id == dev.assigned_employee_id).first()

    now = datetime.now()
    shift_name, op_date = get_current_shift_and_op_date(now)

    dmg = DamageReport(
        report_time=now,
        operational_date=op_date,
        shift_name=shift_name,
        serial=dev.serial,
        employee_id=emp.employee_id if emp else None,
        employee_name=emp.name if emp else "",
        department=emp.department if emp else "",
        fault_type=payload.fault_type,
        details=payload.details or "Direct damage report",
        reported_by_id=user.id,
        reported_by_name=user.full_name or user.username,
        review_status="Open"
    )
    db.add(dmg)

    # If device is not issued, mark status as Damaged immediately
    if dev.status != "Issued":
        dev.status = "Damaged"
    dev.condition = payload.fault_type
    dev.updated_at = now

    db.commit()

    return {
        "ok": True,
        "state": "done",
        "title": "Damage Logged",
        "message": f"Device {dev.serial} reported with '{payload.fault_type}'.",
        "board": get_live_board_data(db)
    }

@router.get("/board/live")
async def get_live_board(
    v: Optional[str] = None,
    db: Session = Depends(get_db)
):
    data = get_live_board_data(db)
    if v and v == data["version"]:
        return {"changed": False}
    return {"changed": True, "board": data}

class QuickAddDevicePayload(BaseModel):
    serial: str
    make: str = "CHAINWAY"
    model: str = "HHD"
    cover_qty: int = 0

@router.post("/device/quick-add")
async def quick_add_device(
    payload: QuickAddDevicePayload,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    serial_clean = sanitize_code(payload.serial)
    if not serial_clean:
        raise HTTPException(status_code=400, detail="Serial is required.")
    existing = db.query(Device).filter(Device.serial == serial_clean).first()
    if existing:
        return {"ok": False, "message": "Device serial already exists in inventory."}

    now = datetime.now()
    dev = Device(
        serial=serial_clean,
        make=payload.make,
        model=payload.model,
        cover_qty=payload.cover_qty,
        status="Available",
        condition="Working",
        created_at=now,
        updated_at=now
    )
    db.add(dev)
    db.commit()

    return {
        "ok": True,
        "message": f"Device {serial_clean} added to inventory.",
        "serial": serial_clean,
        "board": get_live_board_data(db)
    }

