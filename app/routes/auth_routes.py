from fastapi import APIRouter, Request, Depends, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User
from app.auth import verify_password, sign_session, get_current_user_optional
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
    user = db.query(User).filter(User.username == username_clean, User.active == True).first()

    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(request=request, name="login.html", context={
            "title": "Sign In - HHD System",
            "error": "Invalid username or password.",
            "username": username_clean
        }, status_code=status.HTTP_400_BAD_REQUEST)

    # Valid login: create signed session
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
