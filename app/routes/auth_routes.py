import asyncio
import re
from datetime import datetime
from fastapi import APIRouter, Request, Depends, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.database import get_db, SCAN_WRITE_LOCK
from app.models import User, AuditLog, SystemSetting
from app.auth import (
    verify_password, hash_password, sign_session, get_current_user_optional,
    login_limiter, is_initial_setup_required, is_local_client, ROLE_ADMIN
)
from app.config import now_kolkata
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

router = APIRouter()

@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request, db: Session = Depends(get_db)):
    # 1. Enforce local access only from central host server
    if not is_local_client(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Initial administrator setup is restricted to local access directly on the central Windows server (127.0.0.1). Unauthenticated setup cannot be accessed from remote LAN computers."
        )

    # 2. Check if setup is already completed
    if not is_initial_setup_required(db):
        return RedirectResponse("/login?msg=Initial+setup+has+already+been+completed.+Please+sign+in.", status_code=status.HTTP_302_FOUND)
    
    return templates.TemplateResponse(request=request, name="setup.html", context={
        "title": "Initial Administrator Setup - HHD System",
        "hide_nav": True,
        "error": None,
        "username": "",
        "full_name": ""
    })

@router.post("/setup", response_class=HTMLResponse)
async def setup_submit(
    request: Request,
    username: str = Form(...),
    full_name: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db)
):
    # 1. Enforce local access only
    if not is_local_client(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Initial administrator setup is restricted to local access directly on the central Windows server (127.0.0.1). Remote LAN setup registration is strictly blocked."
        )

    # 2. Check if setup is already completed
    if not is_initial_setup_required(db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Initial setup has already been completed. Unauthorized registration attempt blocked."
        )

    uname = username.strip()
    fname = full_name.strip()
    client_ip = request.client.host if request.client else "127.0.0.1"

    error_msg = None
    if not fname:
        error_msg = "Administrator Full Name is required."
    elif len(uname) < 3 or len(uname) > 64:
        error_msg = "Username must be between 3 and 64 characters."
    elif not re.match(r"^[a-zA-Z0-9_\-\.]+$", uname):
        error_msg = "Username may only contain letters, numbers, underscores, hyphens, and periods."
    elif len(password) < 8:
        error_msg = "Password must be at least 8 characters long."
    elif password != confirm_password:
        error_msg = "Passwords do not match. Please verify your password confirmation."
    elif password.lower() == uname.lower():
        error_msg = "Password cannot be identical to the username."
    elif password.lower() in ("password", "12345678", "admin12345", "operator123"):
        error_msg = "Password is too common or insecure. Please choose a stronger password."
    else:
        existing = db.query(User).filter(func.lower(User.username) == uname.lower()).first()
        if existing:
            error_msg = f"User '{uname}' already exists. Please choose a different username."

    if error_msg:
        return templates.TemplateResponse(request=request, name="setup.html", context={
            "title": "Initial Administrator Setup - HHD System",
            "hide_nav": True,
            "error": error_msg,
            "username": uname,
            "full_name": fname
        }, status_code=status.HTTP_400_BAD_REQUEST)

    # 3. Thread-safe lock to prevent concurrent first admin race condition
    with SCAN_WRITE_LOCK:
        if not is_initial_setup_required(db):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Initial setup has already been completed. Unauthorized registration attempt blocked."
            )

        existing = db.query(User).filter(func.lower(User.username) == uname.lower()).first()
        if existing:
            return templates.TemplateResponse(request=request, name="setup.html", context={
                "title": "Initial Administrator Setup - HHD System",
                "hide_nav": True,
                "error": f"User '{uname}' already exists. Please choose a different username.",
                "username": uname,
                "full_name": fname
            }, status_code=status.HTTP_400_BAD_REQUEST)

        now = now_kolkata()
        pwd_hash = hash_password(password)
        admin_user = User(
            username=uname,
            password_hash=pwd_hash,
            full_name=fname,
            role=ROLE_ADMIN,  # Automatically receives Admin role
            active=True,
            created_at=now,
            updated_at=now
        )
        db.add(admin_user)
        db.flush()

        # Mark first-time setup permanently completed in system settings
        setting = db.query(SystemSetting).filter(SystemSetting.key == "setup_completed").first()
        if not setting:
            setting = SystemSetting(
                key="setup_completed",
                value="true",
                description="First-time setup completed flag",
                updated_at=now
            )
            db.add(setting)
        else:
            setting.value = "true"
            setting.updated_at = now

        # Audit Log: Record initial Admin creation without inventing an authenticated actor
        audit = AuditLog(
            timestamp=now,
            user_id=None,
            username=uname,
            role=ROLE_ADMIN,
            action="INITIAL_ADMIN_SETUP",
            target_type="SECURITY",
            target_id=str(admin_user.id),
            details=f"Primary administrator account initialized during first-time setup from IP {client_ip}.",
            status="SUCCESS"
        )
        db.add(audit)
        db.commit()

    # Create authenticated session
    session_data = {
        "user_id": admin_user.id,
        "username": admin_user.username,
        "role": admin_user.role,
        "full_name": admin_user.full_name
    }
    token = sign_session(session_data, max_age_seconds=43200)

    resp = RedirectResponse("/admin?msg=Primary+administrator+account+successfully+created.+Welcome+to+HHD+System.", status_code=status.HTTP_302_FOUND)
    resp.set_cookie(
        key="hhd_session",
        value=token,
        max_age=43200,
        httponly=True,
        samesite="lax",
        path="/"
    )
    return resp

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, db: Session = Depends(get_db)):
    if is_initial_setup_required(db):
        if is_local_client(request):
            return RedirectResponse("/setup", status_code=status.HTTP_302_FOUND)
        else:
            return templates.TemplateResponse(
                request=request,
                name="setup_required_lan.html",
                context={
                    "title": "Server Setup Required - HHD System",
                    "hide_nav": True
                },
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE
            )

    user = get_current_user_optional(request, db)
    if user:
        if user.role == "admin":
            return RedirectResponse("/admin", status_code=status.HTTP_302_FOUND)
        return RedirectResponse("/", status_code=status.HTTP_302_FOUND)
    
    return templates.TemplateResponse(request=request, name="login.html", context={
        "title": "Sign In - HHD System",
        "error": None
    })

