import json
import os
import shutil
import sqlite3

from flask import current_app


INITIAL_STOCKS = [
    "AAV",
    "ADVANC",
    "AEONTS",
    "AMATA",
    "AOT",
    "AP",
    "AURA",
    "AWC",
    "BA",
    "BAM",
    "BANPU",
    "BBL",
    "BCH",
    "BCP",
    "BCPG",
    "BDMS",
    "BEM",
    "BGRIM",
    "BH",
    "BJC",
    "BLA",
    "BTG",
    "BTS",
    "CBG",
    "CCET",
    "CENTEL",
    "CHG",
    "CK",
    "COM7",
    "CPALL",
    "CPF",
    "CPN",
    "CRC",
    "DELTA",
    "DOHOME",
    "EA",
    "EGCO",
    "ERW",
    "GFPT",
    "GLOBAL",
    "GPSC",
    "GULF",
    "GUNKUL",
    "HANA",
    "HMPRO",
    "ICHI",
    "IRPC",
    "IVL",
    "JAS",
    "JMART",
    "JMT",
    "JTS",
    "KBANK",
    "KCE",
    "KKP",
    "KTB",
    "KTC",
    "LH",
    "M",
    "MEGA",
    "MINT",
    "MOSHI",
    "MTC",
    "OR",
    "OSP",
    "PLANB",
    "PR9",
    "PRM",
    "PTG",
    "PTT",
    "PTTEP",
    "PTTGC",
    "QH",
    "RATCH",
    "RCL",
    "SAWAD",
    "SCB",
    "SCC",
    "SCGP",
    "SIRI",
    "SISB",
    "SJWD",
    "SPALI",
    "SPRC",
    "STA",
    "STECON",
    "STGT",
    "TASCO",
    "TCAP",
    "TFG",
    "TIDLOR",
    "TISCO",
    "TLI",
    "TOA",
    "TOP",
    "TRUE",
    "TTB",
    "TU",
    "VGI",
    "WHA",
]


def get_base_dir():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def get_db_paths():
    base_dir = get_base_dir()
    file_name = current_app.config["DB_FILE_NAME"]
    stocks_file_name = current_app.config["STOCKS_FILE_NAME"]

    source_db_path = os.path.join(base_dir, file_name)
    source_stocks_path = os.path.join(base_dir, stocks_file_name)

    if current_app.config.get("IS_VERCEL"):
        db_path = os.path.join("/tmp", file_name)
    else:
        db_path = source_db_path

    return db_path, source_db_path, source_stocks_path


def get_db_connection():
    db_path, _, _ = get_db_paths()
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def ensure_stocks_schema(conn):
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(stocks)").fetchall()}

    if "is_manual" not in columns:
        conn.execute("ALTER TABLE stocks ADD COLUMN is_manual INTEGER NOT NULL DEFAULT 0")
    if "is_set100" not in columns:
        conn.execute("ALTER TABLE stocks ADD COLUMN is_set100 INTEGER NOT NULL DEFAULT 0")

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


def init_db():
    db_path, source_db_path, source_stocks_path = get_db_paths()

    if current_app.config.get("IS_VERCEL") and not os.path.exists(db_path) and os.path.exists(source_db_path):
        shutil.copy2(source_db_path, db_path)

    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stocks (
                symbol TEXT PRIMARY KEY,
                is_manual INTEGER NOT NULL DEFAULT 0,
                is_set100 INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS translations (
                symbol TEXT PRIMARY KEY,
                description_th TEXT
            )
            """
        )
        ensure_stocks_schema(conn)
        ensure_sync_schema(conn)

        count = conn.execute(
            "SELECT count(*) AS count FROM stocks WHERE is_manual = 1 OR is_set100 = 1"
        ).fetchone()["count"]
        if count:
            conn.commit()
            return

        initial_data = None
        if os.path.exists(source_stocks_path):
            try:
                with open(source_stocks_path, "r", encoding="utf-8") as handle:
                    initial_data = json.load(handle)
            except Exception as exc:
                current_app.logger.warning(
                    "Unable to read %s, using defaults: %s", source_stocks_path, exc
                )

        if not initial_data:
            initial_data = INITIAL_STOCKS

        unique_stocks = sorted(set(initial_data))
        conn.executemany(
            "INSERT OR IGNORE INTO stocks (symbol, is_manual, is_set100) VALUES (?, 0, 1)",
            [(s,) for s in unique_stocks],
        )
        conn.commit()
        current_app.logger.info("Initialized DB with %s stocks.", len(unique_stocks))
    finally:
        conn.close()


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
        current_app.logger.warning("Unable to add stock %s: %s", symbol, exc)
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
        conn.execute(
            "DELETE FROM stocks WHERE symbol = ? AND is_manual = 0 AND is_set100 = 0",
            (symbol,),
        )
        conn.commit()
        return cursor.rowcount > 0
    except sqlite3.Error as exc:
        current_app.logger.warning("Unable to remove stock %s: %s", symbol, exc)
        return False
    finally:
        conn.close()


def clear_all_manual_stocks_db():
    conn = get_db_connection()
    try:
        conn.execute("UPDATE stocks SET is_manual = 0 WHERE is_manual = 1")
        conn.execute("DELETE FROM stocks WHERE is_manual = 0 AND is_set100 = 0")
        conn.commit()
        return True
    except sqlite3.Error as exc:
        current_app.logger.warning("Unable to clear all stocks: %s", exc)
        return False
    finally:
        conn.close()


def get_translation_db(symbol):
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT description_th FROM translations WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        if row:
            return row["description_th"]
    except sqlite3.Error as exc:
        current_app.logger.warning("Unable to load translation cache for %s: %s", symbol, exc)
    finally:
        conn.close()
    return None


def save_translation_db(symbol, text):
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO translations (symbol, description_th) VALUES (?, ?)",
            (symbol, text),
        )
        conn.commit()
    except sqlite3.Error as exc:
        current_app.logger.warning("Unable to save translation cache for %s: %s", symbol, exc)
    finally:
        conn.close()


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


def save_set100_sync_status(status, conn=None, commit=True):
    own_conn = conn is None
    if own_conn:
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
        if own_conn:
            conn.close()
