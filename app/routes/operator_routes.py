from pathlib import Path
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User, Transaction
from app.auth import get_current_user_optional, require_user
from app.routes.scan_routes import get_live_board_data
from app.config import DAMAGE_CATEGORIES, today_kolkata_str

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

    # Reconstruct operator daily metrics from permanent database records
    today_str = today_kolkata_str()
    op_issued = db.query(Transaction).filter(
        Transaction.operator_id == user.id,
        Transaction.transaction_date == today_str,
        Transaction.transaction_type == "Issue"
    ).count()

    op_returned = db.query(Transaction).filter(
        Transaction.operator_id == user.id,
        Transaction.transaction_date == today_str,
        Transaction.transaction_type == "Return"
    ).count()

    return templates.TemplateResponse(request=request, name="operator.html", context={
        "title": "HHD Scan Station - Fast Allocation & Return",
        "user": user,
        "board": board,
        "op_issued": op_issued,
        "op_returned": op_returned,
        "damage_categories": DAMAGE_CATEGORIES
    })

@router.get("/board", response_class=HTMLResponse)
async def live_wall_board(
    request: Request,
    db: Session = Depends(get_db)
):
    user = get_current_user_optional(request, db)
    if not user:
        return RedirectResponse("/login?next=/board", status_code=302)

    board = get_live_board_data(db)

    return templates.TemplateResponse(request=request, name="board.html", context={
        "title": "HHD Live Display Board",
        "user": user,
        "board": board
    })
