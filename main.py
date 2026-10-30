import os
import socket
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from app.config import SERVER_HOST, SERVER_PORT, BASE_DIR, DATA_DIR, LOGS_DIR, BACKUP_DIR
from app.database import engine, Base, SessionLocal
from app.models import User, Employee, Device
from app.auth import hash_password
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
    """Seeds minimal initial administrator and operators only if database is completely empty."""
    db = SessionLocal()
    try:
        user_count = db.query(User).count()
        now = datetime.now()
        if user_count == 0:
            admin_user = User(
                username="hhdadmin",
                password_hash=hash_password("admin12345"),
                full_name="Warehouse Administrator",
                role="admin",
                active=True,
                created_at=now,
                updated_at=now
            )
            op1 = User(
                username="operator1",
                password_hash=hash_password("operator123"),
                full_name="Rahul (Operator 1)",
                role="operator",
                active=True,
                created_at=now,
                updated_at=now
            )
            op2 = User(
                username="operator2",
                password_hash=hash_password("operator123"),
                full_name="Amit (Operator 2)",
                role="operator",
                active=True,
                created_at=now,
                updated_at=now
            )
            db.add_all([admin_user, op1, op2])
            db.commit()
    finally:
        db.close()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Run safe schema migration for existing databases
    run_schema_migration()
    # 2. Ensure all tables and indexes exist
    Base.metadata.create_all(bind=engine)
    # 3. Seed minimal accounts if fresh
    seed_initial_data_if_needed()

    lan_ips = get_lan_ips()
    primary_ip = lan_ips[0]
    print("\n" + "=" * 68)
    print(" HHD DEVICE MANAGEMENT SYSTEM — VERSION 3.0 (24/7 CONTINUOUS)")
    print("=" * 68)
    print(f" Local Access:        http://127.0.0.1:{SERVER_PORT}")
    print(f" LAN Access (PCs):    http://{primary_ip}:{SERVER_PORT}")
    print(f" Wall Display Board:  http://{primary_ip}:{SERVER_PORT}/board")
    print(f" Excel Master Import: http://{primary_ip}:{SERVER_PORT}/admin/import")
    print(f" Admin Credentials:   Username: hhdadmin / Password: admin12345")
    print(f" Operator 1 Login:    Username: operator1 / Password: operator123")
    print(f" Operator 2 Login:    Username: operator2 / Password: operator123")
    print("=" * 68 + "\n")
    yield

app = FastAPI(
    title="HHD Device Management System",
    description="24/7 Continuous Warehouse Device Handover & Allocation Management System",
    version="3.0.0",
    lifespan=lifespan
)

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

# Global 404 / Error Handlers
@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    if request.url.path.startswith("/api/"):
        return {"ok": False, "message": "Resource not found"}
    return templates.TemplateResponse(request=request, name="base.html", context={
        "title": "Page Not Found",
        "hide_nav": False
    }, status_code=404)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=SERVER_HOST, port=SERVER_PORT, reload=False, workers=1)
