from flask import Blueprint, current_app, redirect, render_template, request, url_for

from ..db import (
    add_stock_db,
    clear_all_manual_stocks_db,
    get_set100_sync_status,
    load_stock_records,
    remove_stock_db,
)
from ..security import get_csrf_token, is_valid_csrf_token
from ..services.set100 import sync_set100_list
from ..services.stock_data import normalize_ticker, parse_tickers


web_bp = Blueprint("web", __name__)


def build_redirect(message, level="success"):
    return redirect(url_for("web.index", message=message, level=level))


@web_bp.route("/")
def index():
    stocks = load_stock_records()
    sync_status = get_set100_sync_status()
    return render_template(
        "index.html",
        stocks=stocks,
        sync_status=sync_status,
        csrf_token=get_csrf_token(),
        grade_a_min_score=current_app.config["GRADE_A_MIN_SCORE"],
        sniper_min_mos=current_app.config["SNIPER_MIN_MOS"],
        status_message=request.args.get("message"),
        status_level=request.args.get("level", "success"),
    )


@web_bp.route("/add", methods=["POST"])
def add_stock():
    if not is_valid_csrf_token():
        return build_redirect("คำขอไม่ถูกต้อง กรุณาลองใหม่อีกครั้ง", "danger")

    raw_input = request.form.get("ticker")
    if not raw_input:
        return build_redirect("กรุณากรอกรายชื่อหุ้นที่ต้องการเพิ่ม", "warning")

    tickers, invalid_tickers = parse_tickers(raw_input)
    if not tickers:
        return build_redirect("ไม่พบรหัสหุ้นที่ถูกต้อง", "warning")

    added_count = 0
    for ticker in tickers:
        if add_stock_db(ticker):
            added_count += 1

    if invalid_tickers:
        current_app.logger.info("Ignored invalid tickers: %s", ", ".join(invalid_tickers))

    message = f"บันทึกรายชื่อหุ้น {added_count} รายการ"
    if invalid_tickers:
        message += f" และข้ามข้อมูลที่ไม่ถูกต้อง {len(invalid_tickers)} รายการ"
    return build_redirect(message, "success")


@web_bp.route("/remove/<ticker>", methods=["POST"])
def remove_stock(ticker):
    if not is_valid_csrf_token():
        return build_redirect("คำขอไม่ถูกต้อง กรุณาลองใหม่อีกครั้ง", "danger")

    clean_ticker = normalize_ticker(ticker)
    if not clean_ticker:
        return build_redirect("รหัสหุ้นไม่ถูกต้อง", "warning")

    if remove_stock_db(clean_ticker):
        return build_redirect(f"ลบหุ้น {clean_ticker} ออกจากรายการส่วนตัวเรียบร้อยแล้ว", "success")
    return build_redirect(f"ไม่พบหุ้น {clean_ticker} ในรายการส่วนตัว", "warning")


@web_bp.route("/clear_all", methods=["POST"])
def clear_all_stocks():
    if not is_valid_csrf_token():
        return build_redirect("คำขอไม่ถูกต้อง กรุณาลองใหม่อีกครั้ง", "danger")

    if clear_all_manual_stocks_db():
        return build_redirect("ลบรายชื่อหุ้นที่เพิ่มเองทั้งหมดเรียบร้อยแล้ว", "success")
    return build_redirect("ไม่สามารถลบรายชื่อหุ้นทั้งหมดได้", "danger")


@web_bp.route("/sync_set100", methods=["POST"])
def sync_set100():
    if not is_valid_csrf_token():
        return build_redirect("คำขอไม่ถูกต้อง กรุณาลองใหม่อีกครั้ง", "danger")

    try:
        status = sync_set100_list(mode="manual")
        message = (
            f"ซิงก์ SET100 สำเร็จ {status['total_symbols']} รายการ "
            f"(เพิ่ม {status['added_count']} / เอาออก {status['removed_count']})"
        )
        return build_redirect(message, "success")
    except Exception as exc:
        return build_redirect(f"ซิงก์ SET100 ไม่สำเร็จ: {exc}", "danger")

