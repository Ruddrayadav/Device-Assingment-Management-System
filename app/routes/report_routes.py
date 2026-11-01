import csv
import io
import os
import uuid
import asyncio
from datetime import datetime
from typing import Optional, Dict, Any
from pathlib import Path
from fastapi import APIRouter, Request, Depends, Query, BackgroundTasks, HTTPException, status
from fastapi.responses import Response, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db, SessionLocal
from app.models import User, Device, Employee, Transaction, DamageReport
from app.auth import require_inventory_manager, ROLE_ADMIN, ROLE_INVENTORY_MANAGER
from app.jobs import job_manager
from app.reports import generate_excel_report
from app.timefmt import fmt_datetime
from app.config import now_kolkata

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

router = APIRouter()

def _excel_export_worker(output_file_path: str, params: Dict[str, Any]):
    """Thread worker executed inside JobManager semaphore concurrency control."""
    db = SessionLocal()
    try:
        excel_stream = generate_excel_report(
            db,
            start_date=params.get("start_date"),
            end_date=params.get("end_date"),
            operator=params.get("operator"),
            employee=params.get("employee"),
            serial=params.get("serial"),
            department=params.get("department"),
            transaction_type=params.get("transaction_type"),
            condition=params.get("condition"),
            all_history=params.get("all_history", False)
        )
        with open(output_file_path, "wb") as f:
            f.write(excel_stream.getvalue())
    finally:
        db.close()

@router.get("/admin/reports", response_class=HTMLResponse)
async def reports_page(
    request: Request,
    user: User = Depends(require_inventory_manager),
    db: Session = Depends(get_db)
):
    operators = db.query(User.full_name).filter(User.full_name != "").distinct().all()
    departments = db.query(Employee.department).filter(Employee.department != "").distinct().all()
    return templates.TemplateResponse(request=request, name="admin/reports.html", context={
        "title": "Reports & Analytics - Admin",
        "user": user,
        "operators": [o[0] for o in operators if o[0]],
        "departments": [d[0] for d in departments if d[0]]
    })

@router.get("/reports/excel")
async def download_excel_report(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    operator: Optional[str] = Query(None),
    employee: Optional[str] = Query(None),
    serial: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    transaction_type: Optional[str] = Query(None),
    condition: Optional[str] = Query(None),
    all_history: bool = Query(False),
    user: User = Depends(require_inventory_manager),
    db: Session = Depends(get_db)
):
    """
    Direct Excel report download executed in a worker thread.
    Does not block the asyncio event loop and uses read-only queries.
    """
    excel_stream = await asyncio.to_thread(
        generate_excel_report,
        db,
        start_date=start_date,
        end_date=end_date,
        operator=operator,
        employee=employee,
        serial=serial,
        department=department,
        transaction_type=transaction_type,
        condition=condition,
        all_history=all_history
    )
    timestamp = now_kolkata().strftime("%Y%m%d_%H%M")
    filename = f"HHD_Warehouse_Report_{timestamp}.xlsx"

    return Response(
        content=excel_stream.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

@router.post("/api/reports/async/generate")
@router.post("/api/reports/jobs")
async def create_async_report_job(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    operator: Optional[str] = Query(None),
    employee: Optional[str] = Query(None),
    serial: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    transaction_type: Optional[str] = Query(None),
    condition: Optional[str] = Query(None),
    all_history: bool = Query(False),
    user: User = Depends(require_inventory_manager)
):
    """Starts a non-blocking background report generation job managed by thread pool."""
    timestamp = now_kolkata().strftime("%Y%m%d_%H%M%S")
    download_filename = f"HHD_Warehouse_Report_{timestamp}.xlsx"
    params = {
        "start_date": start_date,
        "end_date": end_date,
        "operator": operator,
        "employee": employee,
        "serial": serial,
        "department": department,
        "transaction_type": transaction_type,
        "condition": condition,
        "all_history": all_history
    }
    job = job_manager.submit_job(
        user_id=user.id,
        job_type="EXCEL_REPORT",
        download_filename=download_filename,
        worker_func=_excel_export_worker,
        params=params
    )
    return {
        "ok": True,
        "job_id": job.job_id,
        "status": job.status.lower(),
        "status_url": f"/api/reports/async/status/{job.job_id}",
        "download_url": f"/api/reports/async/download/{job.job_id}"
    }

@router.get("/api/reports/async/status/{job_id}")
@router.get("/api/reports/jobs/{job_id}")
async def get_report_job_status(
    job_id: str,
    user: User = Depends(require_inventory_manager)
):
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Report job not found.")
    info = job.to_dict()
    info["ok"] = job.status != "FAILED"
    # Legacy compatibility status key
    info["status"] = job.status.lower() if job.status != "COMPLETED" else "completed"
    if job.status == "COMPLETED":
        info["download_url"] = f"/api/reports/async/download/{job_id}"
    return info

@router.get("/api/reports/async/download/{job_id}")
@router.get("/api/reports/jobs/{job_id}/download")
async def download_report_job_file(
    job_id: str,
    user: User = Depends(require_inventory_manager)
):
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Report job not found.")
    if job.status != "COMPLETED" or not job.file_path or not os.path.exists(job.file_path):
        if job.status == "FAILED":
            raise HTTPException(status_code=500, detail=f"Report generation failed: {job.error_message}")
        raise HTTPException(status_code=400, detail="Report is not ready for download.")

    with open(job.file_path, "rb") as f:
        data = f.read()

    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{job.download_filename}"'}
    )

