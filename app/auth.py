import hmac
import hashlib
import json
import base64
import time
from typing import Optional, Dict, Any
from fastapi import Request, HTTPException, status, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from app.config import SECRET_KEY
from app.database import get_db
from app.models import User

# Password hashing using PBKDF2-HMAC-SHA256 (standard library, zero native dependency risk on Windows)
def hash_password(password: str) -> str:
    salt = hashlib.sha256(str(time.time()).encode()).hexdigest()[:16]
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200000)
    return f"pbkdf2_sha256$200000${salt}${derived.hex()}"

def verify_password(plain_password: str, hashed: str) -> bool:
    if not hashed:
        return False
    # Check for Werkzeug style hash if legacy
    if hashed.startswith("pbkdf2:sha256:") or hashed.startswith("scrypt:"):
        try:
            from werkzeug.security import check_password_hash
            return check_password_hash(hashed, plain_password)
        except Exception:
            pass
    # Custom PBKDF2 format
    if hashed.startswith("pbkdf2_sha256$"):
        try:
            parts = hashed.split("$")
            iterations = int(parts[1])
            salt = parts[2]
            stored_hash = parts[3]
            derived = hashlib.pbkdf2_hmac("sha256", plain_password.encode("utf-8"), salt.encode("utf-8"), iterations)
            return hmac.compare_digest(derived.hex(), stored_hash)
        except Exception:
            return False
    # Check if bcrypt
    if hashed.startswith("$2a$") or hashed.startswith("$2b$"):
        try:
            import bcrypt
            return bcrypt.checkpw(plain_password.encode("utf-8"), hashed.encode("utf-8"))
        except Exception:
            return False
    return False

# Session token encoder & decoder (HMAC SHA-256)
def sign_session(data: Dict[str, Any], max_age_seconds: int = 43200) -> str:
    payload = {
        "data": data,
        "exp": int(time.time()) + max_age_seconds
    }
    raw = json.dumps(payload, separators=(',', ':')).encode("utf-8")
    b64_payload = base64.urlsafe_b64encode(raw).decode("utf-8")
    signature = hmac.new(SECRET_KEY.encode("utf-8"), b64_payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{b64_payload}.{signature}"

def decode_session(token: str) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    token = token.strip('"')
    if "." not in token:
        return None
    try:
        b64_payload, signature = token.rsplit(".", 1)
        expected_sig = hmac.new(SECRET_KEY.encode("utf-8"), b64_payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected_sig):
            return None
        raw = base64.urlsafe_b64decode(b64_payload.encode("utf-8")).decode("utf-8")
        payload = json.loads(raw)
        if time.time() > payload.get("exp", 0):
            return None
        return payload.get("data")
    except Exception:
        return None

def get_current_user_optional(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    token = request.cookies.get("hhd_session")
    if not token:
        return None
    data = decode_session(token)
    if not data or "username" not in data:
        return None
    user = db.query(User).filter(User.username == data["username"], User.active == True).first()
    return user

def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = get_current_user_optional(request, db)
    if not user:
        if request.url.path.startswith("/api/"):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session expired or not authenticated. Please log in."
            )
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": f"/login?next={request.url.path}"}
        )
    return user

def require_admin(request: Request, user: User = Depends(require_user)) -> User:
    if user.role != "admin":
        if request.url.path.startswith("/api/"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Administrator access required."
            )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access required."
        )
    return user

