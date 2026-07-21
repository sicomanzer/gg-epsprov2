import re
from datetime import datetime
from functools import lru_cache

import pandas as pd
import yfinance as yf

from .. import config

try:
    from thaifin import Stock as ThaiFinStock

    THAIFIN_AVAILABLE = True
except Exception:
    ThaiFinStock = None
    THAIFIN_AVAILABLE = False


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
        },
    }


def get_thaifin_symbol(ticker):
    return config.THAIFIN_TICKER_ALIASES.get(ticker, ticker)


@lru_cache(maxsize=512)
def get_eps_trend_from_thaifin(ticker, current_year):
    years_eps = [current_year - config.EPS_TREND_YEARS + i for i in range(config.EPS_TREND_YEARS)]
    eps_trend = [None] * config.EPS_TREND_YEARS

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
    except Exception:
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
        computed_div_rate = 0
        dividends = stock.dividends
        if not dividends.empty:
            div_yearly = dividends.resample("YE").sum()
            div_dict = {ts.year: val for ts, val in div_yearly.items()}

            computed_div_rate = float(div_dict.get(current_year - 1, 0.0) or 0.0)
            start_year = current_year - config.DIV_TREND_YEARS
            for y in range(start_year, current_year):
                div_trend.append(float(div_dict.get(y, 0.0) or 0.0))
        else:
            div_trend = [0.0] * config.DIV_TREND_YEARS

        final_div_rate = computed_div_rate if computed_div_rate > 0 else get_val("dividendRate", "-")

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

        lynch_value = "-"
        try:
            eps_ttm = get_float("trailingEps")
            growth_rate = get_float("earningsGrowth")
            if eps_ttm != "-" and growth_rate != "-" and eps_ttm > 0 and growth_rate > 0:
                g_percent = growth_rate * 100
                if g_percent > 25:
                    g_percent = 25
                div_yield_percent = get_float("dividendYield", 1, 0)
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
        price = get_float("currentPrice")
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

        roe = get_float("returnOnEquity")
        if roe != "-" and roe > 0.12:
            score += 1
            score_details.append("ROE > 12%")

        de = debt_to_equity
        if de != "-" and de < 1.5:
            score += 1
            score_details.append("D/E < 1.5")

        dy = get_float("dividendYield", 1)
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
            "dividend_yield": get_float("dividendYield", 1)
            if get_float("dividendYield") != "-"
            else "-",
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
            },
        }
    except Exception:
        return build_empty_stock_payload(ticker, "Data Unavailable")