@router.get("/reports/csv/devices")
async def export_devices_csv(
    user: User = Depends(require_inventory_manager),
    db: Session = Depends(get_db)
):
    devices = db.query(Device).order_by(Device.serial.asc()).all()
    emp_map = {e.employee_id: e.name for e in db.query(Employee.employee_id, Employee.name).all()}
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Serial", "Make", "Model", "Department", "Cover Qty", "Status", "Condition", "Assigned Employee ID", "Assigned Employee Name", "Notes", "Updated At"])

    for d in devices:
        holder_name = emp_map.get(d.assigned_employee_id, "") if d.assigned_employee_id else ""
        writer.writerow([
            d.serial, d.make, d.model, getattr(d, "department", "") or "",
            d.cover_qty, d.status, d.condition,
            d.assigned_employee_id or "", holder_name,
            d.notes, fmt_datetime(d.updated_at)
        ])

    csv_data = ("\ufeff" + output.getvalue()).encode("utf-8")
    return Response(
        content=csv_data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="HHD_Device_Inventory.csv"'}
    )

@router.get("/reports/csv/employees")
async def export_employees_csv(
    user: User = Depends(require_inventory_manager),
    db: Session = Depends(get_db)
):
    employees = db.query(Employee).order_by(Employee.name.asc()).all()
    held_by_emp = dict(
        db.query(Device.assigned_employee_id, func.count(Device.serial))
        .filter(Device.status == "Issued", Device.assigned_employee_id.isnot(None))
        .group_by(Device.assigned_employee_id).all()
    )
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Employee ID", "Name", "Department", "Active", "Assigned Devices Count"])

    for e in employees:
        writer.writerow([e.employee_id, e.name, e.department, "Yes" if e.active else "No", held_by_emp.get(e.employee_id, 0)])

    csv_data = ("\ufeff" + output.getvalue()).encode("utf-8")
    return Response(
        content=csv_data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="HHD_Employee_Master.csv"'}
    )

@router.get("/reports/csv/transactions")
async def export_transactions_csv(
    user: User = Depends(require_inventory_manager),
    db: Session = Depends(get_db)
):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["TX ID", "Exact Time", "Date", "Action", "Device Serial", "Employee ID", "Employee Name", "Department", "Operator", "Condition", "Covers", "Remarks"])

    for t in db.query(Transaction).order_by(Transaction.id.desc()).yield_per(1000):
        writer.writerow([
            t.id, fmt_datetime(t.transaction_time),
            t.transaction_date, t.transaction_type, t.serial,
            t.employee_id, t.employee_name, t.department, t.operator_name,
            t.condition, t.cover_qty, t.remarks
        ])

    csv_data = ("\ufeff" + output.getvalue()).encode("utf-8")
    return Response(
        content=csv_data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="HHD_Transaction_Audit.csv"'}
    )

@router.get("/reports/csv/damage")
async def export_damage_csv(
    user: User = Depends(require_inventory_manager),
    db: Session = Depends(get_db)
):
    dmgs = db.query(DamageReport).order_by(DamageReport.id.desc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Report ID", "Report Time", "Date", "Device Serial", "Employee ID", "Employee Name", "Department", "Fault Type", "Record Source", "Details", "Reported By", "Review Status", "Resolution", "Resolved At"])

    for d in dmgs:
        writer.writerow([
            d.id, fmt_datetime(d.report_time),
            getattr(d, "report_date", d.report_time.strftime("%Y-%m-%d")),
            d.serial, d.employee_id or "", d.employee_name or "",
            d.department or "", d.fault_type,
            getattr(d, "source", "Operator Report"),
            d.details, d.reported_by_name,
            d.review_status, d.resolution_notes or "",
            fmt_datetime(d.resolved_at) if d.resolved_at else ""
        ])

    csv_data = ("\ufeff" + output.getvalue()).encode("utf-8")
    return Response(
        content=csv_data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="HHD_Damage_Reports.csv"'}
    )

