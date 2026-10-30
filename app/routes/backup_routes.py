from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates

from app.models import User
from app.auth import require_admin
from app.backup import create_safe_backup, list_backups, restore_backup
from app.config import BACKUP_DIR

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

router = APIRouter(prefix="/admin/backups")

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def backup_page(
    request: Request,
    admin: User = Depends(require_admin)
):
    backups = list_backups()
    return templates.TemplateResponse(request=request, name="admin/backups.html", context={
        "title": "Database Backups & Restore - Admin",
        "user": admin,
        "backups": backups
    })

@router.post("/create")
async def trigger_backup(
    custom_tag: Optional[str] = Form(""),
    admin: User = Depends(require_admin)
):
    try:
        res = create_safe_backup(custom_tag.strip())
        return RedirectResponse(f"/admin/backups?msg=Backup+created+successfully:+{res['filename']}", status_code=302)
    except Exception as ex:
        return RedirectResponse(f"/admin/backups?error=Backup+failed:+{str(ex)[:80]}", status_code=302)

@router.post("/restore")
async def trigger_restore(
    filename: str = Form(...),
    admin: User = Depends(require_admin)
):
    try:
        restore_backup(filename.strip())
        return RedirectResponse(f"/admin/backups?msg=Database+restored+successfully+from+{filename}", status_code=302)
    except Exception as ex:
        return RedirectResponse(f"/admin/backups?error=Restore+failed:+{str(ex)[:80]}", status_code=302)

@router.get("/download/{filename}")
async def download_backup(
    filename: str,
    admin: User = Depends(require_admin)
):
    file_path = BACKUP_DIR / filename
    if not file_path.exists() or not filename.endswith(".db"):
        raise HTTPException(status_code=404, detail="Backup file not found.")
    return FileResponse(
        path=str(file_path),
        media_type="application/octet-stream",
        filename=filename
    )
