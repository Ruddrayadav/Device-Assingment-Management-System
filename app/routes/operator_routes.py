from pathlib import Path
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User, Transaction, DamageReport, AuditLog, DeviceAssignment
from app.auth import get_current_user_optional, require_user
from app.routes.scan_routes import get_live_board_data
from app.config import DAMAGE_CATEGORIES, today_kolkata_str, now_kolkata

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

    op_damaged = db.query(DamageReport).filter(
        DamageReport.reported_by_id == user.id,
        DamageReport.report_date == today_str
    ).count()

    t0_today = now_kolkata().replace(hour=0, minute=0, second=0, microsecond=0)
    op_rejected = db.query(AuditLog).filter(
        AuditLog.user_id == user.id,
        AuditLog.action == "SCAN_REJECTED",
        AuditLog.timestamp >= t0_today
    ).count()

    outstanding_cnt = db.query(DeviceAssignment).filter(DeviceAssignment.status == "ACTIVE").count()

    # Operator's personal transaction history for today
    op_history = db.query(Transaction).filter(
        Transaction.operator_id == user.id,
        Transaction.transaction_date == today_str
    ).order_by(Transaction.id.desc()).limit(30).all()

    return templates.TemplateResponse(request=request, name="operator.html", context={
        "title": "HHD Scan Station - Fast Allocation & Return",
        "user": user,
        "board": board,
        "op_issued": op_issued,
        "op_returned": op_returned,
        "op_damaged": op_damaged,
        "op_rejected": op_rejected,
        "outstanding_count": outstanding_cnt,
        "operator_history": op_history,
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
