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
from app.timefmt import fmt_time, fmt_datetime

router = APIRouter(prefix="/api")

def get_board_version(db: Session) -> str:
    """
    Fast 4-query version string including max(Device.updated_at).
    """
    today_str = date.today().isoformat()
    last_tx_id = db.query(func.coalesce(func.max(Transaction.id), 0)).scalar() or 0
    device_count = db.query(func.count(Device.serial)).scalar() or 0
    open_issues = db.query(func.count(DamageReport.id)).filter(DamageReport.review_status == "Open").scalar() or 0
    max_updated = db.query(func.max(Device.updated_at)).scalar()
    max_updated_str = max_updated.isoformat() if max_updated else ""
    return f"{last_tx_id}|{device_count}|{open_issues}|{max_updated_str}|{today_str}"

def get_live_board_data(db: Session) -> Dict[str, Any]:
    today_str = date.today().isoformat()
    t0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    devices = db.query(Device).all()
    
    # Bulk query 1: All employees as a dict
    employees_by_id = {e.employee_id: e for e in db.query(Employee).all()}

    # Bulk query 2: Last Issue time per serial (GROUP BY serial)
    last_tx_rows = db.query(
        Transaction.serial,
        func.max(Transaction.transaction_time)
    ).filter(Transaction.transaction_type == "Issue").group_by(Transaction.serial).all()
    last_ho_by_serial = {serial: max_t for serial, max_t in last_tx_rows}

    avail, allot, unavail, not_today = [], [], [], []

    for d in devices:
        emp = employees_by_id.get(d.assigned_employee_id) if d.assigned_employee_id else None
        last_dt = last_ho_by_serial.get(d.serial)
        last_time_iso = last_dt.strftime("%Y-%m-%d %H:%M:%S") if last_dt else None
        last_time_fmt = fmt_datetime(last_dt) if last_dt else ""

        it = {
            "serial": d.serial,
            "make": d.make,
            "model": d.model,
            "department": getattr(d, "department", "GENERAL"),
            "status": d.status,
            "condition": d.condition,
            "eid": emp.employee_id if emp else "",
            "ename": emp.name if emp else "",
            "dept": emp.department if emp else "",
            "last_ho": last_time_iso,
            "last_ho_fmt": last_time_fmt
        }

        if d.status == "Available":
            avail.append(it)
        elif d.status == "Issued":
            allot.append(it)
        else:
            unavail.append(it)

        # Devices not issued today
        if d.status in ("Available", "Issued"):
            if not last_dt or last_dt < t0:
                not_today.append(it)

    # Sort
    allot.sort(key=lambda x: x["last_ho"] or "", reverse=True)
    not_today.sort(key=lambda x: (x["status"] != "Issued", x["last_ho"] or ""))

    # Recent 15 transactions
    recent_txs = db.query(Transaction).order_by(Transaction.id.desc()).limit(15).all()
    recent = [{
        "time": fmt_time(t.transaction_time),
        "full_time": fmt_datetime(t.transaction_time),
        "type": t.transaction_type,
        "serial": t.serial,
        "name": t.employee_name,
        "dept": t.department,
        "cond": t.condition,
        "by": t.operator_name
    } for t in recent_txs]

    open_issues = db.query(func.count(DamageReport.id)).filter(DamageReport.review_status == "Open").scalar() or 0
    version = get_board_version(db)

    out_today = db.query(func.count(Transaction.id)).filter(
        Transaction.transaction_type == "Issue",
        Transaction.transaction_time >= t0
    ).scalar() or 0
    back_today = db.query(func.count(Transaction.id)).filter(
        Transaction.transaction_type == "Return",
        Transaction.transaction_time >= t0
    ).scalar() or 0

    return {
        "version": version,
        "server_now": fmt_datetime(datetime.now()),
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
    result = process_scan_pipeline(
        db=db,
        code=payload.code,
        mode=payload.mode,
        pending=payload.pending,
        operator=user,
        idempotency_key=payload.idempotency_key
    )

    # Include live board data and operator stats
    board_data = get_live_board_data(db)
    result["board"] = board_data

    # Current operator daily stats
    today_str = date.today().isoformat()
    op_issued = db.query(Transaction).filter(
        Transaction.operator_id == user.id,
        Transaction.transaction_date == today_str,
        Transaction.transaction_type == "Issue"
    ).count()
    op_returned = db.query(Transaction).filter(
        Transaction.operator_id == user.id,
        Transaction.transaction_date == today_str,
        Transaction.transaction_type == "Return"
    ).count()

    result["operator_stats"] = {
        "operator_name": user.full_name or user.username,
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
    try:
        res = return_device_atomic(
            db=db,
            device_serial=payload.serial,
            operator=user,
            condition=payload.condition,
            damage_type=payload.damage_type,
            remarks=payload.remarks or "",
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
    date_str = now.strftime("%Y-%m-%d")

    dmg = DamageReport(
        report_time=now,
        report_date=date_str,
        serial=dev.serial,
        employee_id=emp.employee_id if emp else None,
        employee_name=emp.name if emp else "",
        department=emp.department if emp else getattr(dev, "department", "GENERAL"),
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
    current_ver = get_board_version(db)
    if v and v == current_ver:
        return {"changed": False}
    data = get_live_board_data(db)
    return {"changed": True, "board": data}

class QuickAddDevicePayload(BaseModel):
    serial: str
    make: str = "CHAINWAY"
    model: str = "HHD"
    department: str = "GENERAL"
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
        department=payload.department or "GENERAL",
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
