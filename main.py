import os
import socket
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from app.config import SERVER_HOST, SERVER_PORT, BASE_DIR, DATA_DIR, LOGS_DIR, BACKUP_DIR, DB_PATH
from app.database import engine, Base, SessionLocal, verify_database_state
from app.models import User, Employee, Device
from app.auth import is_initial_setup_required
from app.migration import run_schema_migration
from app.routes import (
    auth_routes, operator_routes, scan_routes,
    admin_routes, report_routes, backup_routes,
    import_routes
)
from app.templating import register_jinja_extensions

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

for tmpl in [
    templates,
    auth_routes.templates,
    operator_routes.templates,
    admin_routes.templates,
    report_routes.templates,
    backup_routes.templates,
    import_routes.templates,
]:
    register_jinja_extensions(tmpl)

def get_lan_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("8.8.8.8", 80))
        default_ip = s.getsockname()[0]
        s.close()
        if default_ip and not default_ip.startswith("127."):
            ips.append(default_ip)
    except Exception:
        pass

    try:
        hostname = socket.gethostname()
        host_ip = socket.gethostbyname(hostname)
        if host_ip and not host_ip.startswith("127.") and host_ip not in ips:
            ips.append(host_ip)
    except Exception:
        pass

    return ips or ["127.0.0.1"]

def seed_initial_data_if_needed():
    """Deprecated: Insecure automatic default credential creation has been permanently removed.
    First-time administrator setup is conducted interactively via /setup or setup_admin.py."""
    return None

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Safely verify database health and existence
    db_state = verify_database_state()
    if db_state["status"] == "CORRUPT":
        print("\n" + "!" * 68)
        print(" [CRITICAL DATABASE ERROR] DATABASE FILE CORRUPTION DETECTED")
        print("!" * 68)
        print(f" Database Location: {db_state.get('db_path', DB_PATH)}")
        print(f" Error: {db_state.get('error', 'Integrity check failed')}")
        print(" System startup aborted to prevent data loss.")
        print(" Recovery: Restore the most recent valid backup from the backups directory.")
        print("!" * 68 + "\n")
        raise RuntimeError(f"Database corruption detected: {db_state.get('error')}")

    if db_state["status"] == "UNAVAILABLE":
        print("\n" + "!" * 68)
        print(" [CRITICAL DATABASE ERROR] DATABASE FILE INACCESSIBLE")
        print("!" * 68)
        print(f" Database Location: {db_state.get('db_path', DB_PATH)}")
        print(f" Error: {db_state.get('error', 'Lock or permission error')}")
        print(" System startup aborted. Check file permissions or ensure another process")
        print(" does not hold an exclusive lock on the database file.")
        print("!" * 68 + "\n")
        raise RuntimeError(f"Database unavailable: {db_state.get('error')}")

    if db_state["status"] == "FRESH":
        # Fresh installation: safely initialize table schema
        Base.metadata.create_all(bind=engine)
    elif db_state["status"] in ("OPERATIONAL", "INITIALIZING"):
        # Existing database: run safe migrations then ensure all tables/indexes exist
        run_schema_migration()
        Base.metadata.create_all(bind=engine)

    # 2. Check if administrator setup is required
    db = SessionLocal()
    try:
        setup_required = is_initial_setup_required(db)
    finally:
        db.close()

    lan_ips = get_lan_ips()
    primary_ip = lan_ips[0]

    if setup_required:
        print("\n" + "=" * 68)
        print(" HHD SERVER HANDOVER SYSTEM — FIRST-TIME SETUP REQUIRED")
        print("=" * 68)
        print(f" Database Location:   {DB_PATH}")
        print(" Status:              Fresh installation detected. No Administrator exists.")
        print(f" Setup URL (Host):    http://127.0.0.1:{SERVER_PORT}/setup")
        print("")
        print(" First-time setup required.")
        print(f" Open http://127.0.0.1:{SERVER_PORT}/setup in your browser to create your Administrator account.")
        print("")
        print(" Note: Operator PCs on LAN will be paused until setup completes.")
        print(f" CLI Alternative:     python setup_admin.py")
        print("=" * 68 + "\n")
    else:
        print("\n" + "=" * 68)
        print(" HHD SERVER HANDOVER SYSTEM — VERSION 3.0 (24/7 CONTINUOUS)")
        print("=" * 68)
        print(f" Database Location:   {DB_PATH}")
        print(f" Local Access:        http://127.0.0.1:{SERVER_PORT}")
        print(f" LAN Access (PCs):    http://{primary_ip}:{SERVER_PORT}")
        print(f" Wall Display Board:  http://{primary_ip}:{SERVER_PORT}/board")
        print(f" Excel Master Import: http://{primary_ip}:{SERVER_PORT}/admin/import")
        print(" Security Status:     Administrator configured · Setup endpoint locked")
        print(" Normal operation:    ACTIVE (Ready for scanning)")
        print("=" * 68 + "\n")
    yield

app = FastAPI(
    title="HHD Device Management System",
    description="24/7 Continuous Warehouse Device Handover & Allocation Management System",
    version="3.0.0",
    lifespan=lifespan
)

# Security Headers & CSP Middleware
@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "frame-ancestors 'none';"
    )
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response

# Mount Static Files
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")

# Include Routers
app.include_router(auth_routes.router)
app.include_router(operator_routes.router)
app.include_router(scan_routes.router)
app.include_router(admin_routes.router)
app.include_router(report_routes.router)
app.include_router(backup_routes.router)
app.include_router(import_routes.router)

# Global Exception Handlers
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    # If 503 on a non-API web request, show the remote setup holding page
    if exc.status_code == 503 and not request.url.path.startswith("/api/"):
        return templates.TemplateResponse(
            request=request,
            name="setup_required_lan.html",
            context={
                "title": "Server Setup Required - HHD System",
                "hide_nav": True,
                "detail": exc.detail
            },
            status_code=503
        )
    if exc.status_code in (301, 302, 303, 307, 308) and exc.headers and "Location" in exc.headers:
        return RedirectResponse(url=exc.headers["Location"], status_code=exc.status_code)
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=exc.status_code, content={"ok": False, "detail": exc.detail, "message": exc.detail})
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=getattr(exc, "headers", None))

@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    if request.url.path.startswith("/api/"):
        detail_msg = getattr(exc, "detail", "Resource not found")
        return JSONResponse(status_code=404, content={"ok": False, "detail": detail_msg, "message": detail_msg})
    return templates.TemplateResponse(request=request, name="base.html", context={
        "title": "Page Not Found",
        "hide_nav": False
    }, status_code=404)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=SERVER_HOST, port=SERVER_PORT, reload=False, workers=1)
