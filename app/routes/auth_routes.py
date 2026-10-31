import asyncio
from datetime import datetime
from fastapi import APIRouter, Request, Depends, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User, AuditLog
from app.auth import verify_password, sign_session, get_current_user_optional, login_limiter
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

router = APIRouter()

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, db: Session = Depends(get_db)):
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
    username_clean = username.strip()
    client_ip = request.client.host if request.client else "127.0.0.1"

    # 1. Check brute-force lockout
    is_locked, secs = login_limiter.is_locked_out(username_clean.lower())
    if not is_locked:
        is_locked, secs = login_limiter.is_locked_out(client_ip)

    if is_locked:
        return templates.TemplateResponse(request=request, name="login.html", context={
            "title": "Sign In - HHD System",
            "error": f"Too many failed login attempts. Account temporarily locked for {secs} seconds. Please wait or contact an administrator to unlock immediately.",
            "username": username_clean
        }, status_code=status.HTTP_429_TOO_MANY_REQUESTS)

    user = db.query(User).filter(User.username == username_clean, User.active == True).first()

    if not user or not verify_password(password, user.password_hash):
        # Record failure for both username and IP
        attempts_u, lock_u = login_limiter.record_failure(username_clean.lower())
        attempts_ip, lock_ip = login_limiter.record_failure(client_ip)

        # Progressive backoff delay
        if attempts_u >= 3:
            await asyncio.sleep(min(2.0, 0.5 * (attempts_u - 2)))

        # Audit log failed attempt
        try:
            audit = AuditLog(
                timestamp=datetime.now(),
                user_id=user.id if user else None,
                username=username_clean,
                action="LOGIN_FAILED",
                target_type="AUTH",
                target_id=username_clean,
                details=f"Failed login attempt from IP {client_ip} (Attempt #{attempts_u})"
            )
            db.add(audit)
            db.commit()
        except Exception:
            db.rollback()

        if lock_u > 0 or lock_ip > 0:
            err_msg = f"Account locked for {lock_u or lock_ip} seconds due to repeated failed attempts. Contact administrator to unlock."
            status_code = status.HTTP_429_TOO_MANY_REQUESTS
        else:
            remaining = 5 - attempts_u
            err_msg = f"Invalid username or password. ({remaining} attempt{'s' if remaining != 1 else ''} remaining before temporary lockout)"
            status_code = status.HTTP_400_BAD_REQUEST

        return templates.TemplateResponse(request=request, name="login.html", context={
            "title": "Sign In - HHD System",
            "error": err_msg,
            "username": username_clean
        }, status_code=status_code)

    # Valid login: reset rate limiter
    login_limiter.reset(username_clean.lower())
    login_limiter.reset(client_ip)

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
    token = sign_session(session_data, max_age_seconds=43200)

    target_redirect = next_url if (next_url and next_url.startswith("/") and not next_url.startswith("//")) else ("/" if user.role == "operator" else "/admin")
    resp = RedirectResponse(target_redirect, status_code=status.HTTP_302_FOUND)
    resp.set_cookie(
        key="hhd_session",
        value=token,
        max_age=43200,
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
