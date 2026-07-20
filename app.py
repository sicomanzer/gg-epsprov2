import json
import logging
import os
import re
import secrets
import sqlite3
import time
import random
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, redirect, url_for, session
import requests
import yfinance as yf
import pandas as pd
from concurrent.futures import ThreadPoolExecutor
from deep_translator import GoogleTranslator
from functools import lru_cache

try:
    from thaifin import Stock as ThaiFinStock
    THAIFIN_AVAILABLE = True
except Exception:
    ThaiFinStock = None
    THAIFIN_AVAILABLE = False

# Set yfinance cache path to a local directory to avoid permission issues
try:
    yf.set_tz_cache_location("yfinance_cache")
except:
    pass

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "gg-epsprov2-dev-secret")
app.logger.setLevel(logging.INFO)

# --- Vercel Configuration ---
# Vercel file system is read-only except for /tmp
# We need to copy the DB to /tmp to make it writable (but data is ephemeral)
import shutil

DB_FILE = 'stocks.db'
STOCKS_FILE = 'stocks.json' # Keep for migration
GRADE_A_MIN_SCORE = 7
SNIPER_MIN_MOS = 10
API_MAX_WORKERS = int(os.environ.get("MAX_FETCH_WORKERS", "12"))
TICKER_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")
SET100_SOURCE_PAGE_URL = "https://www.settrade.com/th/equities/market-data/overview?category=Index&index=SET100"
SET100_SOURCE_API_URL = "https://www.settrade.com/api/set/index/SET100/composition"
SET100_AUTO_SYNC_INTERVAL_SECONDS = int(os.environ.get("SET100_AUTO_SYNC_INTERVAL_SECONDS", "43200"))
SET100_MIN_EXPECTED_SYMBOLS = 80
REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0"}
SET100_TICKER_ALIASES = {
    "BANPUU": "BANPU",
}
THAIFIN_TICKER_ALIASES = {
    "BANPU": "BANPUU",
}
EPS_TREND_YEARS = 10
DIV_TREND_YEARS = 10

# Check if running on Vercel (or any environment where root is read-only)
# We can check if we can write to the current directory or just default to /tmp in production
# Simple check: Is there a 'VERCEL' env var?
IS_VERCEL = os.environ.get('VERCEL') == '1'

if IS_VERCEL:
    # Use /tmp for database
    DB_PATH = os.path.join('/tmp', DB_FILE)
    CACHE_DIR = os.path.join('/tmp', 'yfinance_cache')
else:
    DB_PATH = DB_FILE
    CACHE_DIR = "yfinance_cache"

# Set yfinance cache path
try:
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR, exist_ok=True)
    yf.set_tz_cache_location(CACHE_DIR)
except Exception as exc:
    app.logger.warning("Unable to initialize yfinance cache directory: %s", exc)

# Initial SET100 list
INITIAL_STOCKS = [
    "AAV", "ADVANC", "AEONTS", "AMATA", "AOT", "AP", "AURA", "AWC", "BA", "BAM", 
    "BANPU", "BBL", "BCH", "BCP", "BCPG", "BDMS", "BEM", "BGRIM", "BH", "BJC", 
    "BLA", "BTG", "BTS", "CBG", "CCET", "CENTEL", "CHG", "CK", "COM7", "CPALL", 
    "CPF", "CPN", "CRC", "DELTA", "DOHOME", "EA", "EGCO", "ERW", "GFPT", "GLOBAL", 
    "GPSC", "GULF", "GUNKUL", "HANA", "HMPRO", "ICHI", "IRPC", "IVL", "JAS", "JMART", 
    "JMT", "JTS", "KBANK", "KCE", "KKP", "KTB", "KTC", "LH", "M", "MEGA", 
    "MINT", "MOSHI", "MTC", "OR", "OSP", "PLANB", "PR9", "PRM", "PTG", "PTT", 
    "PTTEP", "PTTGC", "QH", "RATCH", "RCL", "SAWAD", "SCB", "SCC", "SCGP", "SIRI", 
    "SISB", "SJWD", "SPALI", "SPRC", "STA", "STECON", "STGT", "TASCO", "TCAP", "TFG", 
    "TIDLOR", "TISCO", "TLI", "TOA", "TOP", "TRUE", "TTB", "TU", "VGI", "WHA"
]

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout = 30000')
    return conn

def get_csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_hex(16)
        session["csrf_token"] = token
    return token

def is_valid_csrf_token():
    form_token = request.form.get('csrf_token', '')
    session_token = session.get('csrf_token', '')
    return bool(form_token and session_token and secrets.compare_digest(form_token, session_token))

