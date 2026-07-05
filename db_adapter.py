#!/usr/bin/env python3
"""Unified database adapter for NIFTY AI supporting local MySQL and Neon PostgreSQL."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

# Load environment variables
ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"

def load_dotenv(path: Path = ENV_PATH) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

load_dotenv()

# Determine database engine
DATABASE_URL = os.environ.get("DATABASE_URL")
DB_ENGINE = "postgres" if DATABASE_URL else "mysql"

def translate_query(query: str, db_type: str) -> str:
    """Translates SQL statements to align with target database engine syntax."""
    if not query:
        return query
    
    clean = query.strip()
    
    if db_type == "postgres":
        # 1. Translate AUTO_INCREMENT -> BIGSERIAL
        clean = re.sub(r'\bBIGINT\s+AUTO_INCREMENT\b', 'BIGSERIAL', clean, flags=re.IGNORECASE)
        clean = re.sub(r'\bINT\s+AUTO_INCREMENT\b', 'SERIAL', clean, flags=re.IGNORECASE)
        
        # 2. Remove MySQL-specific indices declaration inside CREATE TABLE (they should be created as separate INDEX statements)
        # Match e.g. "INDEX idx_name (col1, col2)"
        clean = re.sub(r',\s*INDEX\s+\w+\s*\([^)]+\)', '', clean, flags=re.IGNORECASE)
        # Match e.g. "UNIQUE KEY uq_name (col1, col2)" and convert to "CONSTRAINT uq_name UNIQUE (col1, col2)"
        clean = re.sub(r'\bUNIQUE\s+KEY\s+(\w+)\s*\(([^)]+)\)', r'CONSTRAINT \1 UNIQUE (\2)', clean, flags=re.IGNORECASE)
        
        # 3. Translate DATETIME -> TIMESTAMP
        clean = re.sub(r'\bDATETIME\b', 'TIMESTAMP', clean, flags=re.IGNORECASE)
        
        # 4. Remove MySQL-specific ON UPDATE CURRENT_TIMESTAMP
        clean = re.sub(r'\bON\s+UPDATE\s+CURRENT_TIMESTAMP\b', '', clean, flags=re.IGNORECASE)
        
        # 5. Translate TINYINT(1) -> SMALLINT
        clean = re.sub(r'\bTINYINT\(1\)\b', 'SMALLINT', clean, flags=re.IGNORECASE)
        
        # 6. Translate ON DUPLICATE KEY UPDATE statements
        if "ON DUPLICATE KEY UPDATE" in clean:
            # nifty_candles table
            if "nifty_candles" in clean:
                return """
                INSERT INTO nifty_candles
                  (symbol, timeframe, market_time, market_date, open, high, low, close, volume, source)
                VALUES
                  (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, timeframe, market_time) DO UPDATE SET
                  open = EXCLUDED.open,
                  high = EXCLUDED.high,
                  low = EXCLUDED.low,
                  close = EXCLUDED.close,
                  volume = EXCLUDED.volume,
                  source = EXCLUDED.source
                """
            # ai_options table
            if "ai_options" in clean:
                return """
                INSERT INTO ai_options (
                  signal_key, symbol, timeframe, option_side, option_name, status,
                  entry_price, target_price, stop_loss, current_price, exit_price,
                  confidence, pattern_name, reason, execution_quality,
                  premium_entry, premium_target, premium_stop_loss, premium_current, premium_exit,
                  pnl_premium, opened_market_time, closed_market_time, result_points
                ) VALUES (
                  %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                ) ON CONFLICT (signal_key) DO UPDATE SET
                  status = EXCLUDED.status,
                  current_price = EXCLUDED.current_price,
                  exit_price = EXCLUDED.exit_price,
                  premium_current = EXCLUDED.premium_current,
                  premium_exit = EXCLUDED.premium_exit,
                  pnl_premium = EXCLUDED.pnl_premium,
                  closed_market_time = EXCLUDED.closed_market_time,
                  result_points = EXCLUDED.result_points,
                  execution_quality = EXCLUDED.execution_quality,
                  updated_at = CURRENT_TIMESTAMP
                """
            # opportunity_audit table
            if "opportunity_audit" in clean:
                return """
                INSERT INTO opportunity_audit (
                  audit_key, market_time, symbol, timeframe, signal_side, pattern_name,
                  entry_candidate, target_candidate, stop_candidate, confidence,
                  pattern_confidence, oi_confidence, min_move_points, rejection_reasons,
                  session_valid, premium_available, nse_confirmation_available,
                  future_max_move_points, future_reached_50_points, future_reached_target,
                  future_hit_stop_first, candles_to_reach_50, candles_evaluated, final_verdict
                ) VALUES (
                  %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                ) ON CONFLICT (audit_key) DO UPDATE SET
                  future_max_move_points = EXCLUDED.future_max_move_points,
                  future_reached_50_points = EXCLUDED.future_reached_50_points,
                  future_reached_target = EXCLUDED.future_reached_target,
                  future_hit_stop_first = EXCLUDED.future_hit_stop_first,
                  candles_to_reach_50 = EXCLUDED.candles_to_reach_50,
                  candles_evaluated = EXCLUDED.candles_evaluated,
                  final_verdict = EXCLUDED.final_verdict,
                  updated_at = CURRENT_TIMESTAMP
                """
            # PatternPerformance table
            if "PatternPerformance" in clean:
                return """
                INSERT INTO PatternPerformance
                  (pattern_name, total_trades, wins, losses, accuracy, net_profit_points,
                   average_win, average_loss, profit_factor, max_drawdown, weight_multiplier)
                VALUES
                  (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (pattern_name) DO UPDATE SET
                  total_trades = EXCLUDED.total_trades,
                  wins = EXCLUDED.wins,
                  losses = EXCLUDED.losses,
                  accuracy = EXCLUDED.accuracy,
                  net_profit_points = EXCLUDED.net_profit_points,
                  average_win = EXCLUDED.average_win,
                  average_loss = EXCLUDED.average_loss,
                  profit_factor = EXCLUDED.profit_factor,
                  max_drawdown = EXCLUDED.max_drawdown,
                  weight_multiplier = EXCLUDED.weight_multiplier,
                  last_updated = CURRENT_TIMESTAMP
                """
            # MarketRegime table
            if "MarketRegime" in clean:
                return """
                INSERT INTO MarketRegime
                  (regime_name, pattern_name, total_trades, wins, losses, accuracy, net_profit_points,
                   average_win, average_loss, profit_factor, max_drawdown, weight_multiplier)
                VALUES
                  (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (regime_name, pattern_name) DO UPDATE SET
                  total_trades = EXCLUDED.total_trades,
                  wins = EXCLUDED.wins,
                  losses = EXCLUDED.losses,
                  accuracy = EXCLUDED.accuracy,
                  net_profit_points = EXCLUDED.net_profit_points,
                  average_win = EXCLUDED.average_win,
                  average_loss = EXCLUDED.average_loss,
                  profit_factor = EXCLUDED.profit_factor,
                  max_drawdown = EXCLUDED.max_drawdown,
                  weight_multiplier = EXCLUDED.weight_multiplier,
                  last_updated = CURRENT_TIMESTAMP
                """
    return clean

class CursorWrapper:
    def __init__(self, cursor: Any, engine: str) -> None:
        self.cursor = cursor
        self.engine = engine

    def execute(self, query: str, params: Any = None) -> Any:
        translated = translate_query(query, self.engine)
        # Convert %s placeholders in query to psycopg2 format if needed
        return self.cursor.execute(translated, params or ())

    def executemany(self, query: str, seq_of_params: Any) -> Any:
        translated = translate_query(query, self.engine)
        if self.engine == "postgres":
            from psycopg2.extras import execute_values
            # Translate VALUES (%s, %s, ...) to VALUES %s for psycopg2 execute_values
            translated_clean = re.sub(r'VALUES\s*\(\s*%s(?:\s*,\s*%s)*\s*\)', 'VALUES %s', translated, flags=re.IGNORECASE)
            return execute_values(self.cursor, translated_clean, seq_of_params)
        else:
            return self.cursor.executemany(translated, seq_of_params)

    def fetchone(self) -> Any:
        return self.cursor.fetchone()

    def fetchall(self) -> Any:
        return self.cursor.fetchall()

    def close(self) -> None:
        self.cursor.close()

    def __iter__(self) -> Any:
        return iter(self.cursor)

    def __next__(self) -> Any:
        return next(self.cursor)

    @property
    def description(self) -> Any:
        return self.cursor.description

class ConnectionWrapper:
    def __init__(self) -> None:
        self.conn: Any = None
        self.engine = DB_ENGINE

    def connect(self) -> ConnectionWrapper:
        if self.engine == "postgres":
            import psycopg2
            self.conn = psycopg2.connect(DATABASE_URL)
        else:
            import mysql.connector
            from mysql_config import mysql_settings
            self.conn = mysql.connector.connect(**mysql_settings())
        return self

    def cursor(self, dictionary: bool = False) -> CursorWrapper:
        if self.engine == "postgres":
            if dictionary:
                from psycopg2.extras import RealDictCursor
                native_cursor = self.conn.cursor(cursor_factory=RealDictCursor)
            else:
                native_cursor = self.conn.cursor()
        else:
            if dictionary:
                native_cursor = self.conn.cursor(dictionary=True)
            else:
                native_cursor = self.conn.cursor()
        return CursorWrapper(native_cursor, self.engine)

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def close(self) -> None:
        if self.conn:
            self.conn.close()

def get_db_connection() -> ConnectionWrapper:
    wrapper = ConnectionWrapper()
    return wrapper.connect()
