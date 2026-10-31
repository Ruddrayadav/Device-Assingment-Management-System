import hmac
import hashlib
import json
import base64
import time
from typing import Optional, Dict, Any, Tuple
import threading
from fastapi import Request, HTTPException, status, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from app.config import SECRET_KEY
from app.database import get_db
from app.models import User

# Brute-force & Rate Limiting Protection
class LoginRateLimiter:
    def __init__(self, max_attempts: int = 5, lockout_window_sec: int = 300):
        self.max_attempts = max_attempts
        self.lockout_window_sec = lockout_window_sec
        self._lock = threading.Lock()
        self._store: Dict[str, Dict[str, Any]] = {}

    def _clean_expired(self, now: float):
        keys_to_del = []
        for k, v in self._store.items():
            if now > v.get("locked_until", 0) and (now - v.get("first_attempt", 0) > self.lockout_window_sec):
                keys_to_del.append(k)
        for k in keys_to_del:
            self._store.pop(k, None)

    def is_locked_out(self, key: str) -> Tuple[bool, int]:
        now = time.time()
        with self._lock:
            self._clean_expired(now)
            entry = self._store.get(key)
            if not entry:
                return False, 0
            locked_until = entry.get("locked_until", 0)
            if now < locked_until:
                return True, max(1, int(locked_until - now))
            return False, 0

    def record_failure(self, key: str) -> Tuple[int, int]:
        now = time.time()
        with self._lock:
            self._clean_expired(now)
            entry = self._store.get(key)
            if not entry or (now - entry.get("first_attempt", 0) > self.lockout_window_sec and now > entry.get("locked_until", 0)):
                entry = {"attempts": 1, "first_attempt": now, "locked_until": 0}
                self._store[key] = entry
            else:
                entry["attempts"] += 1

            attempts = entry["attempts"]
            if attempts >= self.max_attempts:
                entry["locked_until"] = now + self.lockout_window_sec
                return attempts, self.lockout_window_sec
            return attempts, 0

    def reset(self, key: str):
        with self._lock:
            self._store.pop(key, None)

    def unlock_user(self, username: str):
        with self._lock:
            self._store.pop(username.lower().strip(), None)

login_limiter = LoginRateLimiter(max_attempts=5, lockout_window_sec=300)


# Password hashing using PBKDF2-HMAC-SHA256 (standard library, zero native dependency risk on Windows)
def hash_password(password: str) -> str:
    salt = hashlib.sha256(str(time.time()).encode()).hexdigest()[:16]
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200000)
    return f"pbkdf2_sha256$200000${salt}${derived.hex()}"

def verify_password(plain_password: str, hashed: str) -> bool:
    if not hashed:
        return False
    # Check for PBKDF2 style hash (Werkzeug / Flask / PBKDF2) - Pure Standard Library
    if hashed.startswith("pbkdf2:sha256:"):
        try:
            parts = hashed.split("$")
            if len(parts) == 3:
                method_part, salt, hashval = parts
                iterations = int(method_part.split(":")[-1])
                derived = hashlib.pbkdf2_hmac("sha256", plain_password.encode("utf-8"), salt.encode("utf-8"), iterations)
                return hmac.compare_digest(derived.hex(), hashval)
        except Exception:
            pass

    # Check for Scrypt style hash (Pure Standard Library)
    if hashed.startswith("scrypt:"):
        try:
            parts = hashed.split("$")
            if len(parts) == 3:
                method_part, salt, hashval = parts
                subparts = method_part.split(":")
                n = int(subparts[1]) if len(subparts) > 1 else 32768
                r = int(subparts[2]) if len(subparts) > 2 else 8
                p = int(subparts[3]) if len(subparts) > 3 else 1
                derived = hashlib.scrypt(plain_password.encode("utf-8"), salt=salt.encode("utf-8"), n=n, r=r, p=p)
                return hmac.compare_digest(derived.hex(), hashval)
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