def build_redirect(message, level="success"):
    return redirect(url_for('index', message=message, level=level))

def normalize_ticker(raw_ticker):
    if not raw_ticker:
        return None

    clean_ticker = raw_ticker.upper().strip()
    if not TICKER_PATTERN.fullmatch(clean_ticker):
        return None
    return clean_ticker

def parse_tickers(raw_input):
    valid = []
    invalid = []

    for value in re.split(r'[,\s\n]+', raw_input or ""):
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
        "eps_trend": [None] * EPS_TREND_YEARS,
        "div_trend": [0.0] * DIV_TREND_YEARS,
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
            "description": error
        }
    }

def now_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def parse_iso_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

def ensure_stocks_schema(conn):
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(stocks)").fetchall()}

    if "is_manual" not in columns:
        conn.execute("ALTER TABLE stocks ADD COLUMN is_manual INTEGER NOT NULL DEFAULT 0")
    if "is_set100" not in columns:
        conn.execute("ALTER TABLE stocks ADD COLUMN is_set100 INTEGER NOT NULL DEFAULT 0")

    # Migrate legacy rows into manual list so user data is preserved.
    conn.execute(
        """
        UPDATE stocks
        SET is_manual = 1
        WHERE COALESCE(is_manual, 0) = 0 AND COALESCE(is_set100, 0) = 0
        """
    )

