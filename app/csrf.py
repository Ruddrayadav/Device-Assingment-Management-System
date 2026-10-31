import hmac
import hashlib
import time
from typing import Optional
from fastapi import Request, HTTPException, status
from app.config import SECRET_KEY

def generate_csrf_token(user_identifier: str) -> str:
    """
    Generates a HMAC-based CSRF token tied to the user session identifier.
    Valid for 24 hours.
    """
    timestamp = str(int(time.time()))
    message = f"{user_identifier}:{timestamp}"
    signature = hmac.new(SECRET_KEY.encode(), message.encode(), hashlib.sha256).hexdigest()
    return f"{timestamp}:{signature}"

def verify_csrf_token(token: Optional[str], user_identifier: str, max_age_seconds: int = 86400) -> bool:
    """
    Verifies that the CSRF token is authentic, untampered, and not expired.
    """
    if not token or ":" not in token:
        return False

    try:
        ts_str, signature = token.split(":", 1)
        token_time = int(ts_str)
        if time.time() - token_time > max_age_seconds:
            return False  # Expired

        message = f"{user_identifier}:{ts_str}"
        expected = hmac.new(SECRET_KEY.encode(), message.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
    except Exception:
        return False

def get_csrf_token_for_request(request: Request) -> str:
    """Helper to generate a CSRF token for the current session in templates."""
    session_cookie = request.cookies.get("hhd_session", "anonymous")
    return generate_csrf_token(session_cookie)

