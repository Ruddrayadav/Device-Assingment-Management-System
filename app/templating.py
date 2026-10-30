from pathlib import Path
from fastapi.templating import Jinja2Templates
from app.config import BASE_DIR
from app.timefmt import fmt_time, fmt_datetime
from app.csrf import get_csrf_token_for_request

def register_jinja_extensions(t: Jinja2Templates) -> Jinja2Templates:
    t.env.filters["time12"] = fmt_time
    t.env.filters["datetime12"] = fmt_datetime
    t.env.globals["csrf_token"] = get_csrf_token_for_request
    return t

templates = register_jinja_extensions(Jinja2Templates(directory=str(BASE_DIR / "app" / "templates")))