def ensure_sync_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS set100_sync_status (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_synced_at TEXT,
            last_success_at TEXT,
            last_status TEXT,
            last_message TEXT,
            source_url TEXT,
            total_symbols INTEGER NOT NULL DEFAULT 0,
            added_count INTEGER NOT NULL DEFAULT 0,
            removed_count INTEGER NOT NULL DEFAULT 0,
            mode TEXT NOT NULL DEFAULT 'manual'
        )
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO set100_sync_status (
            id, last_status, last_message, source_url, total_symbols, added_count, removed_count, mode
        ) VALUES (1, 'pending', 'ยังไม่เคยซิงก์ SET100', '', 0, 0, 0, 'manual')
        """
    )

def get_set100_sync_status():
    conn = get_db_connection()
    try:
        ensure_sync_schema(conn)
        row = conn.execute("SELECT * FROM set100_sync_status WHERE id = 1").fetchone()
        if not row:
            return {
                "last_synced_at": None,
                "last_success_at": None,
                "last_status": "pending",
                "last_message": "ยังไม่เคยซิงก์ SET100",
                "source_url": "",
                "total_symbols": 0,
                "added_count": 0,
                "removed_count": 0,
                "mode": "manual",
            }
        return dict(row)
    finally:
        conn.close()

def save_set100_sync_status(status, commit=True):
    conn = get_db_connection()
    try:
        ensure_sync_schema(conn)
        conn.execute(
            """
            UPDATE set100_sync_status
            SET last_synced_at = ?,
                last_success_at = ?,
                last_status = ?,
                last_message = ?,
                source_url = ?,
                total_symbols = ?,
                added_count = ?,
                removed_count = ?,
                mode = ?
            WHERE id = 1
            """,
            (
                status.get("last_synced_at"),
                status.get("last_success_at"),
                status.get("last_status"),
                status.get("last_message"),
                status.get("source_url", ""),
                status.get("total_symbols", 0),
                status.get("added_count", 0),
                status.get("removed_count", 0),
                status.get("mode", "manual"),
            ),
        )
        if commit:
            conn.commit()
    finally:
        conn.close()

def fetch_set100_symbols():
    session_client = requests.Session()
    session_client.headers.update(REQUEST_HEADERS)

    page_response = session_client.get(SET100_SOURCE_PAGE_URL, timeout=20)
    page_response.raise_for_status()

    api_response = session_client.get(
        SET100_SOURCE_API_URL,
        timeout=20,
        headers={
            "Referer": SET100_SOURCE_PAGE_URL,
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
        ticker = normalize_ticker(raw_ticker)
        if ticker:
            ticker = SET100_TICKER_ALIASES.get(ticker, ticker)
        if ticker:
            symbols.append(ticker)

    unique_symbols = sorted(set(symbols))
    if len(unique_symbols) < SET100_MIN_EXPECTED_SYMBOLS:
        raise RuntimeError(f"ดึงรายชื่อ SET100 ได้ไม่ครบ ({len(unique_symbols)} รายการ)")

    return unique_symbols, SET100_SOURCE_API_URL

def load_stock_records():
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT symbol, is_manual, is_set100
            FROM stocks
            WHERE is_manual = 1 OR is_set100 = 1
            ORDER BY symbol
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()

def load_stock_flags():
    return {
        row["symbol"]: {
            "is_manual": bool(row["is_manual"]),
            "is_set100": bool(row["is_set100"]),
        }
        for row in load_stock_records()
    }

def get_thaifin_symbol(ticker):
    return THAIFIN_TICKER_ALIASES.get(ticker, ticker)

@lru_cache(maxsize=512)
def get_eps_trend_from_thaifin(ticker, current_year):
    years_eps = [current_year - EPS_TREND_YEARS + i for i in range(EPS_TREND_YEARS)]
    eps_trend = [None] * EPS_TREND_YEARS

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
        app.logger.info("ThaiFin EPS fallback for %s: %s", ticker, exc)
        return tuple(years_eps), tuple(eps_trend)

def sync_set100_list(mode="manual"):
    started_at = now_iso()
    try:
        symbols, source_url = fetch_set100_symbols()
        conn = get_db_connection()
        try:
            existing_rows = conn.execute(
                "SELECT symbol, is_set100 FROM stocks WHERE is_set100 = 1"
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

            conn.execute(
                "DELETE FROM stocks WHERE is_manual = 0 AND is_set100 = 0"
            )

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

            ensure_sync_schema(conn)
            conn.execute(
                """
                UPDATE set100_sync_status
                SET last_synced_at = ?,
                    last_success_at = ?,
                    last_status = ?,
                    last_message = ?,
                    source_url = ?,
                    total_symbols = ?,
                    added_count = ?,
                    removed_count = ?,
                    mode = ?
                WHERE id = 1
                """,
                (
                    status["last_synced_at"],
                    status["last_success_at"],
                    status["last_status"],
                    status["last_message"],
                    status["source_url"],
                    status["total_symbols"],
                    status["added_count"],
                    status["removed_count"],
                    status["mode"],
                ),
            )
            conn.commit()
            return status
        finally:
            conn.close()
    except Exception as exc:
        app.logger.warning("SET100 sync failed: %s", exc)
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
    if request.method != "GET" or request.endpoint not in {"index", "api_data"}:
        return

    status = get_set100_sync_status()
    last_success = parse_iso_datetime(status.get("last_success_at"))
    if last_success and datetime.now(last_success.tzinfo) - last_success < timedelta(seconds=SET100_AUTO_SYNC_INTERVAL_SECONDS):
        return

    if getattr(app, "set100_auto_sync_running", False):
        return

    try:
        app.set100_auto_sync_running = True
        sync_set100_list(mode="auto")
    except Exception:
        pass
    finally:
        app.set100_auto_sync_running = False

def init_db():
    # If on Vercel and DB not in /tmp, initialize it (copy from source or create new)
    if IS_VERCEL and not os.path.exists(DB_PATH):
        # On Vercel, copy the bundled DB to /tmp
        if os.path.exists(DB_FILE):
             shutil.copy2(DB_FILE, DB_PATH)
    
    # Only run init if we are creating a fresh DB
    conn = sqlite3.connect(DB_PATH, timeout=30)
    c = conn.cursor()
    c.execute('PRAGMA busy_timeout = 30000')
    c.execute('PRAGMA journal_mode = WAL')
    c.execute('''CREATE TABLE IF NOT EXISTS stocks
                 (symbol TEXT PRIMARY KEY)''')

    c.execute('''CREATE TABLE IF NOT EXISTS translations
                 (symbol TEXT PRIMARY KEY, description_th TEXT)''')
    conn.row_factory = sqlite3.Row
    ensure_stocks_schema(conn)
    ensure_sync_schema(conn)
    
    # Check if empty
    c.execute('SELECT count(*) FROM stocks WHERE is_manual = 1 OR is_set100 = 1')
    count = c.fetchone()[0]
    
    if count == 0:
        # Migrate from JSON if exists, else use INITIAL_STOCKS
        initial_data = []
        if os.path.exists(STOCKS_FILE):
            try:
                with open(STOCKS_FILE, 'r') as f:
                    initial_data = json.load(f)
            except Exception as exc:
                app.logger.warning("Unable to read %s, using defaults: %s", STOCKS_FILE, exc)
                initial_data = INITIAL_STOCKS
        else:
            initial_data = INITIAL_STOCKS
            
        # Bulk insert
        # Ensure unique
        unique_stocks = list(set(initial_data))
        c.executemany(
            'INSERT OR IGNORE INTO stocks (symbol, is_set100) VALUES (?, 1)',
            [(s,) for s in unique_stocks]
        )
        conn.commit()
        print(f"Initialized DB with {len(unique_stocks)} stocks.")
        
    conn.close()

# Initialize DB on startup
# Use before_request to avoid cold start timeouts
@app.before_request
def initialize():
    get_csrf_token()
    if not getattr(app, 'db_initialized', False):
        try:
            init_db()
            app.db_initialized = True
        except Exception as e:
            print(f"DB Init Error: {e}")
    maybe_auto_sync_set100()

@app.route('/health')
def health_check():
    sync_status = get_set100_sync_status()
    return jsonify({"status": "ok", "vercel": IS_VERCEL, "set100_sync": sync_status})

def load_stocks():
    return [row["symbol"] for row in load_stock_records()]

def add_stock_db(symbol):
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO stocks (symbol, is_manual, is_set100)
            VALUES (?, 1, 0)
            ON CONFLICT(symbol) DO UPDATE SET is_manual = 1
            """,
            (symbol,),
        )
        conn.commit()
        return cursor.rowcount > 0
    except sqlite3.Error as exc:
        app.logger.warning("Unable to add stock %s: %s", symbol, exc)
        return False
    finally:
        conn.close()

