from pathlib import Path
from fastapi.templating import Jinja2Templates
from app.config import BASE_DIR
from app.timefmt import fmt_time, fmt_datetime
from app.shifts import get_current_shift_and_op_date

def current_shift() -> str:
    return get_current_shift_and_op_date()[0]

def register_jinja_extensions(t: Jinja2Templates) -> Jinja2Templates:
    t.env.filters["time12"] = fmt_time
    t.env.filters["datetime12"] = fmt_datetime
    t.env.globals["current_shift"] = current_shift
    return t

templates = register_jinja_extensions(Jinja2Templates(directory=str(BASE_DIR / "app" / "templates")))

