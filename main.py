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
from app.routes import (
    auth_routes, operator_routes, scan_routes,
    admin_routes, report_routes, backup_routes
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
    db = SessionLocal()
    try:
        # Seed Admin & Operator if no users exist
        user_count = db.query(User).count()
        now = datetime.now()
        if user_count == 0:
            admin_user = User(
                username="hhdadmin",
                password_hash=hash_password("admin12345"),
                full_name="Warehouse Administrator",
                role="admin",
                default_shift="Morning",
                active=True,
                created_at=now,
                updated_at=now
            )
            op1 = User(
                username="operator1",
                password_hash=hash_password("operator123"),
                full_name="Morning Operator",
                role="operator",
                default_shift="Morning",
                active=True,
                created_at=now,
                updated_at=now
            )
            op2 = User(
                username="operator2",
                password_hash=hash_password("operator123"),
                full_name="Evening Operator",
                role="operator",
                default_shift="Evening",
                active=True,
                created_at=now,
                updated_at=now
            )
            db.add_all([admin_user, op1, op2])

        # Seed initial employees if needed
        sample_emps = [
            ("1222905", "Ajay Kumar", "IB Puter"),
            ("1332772", "Sonu", "IB Puter"),
            ("1334506", "Rajkumar", "IB Receiver"),
            ("1331118", "Sunny", "RTO Puter"),
            ("1266377", "Ravinder", "RTO Associate"),
            ("1205341", "Sanjeev Kumar", "Dispatch Scanner"),
            ("1282781", "Bhupender", "RTO Puter"),
            ("1208400", "Sukhbir Singh", "Dispatch Scanner"),
            ("1192873", "Parmjeet", "Dispatch Scanner"),
            ("1163679", "Rajender", "Dispatch Scanner")
        ]
        for eid, ename, edept in sample_emps:
            if not db.query(Employee).filter(Employee.employee_id == eid).first():
                db.add(Employee(
                    employee_id=eid,
                    name=ename,
                    department=edept,
                    active=True,
                    created_at=now,
                    updated_at=now
                ))

        # Seed initial devices if needed
        sample_serials = [
            "HC660DE260502008", "HC660DE260501682", "HC660DE260504219", "HC660DE260501783",
            "HC660DE260501876", "HC660DE260501990", "HC660DE260501274", "HC660DE260401747",
            "HC660DE260501998", "HC660DE260501980"
        ]
        for s in sample_serials:
            if not db.query(Device).filter(Device.serial == s).first():
                db.add(Device(
                    serial=s,
                    make="CHAINWAY",
                    model="HHD",
                    cover_qty=2,
                    status="Available",
                    condition="Working",
                    notes="Initial warehouse inventory",
                    created_at=now,
                    updated_at=now
                ))

        db.commit()
    finally:
        db.close()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Initialize tables
    Base.metadata.create_all(bind=engine)
    # 2. Seed default data if fresh database
    seed_initial_data_if_needed()

    lan_ips = get_lan_ips()
    primary_ip = lan_ips[0]
    print("\n" + "=" * 65)
    print(" HHD DEVICE MANAGEMENT SYSTEM — SERVER ACTIVE")
    print("=" * 65)
    print(f" Local Access:        http://127.0.0.1:{SERVER_PORT}")
    print(f" LAN Access (PCs):    http://{primary_ip}:{SERVER_PORT}")
    print(f" Wall Display Board:  http://{primary_ip}:{SERVER_PORT}/board")
    print(f" Admin Credentials:   Username: hhdadmin / Password: admin12345")
    print(f" Operator 1 Login:    Username: operator1 / Password: operator123")
    print(f" Operator 2 Login:    Username: operator2 / Password: operator123")
    print("=" * 65 + "\n")
    yield

app = FastAPI(
    title="HHD Device Management System",
    description="High-Speed Warehouse Device Handover & Takeover Management System",
    version="2.0.0",
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
