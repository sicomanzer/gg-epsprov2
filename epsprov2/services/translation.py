import random
import time

import yfinance as yf
from deep_translator import GoogleTranslator
from flask import current_app

from ..db import get_translation_db, save_translation_db
from .stock_data import normalize_ticker


def get_description_th(ticker):
    clean = normalize_ticker(ticker)
    if not clean:
        return None

    cached = get_translation_db(clean)
    if cached:
        return cached

    symbol = clean if "." in clean else f"{clean}.BK"
    info = yf.Ticker(symbol).info
    description_en = info.get("longBusinessSummary") or "-"

    if not current_app.config.get("ENABLE_TRANSLATION", True):
        return description_en

    if description_en == "-" or len(description_en) < 10:
        return description_en

    max_chars = int(current_app.config.get("TRANSLATION_MAX_CHARS", 4500))
    text_to_translate = description_en[:max_chars]

    time.sleep(random.uniform(0.05, 0.25))
    description_th = GoogleTranslator(source="auto", target="th").translate(text_to_translate)
    save_translation_db(clean, description_th)
    return description_th

