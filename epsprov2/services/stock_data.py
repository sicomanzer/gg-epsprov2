import json
import os
import re
from datetime import datetime
from functools import lru_cache

import pandas as pd
import requests
import yfinance as yf

from .. import config

try:
    from thaifin import Stock as ThaiFinStock

    THAIFIN_AVAILABLE = True
except Exception:
    ThaiFinStock = None
    THAIFIN_AVAILABLE = False


FINNOMENA_BASE_URL = "https://www.finnomena.com/market-info/api/public"
EPS_CACHE_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "eps_history_cache.json")


def _log_warning(message, exc=None):
    try:
        from flask import current_app

        if exc is not None:
            current_app.logger.warning("%s: %s", message, exc)
        else:
            current_app.logger.warning("%s", message)
    except Exception:
        return


@lru_cache(maxsize=1)
def _load_packaged_eps_cache():
    try:
        with open(EPS_CACHE_FILE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def get_eps_cache_diagnostics(sample_symbols=None, current_year=None):
    cache = _load_packaged_eps_cache()
    if current_year is None:
        current_year = datetime.now().year
    years_eps = [current_year - config.EPS_TREND_YEARS + i for i in range(config.EPS_TREND_YEARS)]
    sample_symbols = sample_symbols or ["AAV", "PTT", "KBANK"]

    samples = {}
    for symbol in sample_symbols:
        series = _build_eps_series_from_year_map(symbol, years_eps)
        samples[symbol] = {
            "non_null_years": sum(value is not None for value in series),
            "series": series,
        }

    return {
        "file_path": EPS_CACHE_FILE,
        "file_exists": os.path.exists(EPS_CACHE_FILE),
        "symbol_count": len(cache),
        "sample_symbols": samples,
    }


def normalize_ticker(raw_ticker):
    if not raw_ticker:
        return None

    clean_ticker = str(raw_ticker).upper().strip()
    if not config.TICKER_PATTERN.fullmatch(clean_ticker):
        return None
    return clean_ticker


def parse_tickers(raw_input):
    valid = []
    invalid = []

    for value in re.split(r"[,\s\n]+", raw_input or ""):
        if not value.strip():
            continue

        ticker = normalize_ticker(value)
        if ticker:
            valid.append(ticker)
        else:
            invalid.append(value.strip())

    return sorted(set(valid)), invalid


def build_empty_stock_payload(ticker, error="Data Unavailable"):
    return {
        "symbol": ticker,
        "name": ticker,
        "price": "-",
        "pe_trailing": "-",
        "pe_forward": "-",
        "market_cap": "-",
        "dividend_yield": "-",
        "dividend_rate": "-",
        "ddm_value": "-",
        "ddm_k": "-",
        "graham_number": "-",
        "lynch_value": "-",
        "fair_value": "-",
        "peg": "-",
        "target_price": "-",
        "rsi": "-",
        "mos": "-",
        "beta": "-",
        "high_52": "-",
        "low_52": "-",
        "bvps": "-",
        "revenue_growth": "-",
        "ebitda_growth": "-",
        "eps_trend": [None] * config.EPS_TREND_YEARS,
        "div_trend": [0.0] * config.DIV_TREND_YEARS,
        "score": 0,
        "grade": "D",
        "score_details": [],
        "dividend_score": 0,
        "dividend_grade": "D",
        "dividend_score_details": [],
        "dividend_cut_count_10y": 0,
        "dividend_cagr_5y": "-",
        "dividend_cagr_10y": "-",
        "payout_ratio": "-",
        "dividend_safety_score": 0,
        "is_dividend_safe": False,
        "is_value_trap": False,
        "error": error,
        "details": {
            "roa": "-",
            "roe": "-",
            "gross_margin": "-",
            "operating_margin": "-",
            "profit_margin": "-",
            "debt_to_equity": "-",
            "current_ratio": "-",
            "quick_ratio": "-",
            "book_value": "-",
            "price_to_book": "-",
            "industry": "-",
            "sector": "-",
            "description": error,
            "payout_ratio": "-",
            "dividend_cagr_5y": "-",
            "dividend_cagr_10y": "-",
            "dividend_cut_count_10y": 0,
            "dividend_safety_score": 0,
        },
    }


def get_thaifin_symbol(ticker):
    return config.THAIFIN_TICKER_ALIASES.get(ticker, ticker)


def _build_eps_series_from_year_map(ticker, years_eps):
    cache = _load_packaged_eps_cache()
    alias = get_thaifin_symbol(ticker)
    yearly_map = cache.get(str(ticker).upper()) or cache.get(str(alias).upper()) or {}
    eps_trend = [None] * len(years_eps)
    for idx, year in enumerate(years_eps):
        value = yearly_map.get(str(year))
        if value is None:
            continue
        try:
            eps_trend[idx] = float(value)
        except Exception:
            eps_trend[idx] = None
    return eps_trend


@lru_cache(maxsize=2)
def _get_finnomena_security_id_map(cache_day):
    response = requests.get(
        f"{FINNOMENA_BASE_URL}/stock/list",
        params={"exchange": "TH"},
        headers=config.REQUEST_HEADERS,
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    items = payload.get("data") or []
    mapping = {}
    for item in items:
        name = item.get("name")
        security_id = item.get("security_id")
        if name and security_id:
            mapping[str(name).upper()] = str(security_id)
    return mapping


@lru_cache(maxsize=4096)
def _get_finnomena_yearly_eps(security_id, cache_day):
    response = requests.get(
        f"{FINNOMENA_BASE_URL}/stock/summary/{security_id}",
        headers=config.REQUEST_HEADERS,
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("data") or []
    yearly = {}
    for row in rows:
        if row.get("quarter") != 9:
            continue
        fiscal = row.get("fiscal")
        if fiscal is None:
            continue
        try:
            year = int(fiscal)
        except Exception:
            continue
        value = row.get("earning_per_share")
        if value is None:
            yearly[year] = None
            continue
        try:
            yearly[year] = float(value)
        except Exception:
            yearly[year] = None
    return yearly


@lru_cache(maxsize=512)
def get_eps_trend_from_thaifin(ticker, current_year):
    years_eps = [current_year - config.EPS_TREND_YEARS + i for i in range(config.EPS_TREND_YEARS)]
    eps_trend = [None] * config.EPS_TREND_YEARS

    eps_trend = _build_eps_series_from_year_map(ticker, years_eps)
    if any(val is not None for val in eps_trend):
        return tuple(years_eps), tuple(eps_trend)

    cache_day = datetime.utcnow().strftime("%Y%m%d")
    try:
        mapping = _get_finnomena_security_id_map(cache_day)
        security_id = mapping.get(get_thaifin_symbol(ticker).upper()) or mapping.get(str(ticker).upper())
        if security_id:
            yearly_eps = _get_finnomena_yearly_eps(security_id, cache_day)
            for idx, year in enumerate(years_eps):
                value = yearly_eps.get(year)
                if value is not None:
                    eps_trend[idx] = float(value)
            if any(val is not None for val in eps_trend):
                return tuple(years_eps), tuple(eps_trend)
    except Exception as exc:
        _log_warning("Finnomena EPS trend failed", exc)

    if not THAIFIN_AVAILABLE:
        return tuple(years_eps), tuple(eps_trend)

    try:
        thaifin_stock = ThaiFinStock(get_thaifin_symbol(ticker))
        yearly_df = thaifin_stock.yearly_dataframe
        if yearly_df is None or yearly_df.empty or "earning_per_share" not in yearly_df.columns:
            return tuple(years_eps), tuple(eps_trend)

        yearly_eps = yearly_df["earning_per_share"].to_dict()
        for idx, year in enumerate(years_eps):
            period_value = yearly_eps.get(pd.Period(str(year), freq="Y"))
            if pd.notna(period_value):
                eps_trend[idx] = float(period_value)
        return tuple(years_eps), tuple(eps_trend)
    except Exception as exc:
        _log_warning("ThaiFin EPS trend failed", exc)
        return tuple(years_eps), tuple(eps_trend)


def get_stock_data(ticker, include_description=False):
    try:
        if not normalize_ticker(ticker):
            return build_empty_stock_payload(ticker, "Invalid ticker")

        symbol = ticker if "." in ticker else f"{ticker}.BK"
        stock = yf.Ticker(symbol)
        info = stock.info

        def get_val(key, default="-"):
            val = info.get(key, default)
            if val is None:
                return default
            return val

        def get_float(key, multiplier=1.0, default="-"):
            val = info.get(key)
            if val is None:
                return default
            try:
                return float(val) * multiplier
            except Exception:
                return default

        current_year = datetime.now().year

        years_eps, eps_trend = get_eps_trend_from_thaifin(ticker, current_year)
        eps_trend = list(eps_trend)

        if not any(val is not None for val in eps_trend):
            financials = stock.financials
            eps_trend = [None] * config.EPS_TREND_YEARS

            if "Basic EPS" in financials.index:
                eps_series = financials.loc["Basic EPS"]
                eps_dict = {d.year: float(v) for d, v in eps_series.items() if not pd.isna(v)}

                eps_trend = []
                for y in years_eps:
                    val = eps_dict.get(y)
                    if val is None and y == current_year - 1:
                        val = get_val("trailingEps", None)
                        if val == "-":
                            val = None
                    eps_trend.append(val)
            else:
                eps_trend = [None] * config.EPS_TREND_YEARS
                t_eps = get_val("trailingEps", None)
                if t_eps != "-":
                    eps_trend[-1] = t_eps

        div_trend = []
        computed_div_rate = 0.0
        dividends = stock.dividends
        if not dividends.empty:
            div_yearly = dividends.resample("YE").sum()
            div_dict = {ts.year: val for ts, val in div_yearly.items()}

            start_year = current_year - config.DIV_TREND_YEARS
            for y in range(start_year, current_year):
                div_trend.append(float(div_dict.get(y, 0.0) or 0.0))

            try:
                now_tz = pd.Timestamp.now(tz=dividends.index.tz) if dividends.index.tz is not None else pd.Timestamp.now()
                ttm_cutoff = now_tz - pd.Timedelta(days=365)
                ttm_div_series = dividends[dividends.index >= ttm_cutoff]
                if not ttm_div_series.empty:
                    computed_div_rate = float(ttm_div_series.sum())
            except Exception:
                computed_div_rate = 0.0

            if computed_div_rate <= 0:
                computed_div_rate = float(div_dict.get(current_year - 1, 0.0) or 0.0)
        else:
            div_trend = [0.0] * config.DIV_TREND_YEARS

        yf_div_rate = get_val("dividendRate", "-")
        try:
            yf_div_rate_float = float(yf_div_rate) if yf_div_rate != "-" else 0.0
        except Exception:
            yf_div_rate_float = 0.0

        final_div_rate = computed_div_rate if computed_div_rate > 0 else (yf_div_rate_float if yf_div_rate_float > 0 else "-")

        if dividends.empty and final_div_rate != "-" and final_div_rate > 0:
            div_trend[-1] = float(final_div_rate)

        payout_ratio = "-"
        try:
            trailing_eps = get_float("trailingEps")
            if trailing_eps != "-" and trailing_eps > 0 and final_div_rate != "-" and final_div_rate >= 0:
                payout_ratio = round((float(final_div_rate) / float(trailing_eps)) * 100, 2)
        except Exception:
            pass

        debt_to_equity = get_val("debtToEquity", "-")
        try:
            bs = stock.balance_sheet
            if not bs.empty:
                latest_col = bs.iloc[:, 0]

                total_liab = None
                if "Total Liabilities Net Minority Interest" in bs.index:
                    total_liab = latest_col["Total Liabilities Net Minority Interest"]
                elif "Total Liabilities" in bs.index:
                    total_liab = latest_col["Total Liabilities"]

                equity = None
                if "Stockholders Equity" in bs.index:
                    equity = latest_col["Stockholders Equity"]
                elif "Total Stockholder Equity" in bs.index:
                    equity = latest_col["Total Stockholder Equity"]

                if total_liab is not None and equity is not None and equity != 0:
                    debt_to_equity = round(total_liab / equity, 2)
        except Exception:
            pass

        ddm_value = "-"
        k_percent = 10.0
        try:
            rf = 0.025
            erp = 0.08

            beta = get_val("beta", "-")
            if beta == "-":
                beta = 1.0
            else:
                beta = float(beta)

            if beta < 0.5:
                beta = 0.5
            if beta > 2.5:
                beta = 2.5

            k = rf + (beta * erp)
            k_percent = round(k * 100, 2)

            eg_rate = get_float("earningsGrowth")
            if eg_rate == "-":
                eg_rate = 0.03

            g_high = 0.03
            if eg_rate > 0:
                g_high = min(eg_rate, 0.15)

            g_perpetual = 0.03

            d0 = final_div_rate
            if d0 == "-":
                d0 = 0.0

            if d0 > 0:
                pv_stage1 = 0
                dividends_stage1 = []

                for t in range(1, 6):
                    dt = d0 * ((1 + g_high) ** t)
                    dividends_stage1.append(dt)
                    pv_stage1 += dt / ((1 + k) ** t)

                d5 = dividends_stage1[-1]
                d6 = d5 * (1 + g_perpetual)

                if k <= g_perpetual:
                    k = g_perpetual + 0.01

                tv5 = d6 / (k - g_perpetual)
                pv_tv5 = tv5 / ((1 + k) ** 5)

                ddm_value = round(pv_stage1 + pv_tv5, 2)
            else:
                ddm_value = 0.0
        except Exception:
            ddm_value = "-"

        graham_number = "-"
        try:
            eps_ttm = get_float("trailingEps")
            bvps = get_float("bookValue")
            if eps_ttm != "-" and bvps != "-" and eps_ttm > 0 and bvps > 0:
                graham_number = round((22.5 * eps_ttm * bvps) ** 0.5, 2)
        except Exception:
            pass

        price = get_float("currentPrice")
        calculated_div_yield = "-"
        if price != "-" and price > 0 and final_div_rate != "-" and final_div_rate > 0:
            calculated_div_yield = round((float(final_div_rate) / float(price)) * 100, 2)
        else:
            yf_dy = get_float("dividendYield")
            if yf_dy != "-":
                try:
                    yf_dy_val = float(yf_dy)
                    calculated_div_yield = round(yf_dy_val * 100 if yf_dy_val < 1 else yf_dy_val, 2)
                except Exception:
                    calculated_div_yield = "-"

        dy = calculated_div_yield if calculated_div_yield != "-" else get_float("dividendYield", 1)
        if dy != "-" and dy < 1 and dy > 0:
            dy = round(dy * 100, 2)

        lynch_value = "-"
        try:
            eps_ttm = get_float("trailingEps")
            growth_rate = get_float("earningsGrowth")
            if eps_ttm != "-" and growth_rate != "-" and eps_ttm > 0 and growth_rate > 0:
                g_percent = growth_rate * 100
                if g_percent > 25:
                    g_percent = 25
                div_yield_percent = dy if (dy != "-" and dy > 0) else 0
                lynch_value = round(eps_ttm * (g_percent + div_yield_percent), 2)
        except Exception:
            pass

        rsi = "-"
        try:
            hist = stock.history(period="3mo")
            if not hist.empty and len(hist) > 14:
                delta = hist["Close"].diff()
                up = delta.clip(lower=0)
                down = -1 * delta.clip(upper=0)
                ma_up = up.ewm(com=13, adjust=False).mean()
                ma_down = down.ewm(com=13, adjust=False).mean()
                rs = ma_up / ma_down
                rsi_series = 100 - (100 / (1 + rs))
                rsi = round(rsi_series.iloc[-1], 2)
        except Exception:
            pass

        mos = "-"
        fair_value = ddm_value

        valid_values = []
        if ddm_value != "-" and ddm_value > 0:
            valid_values.append(ddm_value)
        if graham_number != "-" and graham_number > 0:
            valid_values.append(graham_number)
        if lynch_value != "-" and lynch_value > 0:
            valid_values.append(lynch_value)

        target_price = get_float("targetMeanPrice")
        if target_price != "-" and target_price > 0:
            valid_values.append(target_price)

        if valid_values:
            fair_value = sum(valid_values) / len(valid_values)

        if fair_value != "-" and price != "-" and price > 0 and fair_value > 0:
            mos = round(((fair_value - price) / fair_value) * 100, 2)

        de = debt_to_equity
        roe_ratio = get_float("returnOnEquity")
        roe_percent = get_float("returnOnEquity", 100)
        eg = get_float("earningsGrowth")

        def calculate_dividend_cagr(series, years):
            if len(series) < years:
                return "-"
            window = series[-years:]
            positive = [(idx, value) for idx, value in enumerate(window) if value is not None and value > 0]
            if len(positive) < 2:
                return "-"
            start_idx, start_val = positive[0]
            end_idx, end_val = positive[-1]
            span = end_idx - start_idx
            if span <= 0 or start_val <= 0 or end_val <= 0:
                return "-"
            try:
                return round((((end_val / start_val) ** (1 / span)) - 1) * 100, 2)
            except Exception:
                return "-"

        dividend_cut_count_10y = 0
        positive_divs = [value if value is not None else 0.0 for value in div_trend]
        for i in range(1, len(positive_divs)):
            prev_val = positive_divs[i - 1]
            curr_val = positive_divs[i]
            if prev_val > 0 and curr_val < prev_val * 0.85:
                dividend_cut_count_10y += 1

        dividend_cagr_5y = calculate_dividend_cagr(div_trend, 5)
        dividend_cagr_10y = calculate_dividend_cagr(div_trend, 10)

        dividend_score = 0
        dividend_score_details = []

        if dy != "-" and dy >= 3:
            dividend_score += 1
            dividend_score_details.append("Dividend Yield >= 3%")

        if final_div_rate != "-" and final_div_rate > 0:
            dividend_score += 1
            dividend_score_details.append("มีเงินปันผลล่าสุด")

        non_zero_div_years = sum(1 for value in div_trend if value and value > 0)
        if non_zero_div_years >= 7:
            dividend_score += 1
            dividend_score_details.append("จ่ายปันผล >= 7/10 ปี")

        if dividend_cut_count_10y == 0 and non_zero_div_years >= 5:
            dividend_score += 1
            dividend_score_details.append("ไม่ตัดปันผลแรงใน 10 ปี")
        elif dividend_cut_count_10y <= 1 and non_zero_div_years >= 5:
            dividend_score += 0.5
            dividend_score_details.append("ตัดปันผลน้อย (<= 1 ครั้ง)")

        if dividend_cagr_5y != "-" and dividend_cagr_5y > 3:
            dividend_score += 1
            dividend_score_details.append(f"Dividend CAGR 5Y > 3% ({dividend_cagr_5y}%)")

        if payout_ratio != "-" and 20 <= payout_ratio <= 80:
            dividend_score += 1
            dividend_score_details.append(f"Payout Ratio สมดุล ({payout_ratio}%)")
        elif payout_ratio != "-" and payout_ratio <= 100:
            dividend_score += 0.5
            dividend_score_details.append(f"Payout Ratio พอรับได้ ({payout_ratio}%)")

        if de != "-" and de < 1.5:
            dividend_score += 1
            dividend_score_details.append("D/E < 1.5")

        if roe_percent != "-" and roe_percent > 12:
            dividend_score += 1
            dividend_score_details.append("ROE > 12%")

        if mos != "-" and mos > 0:
            dividend_score += 1
            dividend_score_details.append(f"มี Margin of Safety ({mos}%)")

        if eg != "-" and eg > 0:
            dividend_score += 1
            dividend_score_details.append("กำไรยังเติบโต")

        dividend_score = round(dividend_score, 1)
        dividend_grade = "D"
        if dividend_score >= 8:
            dividend_grade = "A"
        elif dividend_score >= 6:
            dividend_grade = "B"
        elif dividend_score >= 4:
            dividend_grade = "C"

        dividend_safety_score = 0
        if payout_ratio != "-" and 20 <= payout_ratio <= 80:
            dividend_safety_score += 2
        elif payout_ratio != "-" and payout_ratio <= 100:
            dividend_safety_score += 1
        if dividend_cut_count_10y == 0:
            dividend_safety_score += 2
        elif dividend_cut_count_10y <= 1:
            dividend_safety_score += 1
        if de != "-" and de < 1.5:
            dividend_safety_score += 2
        elif de != "-" and de < 2.0:
            dividend_safety_score += 1
        if non_zero_div_years >= 7:
            dividend_safety_score += 2
        elif non_zero_div_years >= 5:
            dividend_safety_score += 1
        if eg != "-" and eg > 0:
            dividend_safety_score += 1
        if roe_percent != "-" and roe_percent > 12:
            dividend_safety_score += 1

        is_dividend_safe = dividend_safety_score >= 6 and dividend_score >= 6 and dy != "-" and dy >= 3

        score = 0
        score_details = []

        pe = get_float("trailingPE")
        if pe != "-" and pe < 20 and pe > 0:
            score += 1
            score_details.append("P/E < 20")

        eg = get_float("earningsGrowth")
        peg = "-"
        if pe != "-" and eg != "-" and eg > 0:
            peg = pe / (eg * 100)
            if peg < 1.5:
                score += 1
                score_details.append(f"PEG < 1.5 ({peg:.2f})")

        if mos != "-" and mos > 0:
            score += 1
            score_details.append(f"Price < Fair Value (MOS {mos}%)")

        roe = roe_ratio
        if roe != "-" and roe > 0.12:
            score += 1
            score_details.append("ROE > 12%")

        if de != "-" and de < 1.5:
            score += 1
            score_details.append("D/E < 1.5")

        if dy != "-" and dy > 3:
            score += 1
            score_details.append("Yield > 3%")

        if eg != "-" and eg > 0.05:
            score += 1
            score_details.append("Earn Growth > 5%")

        if rsi != "-" and rsi < 50:
            score += 1
            score_details.append(f"RSI < 50 ({rsi})")

        if mos != "-" and mos > 20:
            score += 1
            score_details.append(f"MOS > 20% ({mos}%)")

        nm = get_float("profitMargins")
        if nm != "-" and nm > 0.10:
            score += 1
            score_details.append("Net Margin > 10%")

        grade = "D"
        if score >= 7:
            grade = "A"
        elif score >= 5:
            grade = "B"
        elif score >= 3:
            grade = "C"

        is_value_trap = False
        if pe != "-" and pe < 10 and pe > 0:
            revenue_growth = get_float("revenueGrowth")
            if (eg != "-" and eg < 0) or (revenue_growth != "-" and revenue_growth < 0):
                is_value_trap = True

        description = "-"
        if include_description:
            description = get_val("longBusinessSummary", "-")

        return {
            "symbol": ticker,
            "name": get_val("longName", ticker),
            "price": get_val("currentPrice", 0),
            "pe_trailing": get_val("trailingPE", "-"),
            "pe_forward": get_val("forwardPE", "-"),
            "market_cap": get_val("marketCap", 0),
            "dividend_yield": calculated_div_yield,
            "dividend_rate": final_div_rate,
            "ddm_value": ddm_value,
            "ddm_k": k_percent,
            "graham_number": graham_number,
            "lynch_value": lynch_value,
            "fair_value": round(fair_value, 2) if fair_value != "-" and fair_value > 0 else "-",
            "peg": round(peg, 2) if peg != "-" else "-",
            "target_price": get_val("targetMeanPrice", "-"),
            "rsi": rsi,
            "mos": mos,
            "beta": get_val("beta", "-"),
            "high_52": get_val("fiftyTwoWeekHigh", "-"),
            "low_52": get_val("fiftyTwoWeekLow", "-"),
            "bvps": get_val("bookValue", "-"),
            "revenue_growth": get_float("revenueGrowth", 100),
            "ebitda_growth": get_float("earningsGrowth", 100),
            "eps_trend": eps_trend,
            "div_trend": div_trend,
            "score": score,
            "grade": grade,
            "score_details": score_details,
            "dividend_score": dividend_score,
            "dividend_grade": dividend_grade,
            "dividend_score_details": dividend_score_details,
            "dividend_cut_count_10y": dividend_cut_count_10y,
            "dividend_cagr_5y": dividend_cagr_5y,
            "dividend_cagr_10y": dividend_cagr_10y,
            "payout_ratio": payout_ratio,
            "dividend_safety_score": dividend_safety_score,
            "is_dividend_safe": is_dividend_safe,
            "is_value_trap": is_value_trap,
            "error": None,
            "details": {
                "roa": get_float("returnOnAssets", 100),
                "roe": get_float("returnOnEquity", 100),
                "gross_margin": get_float("grossMargins", 100),
                "operating_margin": get_float("operatingMargins", 100),
                "profit_margin": get_float("profitMargins", 100),
                "debt_to_equity": debt_to_equity,
                "current_ratio": get_val("currentRatio", "-"),
                "quick_ratio": get_val("quickRatio", "-"),
                "book_value": get_val("bookValue", "-"),
                "price_to_book": get_val("priceToBook", "-"),
                "industry": get_val("industry", "-"),
                "sector": get_val("sector", "-"),
                "description": description,
                "payout_ratio": payout_ratio,
                "dividend_cagr_5y": dividend_cagr_5y,
                "dividend_cagr_10y": dividend_cagr_10y,
                "dividend_cut_count_10y": dividend_cut_count_10y,
                "dividend_safety_score": dividend_safety_score,
            },
        }
    except Exception:
        return build_empty_stock_payload(ticker, "Data Unavailable")
