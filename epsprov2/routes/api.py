from concurrent.futures import ThreadPoolExecutor

from flask import Blueprint, current_app, jsonify

from ..db import get_set100_sync_status, load_stock_records
from ..services.stock_data import get_stock_data
from ..services.translation import get_description_th


api_bp = Blueprint("api", __name__)


@api_bp.route("/health")
def health_check():
    sync_status = get_set100_sync_status()
    return jsonify(
        {"status": "ok", "vercel": bool(current_app.config.get("IS_VERCEL")), "set100_sync": sync_status}
    )


@api_bp.route("/api/stocks")
def api_stocks():
    stocks = load_stock_records()
    return jsonify(stocks)


@api_bp.route("/api/data")
def api_data():
    stock_records = load_stock_records()
    stocks = [row["symbol"] for row in stock_records]
    stock_flags = {row["symbol"]: row for row in stock_records}

    max_workers = max(1, min(int(current_app.config.get("API_MAX_WORKERS", 1)), len(stocks) or 1))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = list(executor.map(lambda symbol: get_stock_data(symbol, include_description=False), stocks))

    for item in results:
        flags = stock_flags.get(item.get("symbol"), {})
        item["is_manual"] = bool(flags.get("is_manual", 0))
        item["is_set100"] = bool(flags.get("is_set100", 0))
        if item["is_manual"] and item["is_set100"]:
            item["source"] = "both"
        elif item["is_set100"]:
            item["source"] = "set100"
        elif item["is_manual"]:
            item["source"] = "manual"
        else:
            item["source"] = "unknown"

    return jsonify(results)


@api_bp.route("/api/description/<ticker>")
def api_description(ticker):
    try:
        description = get_description_th(ticker)
        if description is None:
            return jsonify({"symbol": ticker, "description": "-"}), 400
        return jsonify({"symbol": ticker, "description": description})
    except Exception as exc:
        current_app.logger.warning("Unable to fetch description for %s: %s", ticker, exc)
        return jsonify({"symbol": ticker, "description": "-"}), 500

