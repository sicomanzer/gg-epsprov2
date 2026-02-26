import json
import os
import sqlite3
from flask import Flask, render_template, request, jsonify, redirect, url_for
import yfinance as yf
import pandas as pd
from concurrent.futures import ThreadPoolExecutor

# Set yfinance cache path to a local directory to avoid permission issues
try:
    yf.set_tz_cache_location("yfinance_cache")
except:
    pass

app = Flask(__name__)

# --- Vercel Configuration ---
# Vercel file system is read-only except for /tmp
# We need to copy the DB to /tmp to make it writable (but data is ephemeral)
import shutil

DB_FILE = 'stocks.db'
STOCKS_FILE = 'stocks.json' # Keep for migration

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
except:
    pass

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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    # If on Vercel and DB not in /tmp, initialize it (copy from source or create new)
    if IS_VERCEL and not os.path.exists(DB_PATH):
        # On Vercel, copy the bundled DB to /tmp
        if os.path.exists(DB_FILE):
             shutil.copy2(DB_FILE, DB_PATH)
    
    # Only run init if we are creating a fresh DB
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS stocks
                 (symbol TEXT PRIMARY KEY)''')
    
    # Check if empty
    c.execute('SELECT count(*) FROM stocks')
    count = c.fetchone()[0]
    
    if count == 0:
        # Migrate from JSON if exists, else use INITIAL_STOCKS
        initial_data = []
        if os.path.exists(STOCKS_FILE):
            try:
                with open(STOCKS_FILE, 'r') as f:
                    initial_data = json.load(f)
            except:
                initial_data = INITIAL_STOCKS
        else:
            initial_data = INITIAL_STOCKS
            
        # Bulk insert
        # Ensure unique
        unique_stocks = list(set(initial_data))
        c.executemany('INSERT OR IGNORE INTO stocks (symbol) VALUES (?)', [(s,) for s in unique_stocks])
        conn.commit()
        print(f"Initialized DB with {len(unique_stocks)} stocks.")
        
    conn.close()

# Initialize DB on startup
# Use before_request to avoid cold start timeouts
@app.before_request
def initialize():
    if not getattr(app, 'db_initialized', False):
        try:
            init_db()
            app.db_initialized = True
        except Exception as e:
            print(f"DB Init Error: {e}")

@app.route('/health')
def health_check():
    return jsonify({"status": "ok", "vercel": IS_VERCEL})

def load_stocks():
    # Helper to get connection based on environment
    conn = get_db_connection()
    try:
        stocks = conn.execute('SELECT symbol FROM stocks ORDER BY symbol').fetchall()
    except Exception:
        return []
    conn.close()
    return [s['symbol'] for s in stocks]

def add_stock_db(symbol):
    conn = get_db_connection()
    try:
        conn.execute('INSERT OR IGNORE INTO stocks (symbol) VALUES (?)', (symbol,))
        conn.commit()
    except:
        pass
    finally:
        conn.close()

def remove_stock_db(symbol):
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM stocks WHERE symbol = ?', (symbol,))
        conn.commit()
    except:
        pass
    finally:
        conn.close()

def clear_all_stocks_db():
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM stocks')
        conn.commit()
    except:
        pass
    finally:
        conn.close()

def get_stock_data(ticker):
    try:
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

        # Fetch Historical Data (Trend 5Y EPS & 10Y Dividends)
        eps_trend = []
        div_trend = []
        import datetime
        current_year = datetime.datetime.now().year
        
        try:
            # EPS Trend (Last 5 Years, ending at current_year - 1 i.e., 2025)
            # Since user indicated 2026 is too early and 2025 is the latest (even if partial)
            financials = stock.financials
            eps_trend = [None] * 5 
            # 2021, 2022, 2023, 2024, 2025
            years_eps = [current_year - 5, current_year - 4, current_year - 3, current_year - 2, current_year - 1]
            
            if "Basic EPS" in financials.index:
                eps_series = financials.loc["Basic EPS"]
                # Convert to dict for easy lookup {year: value}
                eps_dict = {d.year: float(v) for d, v in eps_series.items() if not pd.isna(v)}
                
                # Fill aligned list
                eps_trend = []
                for y in years_eps:
                    val = eps_dict.get(y)
                    
                    # Fallback strategies for missing data
                    if val is None:
                        # If 2025 (current_year - 1) is missing, try Trailing EPS (TTM)
                        if y == current_year - 1:
                            val = get_val("trailingEps", None)
                            if val == "-": val = None
                            
                    eps_trend.append(val) 
            else:
                # If no financials, try to fill with TTM for 2025
                eps_trend = [None] * 5
                # Try 2025
                t_eps = get_val("trailingEps", None)
                if t_eps != "-": eps_trend[4] = t_eps # 2025
            
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
                start_year = current_year - 10 # 2016 to 2025
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
            g_high = 0.03      # 3% Growth
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
        if ddm_value != "-" and price != "-" and price > 0 and ddm_value > 0:
            mos = round(((ddm_value - price) / ddm_value) * 100, 2)

        # --- Quality Score Calculation (Magic Score) ---
        score = 0
        score_details = []
        
        # 1. Valuation: P/E < 20 (Conservative)
        pe = get_float("trailingPE")
        if pe != "-" and pe < 20 and pe > 0: 
            score += 1
            score_details.append("P/E < 20")
        
        # 2. Valuation: PEG < 1.5 (Growth at reasonable price)
        # PEG = P/E / Growth Rate (Earnings Growth)
        # If Earnings Growth is 0.20 (20%), and P/E is 20, PEG = 1.0
        eg = get_float("earningsGrowth")
        peg = "-"
        if pe != "-" and eg != "-" and eg > 0:
            peg = pe / (eg * 100) # eg is decimal (0.2), we need 20.
            if peg < 1.5:
                score += 1
                score_details.append(f"PEG < 1.5 ({peg:.2f})")
        
        # 3. Valuation: Price < DDM (Margin of Safety)
        if ddm_value != "-" and price != "-" and price < ddm_value: 
            score += 1
            score_details.append(f"Price < DDM (k={k_percent}%)")
        
        # 4. Efficiency: ROE > 12%
        roe = get_float("returnOnEquity")
        if roe != "-" and roe > 0.12: 
            score += 1
            score_details.append("ROE > 12%")
        
        # 5. Financial Health: D/E < 1.5
        de = debt_to_equity
        if de != "-" and de < 1.5: 
            score += 1
            score_details.append("D/E < 1.5")
        
        # 6. Dividend: Yield > 3%
        dy = get_float("dividendYield", 1) 
        if dy != "-" and dy > 3: 
            score += 1
            score_details.append("Yield > 3%")
        
        # 7. Growth: Earnings Growth > 5%
        if eg != "-" and eg > 0.05: 
            score += 1
            score_details.append("Earn Growth > 5%")

        # 8. Technical: RSI < 50 (Good Entry / Not Overbought)
        if rsi != "-" and rsi < 50:
            score += 1
            score_details.append(f"RSI < 50 ({rsi})")

        # 9. Risk: MOS > 10% (Significant Safety Margin)
        if mos != "-" and mos > 10:
            score += 1
            score_details.append(f"MOS > 10% ({mos}%)")

        # 10. Bonus: Grade A if Score >= 7
        # Current Max Score is 9
        
        # Grade Assignment (Adjusted for max score 10)
        # We can add one more criteria or just assume 9/10 is A+
        # User requested: 7-10 = A
        
        # Let's add one more criteria to make it 10 points max
        # 10. Efficiency: Net Margin > 10%
        nm = get_float("profitMargins")
        if nm != "-" and nm > 0.10:
            score += 1
            score_details.append("Net Margin > 10%")

        # A: 7-10, B: 5-6, C: 3-4, D: 0-2
        grade = "D"
        if score >= 7: grade = "A"
        elif score >= 5: grade = "B"
        elif score >= 3: grade = "C"

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
            "details": {
                "roa": get_val("returnOnAssets", "-"),
                "roe": get_val("returnOnEquity", "-"),
                "gross_margin": get_val("grossMargins", "-"),
                "operating_margin": get_val("operatingMargins", "-"),
                "profit_margin": get_val("profitMargins", "-"),
                "debt_to_equity": debt_to_equity,
                "current_ratio": get_val("currentRatio", "-"),
                "quick_ratio": get_val("quickRatio", "-"),
                "book_value": get_val("bookValue", "-"),
                "price_to_book": get_val("priceToBook", "-"),
                "industry": get_val("industry", "-"),
                "sector": get_val("sector", "-"),
                "description": get_val("longBusinessSummary", "-")
            }
        }
    except Exception as e:
        print(f"Error fetching {ticker}: {e}")
        return {
            "symbol": ticker,
            "error": "Data Unavailable"
        }

@app.route('/')
def index():
    stocks = load_stocks()
    return render_template('index.html', stocks=stocks)

@app.route('/api/stocks')
def api_stocks():
    stocks = load_stocks()
    return jsonify(stocks)

@app.route('/api/data')
def api_data():
    stocks = load_stocks()
    results = []
    
    # Use ThreadPool for faster fetching
    with ThreadPoolExecutor(max_workers=30) as executor:
        results = list(executor.map(get_stock_data, stocks))
        
    return jsonify(results)

@app.route('/add', methods=['POST'])
def add_stock():
    raw_input = request.form.get('ticker')
    if raw_input:
        # Normalize: Replace newlines and commas with space, then split
        import re
        tickers = re.split(r'[,\s\n]+', raw_input)
        
        for t in tickers:
            clean_ticker = t.upper().strip()
            if clean_ticker:
                add_stock_db(clean_ticker)
            
    return redirect(url_for('index'))

@app.route('/remove/<ticker>')
def remove_stock(ticker):
    remove_stock_db(ticker)
    return redirect(url_for('index'))

@app.route('/clear_all', methods=['POST'])
def clear_all_stocks():
    clear_all_stocks_db()
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(debug=True, port=5000)
