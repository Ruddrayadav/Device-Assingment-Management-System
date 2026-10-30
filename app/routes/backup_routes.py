import os
import re
import secrets
from pathlib import Path
from urllib.parse import quote_plus
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Request, Depends, Form, File, UploadFile, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from app.models import User
from app.auth import require_admin
from app.config import BACKUP_DIR, MAX_UPLOAD_MB
from app.backup import (
    create_safe_backup, list_backups, restore_backup,
    delete_backup, safe_backup_path, get_live_db_stats,
    validate_uploaded_db
)

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

router = APIRouter(prefix="/admin/backups")

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def backup_page(
    request: Request,
    admin: User = Depends(require_admin)
):
    live_db = get_live_db_stats()
    backups = list_backups()
    return templates.TemplateResponse(request=request, name="admin/backups.html", context={
        "title": "Database Files - Admin",
        "user": admin,
        "live_db": live_db,
        "backups": backups,
        "msg": request.query_params.get("msg"),
        "error": request.query_params.get("error")
    })

@router.post("/create")
async def trigger_backup(
    custom_tag: Optional[str] = Form(""),
    admin: User = Depends(require_admin)
):
    try:
        res = create_safe_backup(custom_tag.strip() if custom_tag else "")
        fname = res["filename"]
        return RedirectResponse(f"/admin/backups?msg={quote_plus(f'Backup snapshot created: {fname}')}", status_code=302)
    except Exception as ex:
        return RedirectResponse(f"/admin/backups?error={quote_plus(f'Backup failed: {str(ex)[:100]}')}", status_code=302)

@router.post("/restore")
async def trigger_restore(
    filename: str = Form(...),
    admin: User = Depends(require_admin)
):
    try:
        safe_backup_path(filename.strip())
        restore_backup(filename.strip())
        return RedirectResponse(f"/admin/backups?msg={quote_plus(f'Database successfully restored from {filename}')}", status_code=302)
    except ValueError as ex:
        return RedirectResponse(f"/admin/backups?error={quote_plus(str(ex))}", status_code=302)
    except Exception as ex:
        return RedirectResponse(f"/admin/backups?error={quote_plus(f'Restore failed: {str(ex)[:100]}')}", status_code=302)

@router.post("/delete")
async def trigger_delete(
    filename: str = Form(...),
    admin: User = Depends(require_admin)
):
    try:
        safe_backup_path(filename.strip())
        delete_backup(filename.strip())
        return RedirectResponse(f"/admin/backups?msg={quote_plus(f'Backup file {filename} deleted successfully.')}", status_code=302)
    except ValueError as ex:
        return RedirectResponse(f"/admin/backups?error={quote_plus(str(ex))}", status_code=302)
    except Exception as ex:
        return RedirectResponse(f"/admin/backups?error={quote_plus(f'Delete failed: {str(ex)[:100]}')}", status_code=302)

@router.post("/upload")
async def upload_backup(
    file: UploadFile = File(...),
    admin: User = Depends(require_admin)
):
    original_name = file.filename or ""
    ext = Path(original_name).suffix.lower()
    if ext not in [".db", ".sqlite", ".sqlite3"]:
        return RedirectResponse(
            f"/admin/backups?error={quote_plus('Invalid file type. Only .db, .sqlite, and .sqlite3 files are allowed.')}",
            status_code=302
        )

    os.makedirs(BACKUP_DIR, exist_ok=True)
    temp_filename = f".upload_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(6)}.part"
    temp_path = BACKUP_DIR / temp_filename

    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    total_bytes = 0
    chunk_size = 1024 * 1024  # 1 MB

    try:
        with open(temp_path, "wb") as f:
            while True:
                chunk = await file.read(chunk_size)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise ValueError(f"Uploaded file exceeds size limit of {MAX_UPLOAD_MB} MB.")
                f.write(chunk)
    except Exception as ex:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass
        return RedirectResponse(
            f"/admin/backups?error={quote_plus(f'Upload failed: {str(ex)}')}",
            status_code=302
        )

    if total_bytes == 0:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass
        return RedirectResponse(
            f"/admin/backups?error={quote_plus('Uploaded file is empty (0 bytes).')}",
            status_code=302
        )

    # Heavy verification run in threadpool
    is_valid, err_msg = await run_in_threadpool(validate_uploaded_db, temp_path)
    if not is_valid:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass
        return RedirectResponse(
            f"/admin/backups?error={quote_plus(f'Database validation failed: {err_msg}')}",
            status_code=302
        )

    # Sanitize name and store as uploaded_<YYYYMMDD_HHMMSS>_<sanitized-name>.db
    raw_stem = Path(original_name).stem
    clean_stem = re.sub(r'[^a-zA-Z0-9_\-]', '_', raw_stem) or "uploaded"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    final_filename = f"uploaded_{ts}_{clean_stem}.db"
    final_path = BACKUP_DIR / final_filename
    counter = 1
    while final_path.exists():
        final_filename = f"uploaded_{ts}_{clean_stem}_{counter}.db"
        final_path = BACKUP_DIR / final_filename
        counter += 1

    temp_path.rename(final_path)
    return RedirectResponse(
        f"/admin/backups?msg={quote_plus(f'Database file uploaded successfully: {final_filename}')}",
        status_code=302
    )

@router.get("/download/{filename}")
async def download_backup(
    filename: str,
    admin: User = Depends(require_admin)
):
    try:
        file_path = safe_backup_path(filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Backup file not found.")

    return FileResponse(
        path=str(file_path),
        media_type="application/octet-stream",
        filename=filename
    )