@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next_url: str = Form("/"),
    db: Session = Depends(get_db)
):
    if is_initial_setup_required(db):
        return RedirectResponse("/setup", status_code=status.HTTP_302_FOUND)
    username_clean = username.strip()
    client_ip = request.client.host if request.client else "127.0.0.1"

    # 1. Check brute-force lockout (username + client IP pair, and high IP limit)
    is_locked, secs, lock_reason = login_limiter.is_locked_out_pair(username_clean.lower(), client_ip)

    if is_locked:
        lock_detail = "from this PC" if lock_reason == "ip" else "for this account on this PC"
        return templates.TemplateResponse(request=request, name="login.html", context={
            "title": "Sign In - HHD System",
            "error": f"Too many failed login attempts {lock_detail}. Temporarily locked for {secs} seconds. Please wait or contact an administrator to unlock immediately.",
            "username": username_clean
        }, status_code=status.HTTP_429_TOO_MANY_REQUESTS)

    user = db.query(User).filter(User.username == username_clean, User.active == True).first()

    if not user or not verify_password(password, user.password_hash):
        # Record failure for both (username, IP) pair and IP-only limit
        attempts_pair, lock_pair, is_ip_locked = login_limiter.record_failure_pair(username_clean.lower(), client_ip)

        # Progressive backoff delay
        if attempts_pair >= 3:
            await asyncio.sleep(min(2.0, 0.5 * (attempts_pair - 2)))

        # Audit log failed attempt
        try:
            audit = AuditLog(
                timestamp=datetime.now(),
                user_id=user.id if user else None,
                username=username_clean,
                action="LOGIN_FAILED",
                target_type="AUTH",
                target_id=username_clean,
                details=f"Failed login attempt from IP {client_ip} (Attempt #{attempts_pair})"
            )
            db.add(audit)
            db.commit()
        except Exception:
            db.rollback()

        if lock_pair > 0 or is_ip_locked:
            err_msg = f"Account locked for {lock_pair or 300} seconds due to repeated failed attempts. Contact administrator to unlock."
            status_code = status.HTTP_429_TOO_MANY_REQUESTS
        else:
            remaining = 5 - attempts_pair
            err_msg = f"Invalid username or password. ({remaining} attempt{'s' if remaining != 1 else ''} remaining before temporary lockout)"
            status_code = status.HTTP_400_BAD_REQUEST

        return templates.TemplateResponse(request=request, name="login.html", context={
            "title": "Sign In - HHD System",
            "error": err_msg,
            "username": username_clean
        }, status_code=status_code)

    # Valid login: reset rate limiter pair
    login_limiter.reset_pair(username_clean.lower(), client_ip)

    # Audit log successful login
    try:
        audit = AuditLog(
            timestamp=datetime.now(),
            user_id=user.id,
            username=user.username,
            action="LOGIN_SUCCESS",
            target_type="AUTH",
            target_id=str(user.id),
            details=f"Successful login from IP {client_ip} (Role: {user.role})"
        )
        db.add(audit)
        db.commit()
    except Exception:
        db.rollback()

    session_data = {
        "user_id": user.id,
        "username": user.username,
        "role": user.role,
        "full_name": user.full_name or user.username
    }
    # Role "display" receives a 30-day sliding session
    max_age_seconds = 30 * 86400 if user.role == "display" else 43200
    token = sign_session(session_data, max_age_seconds=max_age_seconds)

    if user.role == "display":
        target_redirect = "/board"
    elif user.role == "admin":
        target_redirect = next_url if (next_url and next_url != "/" and next_url.startswith("/") and not next_url.startswith("//")) else "/admin"
    else:
        target_redirect = next_url if (next_url and next_url.startswith("/") and not next_url.startswith("//")) else "/"

    resp = RedirectResponse(target_redirect, status_code=status.HTTP_302_FOUND)
    resp.set_cookie(
        key="hhd_session",
        value=token,
        max_age=max_age_seconds,
        httponly=True,
        samesite="lax",
        path="/"
    )
    return resp


@router.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=status.HTTP_302_FOUND)
    resp.delete_cookie(key="hhd_session", path="/")
    return resp