def remove_stock_db(symbol):
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            """
            UPDATE stocks
            SET is_manual = 0
            WHERE symbol = ? AND is_manual = 1
            """,
            (symbol,),
        )
        conn.execute('DELETE FROM stocks WHERE symbol = ? AND is_manual = 0 AND is_set100 = 0', (symbol,))
        conn.commit()
        return cursor.rowcount > 0
    except sqlite3.Error as exc:
        app.logger.warning("Unable to remove stock %s: %s", symbol, exc)
        return False
    finally:
        conn.close()

def clear_all_stocks_db():
    conn = get_db_connection()
    try:
        conn.execute('UPDATE stocks SET is_manual = 0 WHERE is_manual = 1')
        conn.execute('DELETE FROM stocks WHERE is_manual = 0 AND is_set100 = 0')
        conn.commit()
        return True
    except sqlite3.Error as exc:
        app.logger.warning("Unable to clear all stocks: %s", exc)
        return False
    finally:
        conn.close()

def get_translation_db(symbol):
    conn = get_db_connection()
    try:
        row = conn.execute('SELECT description_th FROM translations WHERE symbol = ?', (symbol,)).fetchone()
        if row:
            return row['description_th']
    except sqlite3.Error as exc:
        app.logger.warning("Unable to load translation cache for %s: %s", symbol, exc)
    finally:
        conn.close()
    return None

def save_translation_db(symbol, text):
    conn = get_db_connection()
    try:
        conn.execute('INSERT OR REPLACE INTO translations (symbol, description_th) VALUES (?, ?)', (symbol, text))
        conn.commit()
    except sqlite3.Error as exc:
        app.logger.warning("Unable to save translation cache for %s: %s", symbol, exc)
    finally:
        conn.close()

