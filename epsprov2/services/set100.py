from datetime import datetime, timedelta

import requests
from flask import current_app, request

from .. import config
from ..db import (
    get_db_connection,
    get_set100_sync_status,
    save_set100_sync_status,
)
from ..utils import now_iso, parse_iso_datetime


def fetch_set100_symbols():
    session_client = requests.Session()
    session_client.headers.update(config.REQUEST_HEADERS)

    page_response = session_client.get(config.SET100_SOURCE_PAGE_URL, timeout=20)
    page_response.raise_for_status()

    api_response = session_client.get(
        config.SET100_SOURCE_API_URL,
        timeout=20,
        headers={
            "Referer": config.SET100_SOURCE_PAGE_URL,
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    api_response.raise_for_status()

    payload = api_response.json()
    stock_infos = payload.get("composition", {}).get("stockInfos", [])
    symbols = []
    for item in stock_infos:
        raw_ticker = item.get("symbol")
        if not raw_ticker:
            continue
        ticker = str(raw_ticker).upper().strip()
        if not config.TICKER_PATTERN.fullmatch(ticker):
            continue
        ticker = config.SET100_TICKER_ALIASES.get(ticker, ticker)
        symbols.append(ticker)

    unique_symbols = sorted(set(symbols))
    min_expected = current_app.config.get(
        "SET100_MIN_EXPECTED_SYMBOLS", config.SET100_MIN_EXPECTED_SYMBOLS
    )
    if len(unique_symbols) < min_expected:
        raise RuntimeError(f"ดึงรายชื่อ SET100 ได้ไม่ครบ ({len(unique_symbols)} รายการ)")

    return unique_symbols, config.SET100_SOURCE_API_URL


def sync_set100_list(mode="manual"):
    started_at = now_iso()
    try:
        symbols, source_url = fetch_set100_symbols()

        conn = get_db_connection()
        try:
            existing_rows = conn.execute(
                "SELECT symbol FROM stocks WHERE is_set100 = 1"
            ).fetchall()
            existing_set100 = {row["symbol"] for row in existing_rows}

            new_set100 = set(symbols)
            added_symbols = sorted(new_set100 - existing_set100)
            removed_symbols = sorted(existing_set100 - new_set100)

            for symbol in new_set100:
                conn.execute(
                    """
                    INSERT INTO stocks (symbol, is_manual, is_set100)
                    VALUES (?, 0, 1)
                    ON CONFLICT(symbol) DO UPDATE SET is_set100 = 1
                    """,
                    (symbol,),
                )

            for symbol in removed_symbols:
                conn.execute("UPDATE stocks SET is_set100 = 0 WHERE symbol = ?", (symbol,))

            conn.execute("DELETE FROM stocks WHERE is_manual = 0 AND is_set100 = 0")

            status = {
                "last_synced_at": started_at,
                "last_success_at": started_at,
                "last_status": "success",
                "last_message": f"ซิงก์ SET100 สำเร็จ ({len(symbols)} รายการ)",
                "source_url": source_url,
                "total_symbols": len(symbols),
                "added_count": len(added_symbols),
                "removed_count": len(removed_symbols),
                "mode": mode,
            }

            save_set100_sync_status(status, conn=conn, commit=False)
            conn.commit()
            return status
        finally:
            conn.close()
    except Exception as exc:
        current_app.logger.warning("SET100 sync failed: %s", exc)
        current_status = get_set100_sync_status()
        current_status.update(
            {
                "last_synced_at": started_at,
                "last_status": "error",
                "last_message": str(exc),
                "mode": mode,
            }
        )
        save_set100_sync_status(current_status)
        raise


def maybe_auto_sync_set100():
    if request.method != "GET" or request.endpoint not in {"web.index", "api.api_data"}:
        return

    status = get_set100_sync_status()
    last_success = parse_iso_datetime(status.get("last_success_at"))
    interval = current_app.config.get(
        "SET100_AUTO_SYNC_INTERVAL_SECONDS", config.SET100_AUTO_SYNC_INTERVAL_SECONDS
    )
    if last_success and datetime.now(last_success.tzinfo) - last_success < timedelta(
        seconds=interval
    ):
        return

    if getattr(current_app, "set100_auto_sync_running", False):
        return

    try:
        current_app.set100_auto_sync_running = True
        sync_set100_list(mode="auto")
    except Exception:
        pass
    finally:
        current_app.set100_auto_sync_running = False
