import io
import json
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus
from fastapi import APIRouter, Request, Depends, Form, File, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.auth import require_admin
from app.excel_templates import generate_device_inventory_template, generate_employee_master_template
from app.excel_importer import (
    validate_device_import, commit_device_import,
    validate_employee_import, commit_employee_import
)

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

router = APIRouter(prefix="/admin/import")

# In-memory staging cache for pending import previews (cleaned by session or time)
PREVIEW_CACHE = {}

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def import_dashboard(
    request: Request,
    admin: User = Depends(require_admin)
):
    return templates.TemplateResponse(request=request, name="admin/import.html", context={
        "title": "Excel Master Import Wizard - Admin",
        "user": admin,
        "msg": request.query_params.get("msg"),
        "error": request.query_params.get("error")
    })

@router.get("/template/device")
async def download_device_template(
    admin: User = Depends(require_admin)
):
    buf = generate_device_inventory_template()
    filename = "Device_Inventory_Template.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

@router.get("/template/employee")
async def download_employee_template(
    admin: User = Depends(require_admin)
):
    buf = generate_employee_master_template()
    filename = "Employee_Master_Template.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

@router.post("/preview", response_class=HTMLResponse)
async def preview_import(
    request: Request,
    file: UploadFile = File(...),
    import_type: str = Form(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    filename = file.filename or "upload"
    ext = Path(filename).suffix.lower()
    if ext not in [".xlsx", ".csv"]:
        return RedirectResponse(
            f"/admin/import?error={quote_plus('Invalid file type. Please upload a .xlsx or .csv file.')}",
            status_code=302
        )

    file_bytes = await file.read()
    if len(file_bytes) == 0:
        return RedirectResponse(
            f"/admin/import?error={quote_plus('Uploaded file is empty (0 bytes).')}",
            status_code=302
        )

    if import_type == "device":
        preview = validate_device_import(db, file_bytes, filename)
    elif import_type == "employee":
        preview = validate_employee_import(db, file_bytes, filename)
    else:
        return RedirectResponse(
            f"/admin/import?error={quote_plus('Invalid import type specified.')}",
            status_code=302
        )

    if not preview.get("ok"):
        return RedirectResponse(
            f"/admin/import?error={quote_plus(preview.get('error', 'Validation failed.'))}",
            status_code=302
        )

    # Store in preview cache with token
    preview_id = str(uuid.uuid4())
    PREVIEW_CACHE[preview_id] = {
        "import_type": import_type,
        "filename": filename,
        "valid_rows": preview["valid_rows"],
        "summary": preview
    }

    return templates.TemplateResponse(request=request, name="admin/import_preview.html", context={
        "title": f"Review Import: {filename} - Admin",
        "user": admin,
        "preview_id": preview_id,
        "import_type": import_type,
        "preview": preview
    })

@router.post("/commit")
async def commit_import(
    preview_id: str = Form(...),
    mode: str = Form("UPSERT"),
    confirm_replace: Optional[str] = Form(None),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    cached = PREVIEW_CACHE.get(preview_id)
    if not cached:
        return RedirectResponse(
            f"/admin/import?error={quote_plus('Import session expired or not found. Please upload the file again.')}",
            status_code=302
        )

    import_type = cached["import_type"]
    valid_rows = cached["valid_rows"]

    if mode == "REPLACE" and confirm_replace != "yes":
        return RedirectResponse(
            f"/admin/import?error={quote_plus('Explicit confirmation is required to REPLACE master data.')}",
            status_code=302
        )

    try:
        if import_type == "device":
            res = commit_device_import(db, valid_rows, mode=mode)
        else:
            res = commit_employee_import(db, valid_rows, mode=mode)

        # Clear cache entry
        del PREVIEW_CACHE[preview_id]

        msg = (
            f"Successfully imported {res['total_imported']} {import_type} records "
            f"({res['inserted']} new added, {res['updated']} updated) via {mode} mode."
        )
        return RedirectResponse(f"/admin/import?msg={quote_plus(msg)}", status_code=302)

    except ValueError as ex:
        return RedirectResponse(f"/admin/import?error={quote_plus(str(ex))}", status_code=302)
    except Exception as ex:
        return RedirectResponse(f"/admin/import?error={quote_plus(f'Import error: {str(ex)[:100]}')}", status_code=302)

