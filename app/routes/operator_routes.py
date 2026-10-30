from pathlib import Path
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User
from app.auth import get_current_user_optional, require_user
from app.routes.scan_routes import get_live_board_data
from app.config import DAMAGE_CATEGORIES

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

router = APIRouter()

@router.get("/", response_class=HTMLResponse)
async def operator_station(
    request: Request,
    db: Session = Depends(get_db)
):
    user = get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login?next=/", status_code=302)

    board = get_live_board_data(db)

    return templates.TemplateResponse(request=request, name="operator.html", context={
        "title": "HHD Scan Station - Fast Allocation & Return",
        "user": user,
        "board": board,
        "damage_categories": DAMAGE_CATEGORIES
    })

@router.get("/board", response_class=HTMLResponse)
async def live_wall_board(
    request: Request,
    db: Session = Depends(get_db)
):
    user = get_current_user_optional(request, db)
    board = get_live_board_data(db)

    return templates.TemplateResponse(request=request, name="board.html", context={
        "title": "HHD Live Display Board",
        "user": user,
        "board": board
    })