def get_stock_data(ticker):
    try:
        if not normalize_ticker(ticker):
            return build_empty_stock_payload(ticker, "Invalid ticker")

        # Append .BK for Thai stocks if not present, assuming mostly SET stocks
        symbol = ticker if "." in ticker else f"{ticker}.BK"
        stock = yf.Ticker(symbol)
        info = stock.info
        
        # Safe get for data
        def get_val(key, default="-"):
            val = info.get(key, default)
            if val is None: return default
            return val

        def get_float(key, multiplier=1.0, default="-"):
            val = info.get(key)
            if val is None: return default
            try:
                return float(val) * multiplier
            except:
                return default

        # Fetch Historical Data (Trend 10Y EPS & 10Y Dividends)
        eps_trend = []
        div_trend = []
        import datetime
        current_year = datetime.datetime.now().year
        
        try:
            years_eps, eps_trend = get_eps_trend_from_thaifin(ticker, current_year)

            if not any(val is not None for val in eps_trend):
                financials = stock.financials
                eps_trend = [None] * EPS_TREND_YEARS

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
                    eps_trend = [None] * EPS_TREND_YEARS
                    t_eps = get_val("trailingEps", None)
                    if t_eps != "-":
                        eps_trend[-1] = t_eps
            
            # Dividend Trend (Last 10 Years, ending at 2025)
            dividends = stock.dividends
            div_trend = []
            computed_div_rate = 0
            
            if not dividends.empty:
                # Group by year and sum
                div_yearly = dividends.resample('YE').sum()
                div_dict = {ts.year: val for ts, val in div_yearly.items()}
                
                # Get last year's dividend (e.g. 2025)
                computed_div_rate = div_dict.get(current_year - 1, 0.0)
                
                # Prepare div_trend for chart (Last 10 years ending at current_year - 1)
                div_trend = []
                start_year = current_year - DIV_TREND_YEARS
                for y in range(start_year, current_year):
                    val = div_dict.get(y, 0.0)
                    div_trend.append(float(val))
                    
        except Exception as e:
            print(f"Error fetching history for {ticker}: {e}")

        # Use computed_div_rate if available, otherwise fallback to yfinance info
        # If computed_div_rate is 0, we might want to check if there is ANY dividend.
        # But for now, 0 is fine if no dividend.
        final_div_rate = computed_div_rate if computed_div_rate > 0 else get_val("dividendRate", "-")
        
        # Calculate D/E from Balance Sheet (More accurate for Thai Stocks)
        debt_to_equity = get_val("debtToEquity", "-")
        try:
            bs = stock.balance_sheet
            if not bs.empty:
                # Use iloc[:, 0] to get latest year
                latest_col = bs.iloc[:, 0]
                
                # Total Liabilities
                total_liab = None
                if "Total Liabilities Net Minority Interest" in bs.index:
                    total_liab = latest_col["Total Liabilities Net Minority Interest"]
                elif "Total Liabilities" in bs.index:
                    total_liab = latest_col["Total Liabilities"]
                
                # Stockholders Equity
                equity = None
                if "Stockholders Equity" in bs.index:
                    equity = latest_col["Stockholders Equity"]
                elif "Total Stockholder Equity" in bs.index:
                    equity = latest_col["Total Stockholder Equity"]
                
                if total_liab is not None and equity is not None and equity != 0:
                    debt_to_equity = round(total_liab / equity, 2)
        except Exception as e:
            pass

        # --- Two-Stage DDM Calculation (Dynamic Discount Rate) ---
        ddm_value = "-"
        k_percent = 10.0 # Default 10%
        try:
            # 1. Dynamic Discount Rate (CAPM)
            # k = Rf + Beta * (Rm - Rf)
            # Rf (Thai 10Y Bond) ~= 2.5%
            # ERP (Rm - Rf) ~= 8%
            rf = 0.025
            erp = 0.08
            
            beta = get_val("beta", "-")
            if beta == "-": beta = 1.0 # Default Beta
            else: beta = float(beta)
            
            # Cap Beta to avoid extreme values (0.5 to 2.0)
            if beta < 0.5: beta = 0.5
            if beta > 2.5: beta = 2.5
            
            k = rf + (beta * erp)
            k_percent = round(k * 100, 2)
            
            # 2. Parameters
            # Use Earnings Growth for Stage 1 (Capped at 15% to be conservative)
            eg_rate = get_float("earningsGrowth")
            if eg_rate == "-": eg_rate = 0.03 # Default 3% if missing
            
            # g_high = min(eg_rate, 0.15) but also max(eg_rate, 0.03) to give at least some growth?
            # Let's stick to: if growth is high, use it (capped). If low/negative, use 0 or low.
            g_high = 0.03
            if eg_rate > 0:
                g_high = min(eg_rate, 0.15)
            
            g_perpetual = 0.03 # 3% Terminal Growth
            
            # D0 = Current Dividend (final_div_rate)
            d0 = final_div_rate
            if d0 == "-": d0 = 0.0
            
            if d0 > 0:
                # Perform 2-Stage DDM
                pv_stage1 = 0
                dividends_stage1 = []
                
                # We start from D1. D1 = D0 * (1+g)
                for t in range(1, 6):
                    dt = d0 * ((1 + g_high) ** t)
                    dividends_stage1.append(dt)
                    
                    # Discount to PV
                    pv_dt = dt / ((1 + k) ** t)
                    pv_stage1 += pv_dt
                
                d5 = dividends_stage1[-1]
                
                # STEP 3: Find D6
                d6 = d5 * (1 + g_perpetual)
                
                # STEP 4: Calculate Terminal Value (TV5)
                # TV5 = D6 / (k - g)
                # Safety check: k must be > g
                if k <= g_perpetual:
                    k = g_perpetual + 0.01 # Force k > g by 1%
                
                tv5 = d6 / (k - g_perpetual)
                
                # STEP 5: Discount TV5 to PV
                pv_tv5 = tv5 / ((1 + k) ** 5)
                
                # STEP 6: Total Value
                total_value = pv_stage1 + pv_tv5
                ddm_value = round(total_value, 2)
            else:
                ddm_value = 0.0 # No dividend, no DDM value
                
        except Exception as e:
            print(f"DDM Error {ticker}: {e}")
            ddm_value = "-"

        # --- Graham Number Calculation ---
        # Formula: Sqrt(22.5 * EPS * BVPS)
        graham_number = "-"
        try:
            eps_ttm = get_float("trailingEps")
            bvps = get_float("bookValue")
            if eps_ttm != "-" and bvps != "-" and eps_ttm > 0 and bvps > 0:
                graham_val = (22.5 * eps_ttm * bvps) ** 0.5
                graham_number = round(graham_val, 2)
        except Exception as e:
            pass
            
        # --- Peter Lynch Fair Value (PEG Based) ---
        # Fair P/E = Growth Rate (approx). So Fair Price = EPS * (Growth Rate * 100)
        # We assume fair PEG = 1.0. 
        # Using 5-year expected growth or trailing growth. Let's use 'earningsGrowth' (quarterly) or 'revenueGrowth'
        # Ideally: EPS * (Earnings Growth Rate * 100)
        lynch_value = "-"
        try:
            eps_ttm = get_float("trailingEps")
            growth_rate = get_float("earningsGrowth") # This is like 0.15 for 15%
            if eps_ttm != "-" and growth_rate != "-" and eps_ttm > 0 and growth_rate > 0:
                # Cap growth rate for safety (e.g. max 25%)
                g_percent = growth_rate * 100
                if g_percent > 25: g_percent = 25 
                # Lynch Formula: Fair Value = EPS * Growth Rate
                # Often adds Dividend Yield: Fair Value = EPS * (Growth + Yield)
                
                # Note: dividendYield in yfinance is usually percentage (e.g. 5.5 for 5.5%)
                # But sometimes it might be decimal? Let's check magnitude.
                # If > 1, assume percent. If < 1, might be decimal or just low yield.
                # Safest is to treat it as percentage if it's consistent with recent observation (PRM=6.33, PTT=5.64)
                # But AAPL=0.38 (0.38%).
                # So it seems ALWAYS Percentage.
                div_yield_percent = get_float("dividendYield", 1, 0) 
                
                lynch_val = eps_ttm * (g_percent + div_yield_percent)
                lynch_value = round(lynch_val, 2)
        except:
            pass

        # --- RSI (14-Day) Calculation ---
        rsi = "-"
        try:
            # Fetch 3 months history to ensure enough data for 14-day RSI + smoothing
            hist = stock.history(period="3mo")
            if not hist.empty and len(hist) > 14:
                delta = hist['Close'].diff()
                up = delta.clip(lower=0)
                down = -1 * delta.clip(upper=0)
                
                # Wilder's Smoothing (alpha = 1/n) -> com = n - 1
                ma_up = up.ewm(com=13, adjust=False).mean()
                ma_down = down.ewm(com=13, adjust=False).mean()
                
                rs = ma_up / ma_down
                rsi_series = 100 - (100 / (1 + rs))
                rsi = round(rsi_series.iloc[-1], 2)
        except Exception as e:
            # print(f"RSI Error {ticker}: {e}") # Suppress to avoid spam
            pass

        # --- Margin of Safety (MOS) ---
        mos = "-"
        price = get_float("currentPrice")
        # Use average valuation if available, else fallback to DDM
        fair_value = ddm_value
        
        # Calculate Average Fair Value from valid models
        valid_values = []
        if ddm_value != "-" and ddm_value > 0: valid_values.append(ddm_value)
        if graham_number != "-" and graham_number > 0: valid_values.append(graham_number)
        if lynch_value != "-" and lynch_value > 0: valid_values.append(lynch_value)
        
        # Add Analyst Target if available
        target_price = get_float("targetMeanPrice")
        if target_price != "-" and target_price > 0: valid_values.append(target_price)
        
        if valid_values:
            fair_value = sum(valid_values) / len(valid_values)
            
        if fair_value != "-" and price != "-" and price > 0 and fair_value > 0:
            mos = round(((fair_value - price) / fair_value) * 100, 2)

        # --- Quality Score Calculation (Magic Score) ---
        score = 0
        score_details = []
        
        # 1. Valuation: P/E < 20 (Conservative)
        # Check against Sector P/E if available or use standard 20
        # For now standard 20 is safe
        pe = get_float("trailingPE")
        if pe != "-" and pe < 20 and pe > 0: 
            score += 1
            score_details.append("P/E < 20")
        
        # 2. Valuation: PEG < 1.5 (Growth at reasonable price)
        # PEG = P/E / Growth Rate (Earnings Growth)
        # If Earnings Growth is 0.20 (20%), and P/E is 20, PEG = 1.0
        # Correct Formula: PEG = P/E / (Growth Rate * 100)
        eg = get_float("earningsGrowth")
        peg = "-"
        if pe != "-" and eg != "-" and eg > 0:
            peg = pe / (eg * 100) 
            if peg < 1.5:
                score += 1
                score_details.append(f"PEG < 1.5 ({peg:.2f})")
        
        # 3. Valuation: Price < Fair Value (Margin of Safety > 0)
        # Changed from Price < DDM to Price < Average Fair Value
        if mos != "-" and mos > 0: 
            score += 1
            score_details.append(f"Price < Fair Value (MOS {mos}%)")
        
        # 4. Efficiency: ROE > 12%
        # Check ROE unit. yfinance usually returns 0.15 for 15%
        roe = get_float("returnOnEquity")
        if roe != "-" and roe > 0.12: 
            score += 1
            score_details.append("ROE > 12%")
        
        # 5. Financial Health: D/E < 1.5
        # Calculated from BS earlier
        de = debt_to_equity
        if de != "-" and de < 1.5: 
            score += 1
            score_details.append("D/E < 1.5")
        
        # 6. Dividend: Yield > 3%
        # yfinance returns yield as decimal (0.05) or percentage (5.0)?
        # Based on previous check, dividendYield is PERCENTAGE (e.g. 5.64)
        # BUT wait, get_float("dividendYield", 1) means we take raw value.
        # AAPL was 0.38 (0.38%), PTT was 5.64 (5.64%)
        # So threshold should be > 3 (if percentage)
        dy = get_float("dividendYield", 1) 
        if dy != "-" and dy > 3: 
            score += 1
            score_details.append("Yield > 3%")
        
        # 7. Growth: Earnings Growth > 5%
        # eg is decimal (0.20 for 20%)
        if eg != "-" and eg > 0.05: 
            score += 1
            score_details.append("Earn Growth > 5%")

        # 8. Technical: RSI < 50 (Good Entry / Not Overbought)
        if rsi != "-" and rsi < 50:
            score += 1
            score_details.append(f"RSI < 50 ({rsi})")

        # 9. Risk: MOS > 20% (Significant Safety Margin)
        # Increased from 10% to 20% for stricter safety
        if mos != "-" and mos > 20:
            score += 1
            score_details.append(f"MOS > 20% ({mos}%)")

        # 10. Bonus: Grade A if Score >= 7
        # Current Max Score is 9
        
        # Grade Assignment (Adjusted for max score 10)
        # We can add one more criteria or just assume 9/10 is A+
        # User requested: 7-10 = A
        
        # Let's add one more criteria to make it 10 points max
        # 10. Efficiency: Net Margin > 10%
        # profitMargins is decimal (0.10)
        nm = get_float("profitMargins")
        if nm != "-" and nm > 0.10:
            score += 1
            score_details.append("Net Margin > 10%")

        # A: 7-10, B: 5-6, C: 3-4, D: 0-2
        grade = "D"
        if score >= GRADE_A_MIN_SCORE: grade = "A"
        elif score >= 5: grade = "B"
        elif score >= 3: grade = "C"
        
        # --- Value Trap Detection ---
        # Definition: Low P/E but Negative Growth or Declining Revenue
        is_value_trap = False
        if pe != "-" and pe < 10 and pe > 0:
            if (eg != "-" and eg < 0) or (get_float("revenueGrowth") != "-" and get_float("revenueGrowth") < 0):
                is_value_trap = True

        # Translate Description to Thai
        description_en = get_val("longBusinessSummary", "-")
        description_th = description_en
        
        # Check cache first
        cached_desc = get_translation_db(ticker)
        if cached_desc:
            description_th = cached_desc
        elif description_en != "-" and len(description_en) > 10:
            try:
                # Random delay to avoid hitting rate limits with multiple threads
                time.sleep(random.uniform(0.1, 1.0))
                
                # Limit length to avoid timeout or excessive usage
                text_to_translate = description_en[:4500] 
                description_th = GoogleTranslator(source='auto', target='th').translate(text_to_translate)
                # Save to cache
                save_translation_db(ticker, description_th)
            except Exception as e:
                print(f"Translation Error {ticker}: {e}")
                pass

        return {
            "symbol": ticker,
            "name": get_val("longName", ticker),
            "price": get_val("currentPrice", 0),
            "pe_trailing": get_val("trailingPE", "-"),
            "pe_forward": get_val("forwardPE", "-"),
            "market_cap": get_val("marketCap", 0),
            "dividend_yield": get_float("dividendYield", 1) if get_float("dividendYield") != "-" else "-",
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
                "description": description_th
            }
        }
    except Exception as e:
        print(f"Error fetching {ticker}: {e}")
        return build_empty_stock_payload(ticker, "Data Unavailable")

