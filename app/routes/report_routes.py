import csv
import io
import uuid
import asyncio
from datetime import datetime
from typing import Optional, Dict, Any
from pathlib import Path
from fastapi import APIRouter, Request, Depends, Query, BackgroundTasks, HTTPException, status
from fastapi.responses import Response, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db, SessionLocal
from app.models import User, Device, Employee, Transaction, DamageReport
from app.auth import require_admin
from app.reports import generate_excel_report
from app.timefmt import fmt_datetime
from app.config import now_kolkata

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

router = APIRouter()

# In-memory store for async report generation jobs
REPORT_JOBS: Dict[str, Dict[str, Any]] = {}

def _run_report_job(job_id: str, params: Dict[str, Any]):
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
        REPORT_JOBS[job_id]["status"] = "completed"
        REPORT_JOBS[job_id]["data"] = excel_stream.getvalue()
        REPORT_JOBS[job_id]["completed_at"] = now_kolkata()
    except Exception as ex:
        REPORT_JOBS[job_id]["status"] = "failed"
        REPORT_JOBS[job_id]["error"] = str(ex)
    finally:
        db.close()

@router.get("/admin/reports", response_class=HTMLResponse)
async def reports_page(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    operators = db.query(User.full_name).filter(User.full_name != "").distinct().all()
    departments = db.query(Device.department).distinct().all()
    return templates.TemplateResponse(request=request, name="admin/reports.html", context={
        "title": "Reports & Analytics - Admin",
        "user": admin,
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
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """
    Direct Excel report download executed in a worker thread.
    Does not block the asyncio event loop and uses read-only batched queries.
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

@router.post("/api/reports/jobs")
async def create_report_job(
    background_tasks: BackgroundTasks,
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    operator: Optional[str] = Query(None),
    employee: Optional[str] = Query(None),
    serial: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    transaction_type: Optional[str] = Query(None),
    condition: Optional[str] = Query(None),
    all_history: bool = Query(False),
    admin: User = Depends(require_admin)
):
    """Starts a non-blocking background report generation job."""
    job_id = str(uuid.uuid4())
    REPORT_JOBS[job_id] = {
        "status": "processing",
        "created_at": now_kolkata(),
        "data": None,
        "error": None
    }
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
    background_tasks.add_task(_run_report_job, job_id, params)
    return {"ok": True, "job_id": job_id, "status": "processing"}

@router.get("/api/reports/jobs/{job_id}")
async def get_report_job_status(
    job_id: str,
    admin: User = Depends(require_admin)
):
    job = REPORT_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Report job not found.")
    return {
        "job_id": job_id,
        "status": job["status"],
        "error": job.get("error"),
        "download_url": f"/api/reports/jobs/{job_id}/download" if job["status"] == "completed" else None
    }

@router.get("/api/reports/jobs/{job_id}/download")
async def download_report_job_file(
    job_id: str,
    admin: User = Depends(require_admin)
):
    job = REPORT_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Report job not found.")
    if job["status"] != "completed" or not job["data"]:
        raise HTTPException(status_code=400, detail="Report is not ready for download.")

    timestamp = now_kolkata().strftime("%Y%m%d_%H%M")
    filename = f"HHD_Warehouse_Report_{timestamp}.xlsx"
    return Response(
        content=job["data"],
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

@router.get("/reports/csv/devices")
async def export_devices_csv(
    admin: User = Depends(require_admin),
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
            d.serial, d.make, d.model, getattr(d, "department", "GENERAL"),
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
    admin: User = Depends(require_admin),
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
    admin: User = Depends(require_admin),
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
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    dmgs = db.query(DamageReport).order_by(DamageReport.id.desc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Report ID", "Report Time", "Date", "Device Serial", "Employee ID", "Employee Name", "Department", "Fault Type", "Details", "Reported By", "Review Status", "Resolution", "Resolved At"])

    for d in dmgs:
        writer.writerow([
            d.id, fmt_datetime(d.report_time),
            getattr(d, "report_date", d.report_time.strftime("%Y-%m-%d")),
            d.serial, d.employee_id or "", d.employee_name or "",
            d.department or "", d.fault_type, d.details, d.reported_by_name,
            d.review_status, d.resolution_notes or "",
            fmt_datetime(d.resolved_at) if d.resolved_at else ""
        ])

    csv_data = ("\ufeff" + output.getvalue()).encode("utf-8")
    return Response(
        content=csv_data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="HHD_Damage_Reports.csv"'}
    )