@app.route('/')
def index():
    stocks = load_stock_records()
    sync_status = get_set100_sync_status()
    return render_template(
        'index.html',
        stocks=stocks,
        sync_status=sync_status,
        csrf_token=get_csrf_token(),
        grade_a_min_score=GRADE_A_MIN_SCORE,
        sniper_min_mos=SNIPER_MIN_MOS,
        status_message=request.args.get('message'),
        status_level=request.args.get('level', 'success')
    )

@app.route('/api/stocks')
def api_stocks():
    stocks = load_stock_records()
    return jsonify(stocks)

@app.route('/api/data')
def api_data():
    stock_records = load_stock_records()
    stocks = [row["symbol"] for row in stock_records]
    stock_flags = {row["symbol"]: row for row in stock_records}
    results = []
    
    # Use ThreadPool for faster fetching
    max_workers = max(1, min(API_MAX_WORKERS, len(stocks) or 1))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = list(executor.map(get_stock_data, stocks))

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

@app.route('/add', methods=['POST'])
def add_stock():
    if not is_valid_csrf_token():
        return build_redirect('คำขอไม่ถูกต้อง กรุณาลองใหม่อีกครั้ง', 'danger')

    raw_input = request.form.get('ticker')
    if not raw_input:
        return build_redirect('กรุณากรอกรายชื่อหุ้นที่ต้องการเพิ่ม', 'warning')

    tickers, invalid_tickers = parse_tickers(raw_input)
    if not tickers:
        return build_redirect('ไม่พบรหัสหุ้นที่ถูกต้อง', 'warning')

    added_count = 0
    for ticker in tickers:
        if add_stock_db(ticker):
            added_count += 1

    if invalid_tickers:
        app.logger.info("Ignored invalid tickers: %s", ", ".join(invalid_tickers))

    message = f'บันทึกรายชื่อหุ้น {added_count} รายการ'
    if invalid_tickers:
        message += f' และข้ามข้อมูลที่ไม่ถูกต้อง {len(invalid_tickers)} รายการ'
    return build_redirect(message, 'success')

@app.route('/remove/<ticker>', methods=['POST'])
def remove_stock(ticker):
    if not is_valid_csrf_token():
        return build_redirect('คำขอไม่ถูกต้อง กรุณาลองใหม่อีกครั้ง', 'danger')

    clean_ticker = normalize_ticker(ticker)
    if not clean_ticker:
        return build_redirect('รหัสหุ้นไม่ถูกต้อง', 'warning')

    if remove_stock_db(clean_ticker):
        return build_redirect(f'ลบหุ้น {clean_ticker} ออกจากรายการส่วนตัวเรียบร้อยแล้ว', 'success')
    return build_redirect(f'ไม่พบหุ้น {clean_ticker} ในรายการส่วนตัว', 'warning')

@app.route('/clear_all', methods=['POST'])
def clear_all_stocks():
    if not is_valid_csrf_token():
        return build_redirect('คำขอไม่ถูกต้อง กรุณาลองใหม่อีกครั้ง', 'danger')

    if clear_all_stocks_db():
        return build_redirect('ลบรายชื่อหุ้นที่เพิ่มเองทั้งหมดเรียบร้อยแล้ว', 'success')
    return build_redirect('ไม่สามารถลบรายชื่อหุ้นทั้งหมดได้', 'danger')

@app.route('/sync_set100', methods=['POST'])
def sync_set100():
    if not is_valid_csrf_token():
        return build_redirect('คำขอไม่ถูกต้อง กรุณาลองใหม่อีกครั้ง', 'danger')

    try:
        status = sync_set100_list(mode="manual")
        message = (
            f"ซิงก์ SET100 สำเร็จ {status['total_symbols']} รายการ "
            f"(เพิ่ม {status['added_count']} / เอาออก {status['removed_count']})"
        )
        return build_redirect(message, 'success')
    except Exception as exc:
        return build_redirect(f'ซิงก์ SET100 ไม่สำเร็จ: {exc}', 'danger')

if __name__ == '__main__':
    app.run(debug=True, port=5000)
