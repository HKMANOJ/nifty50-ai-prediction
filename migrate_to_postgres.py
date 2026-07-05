#!/usr/bin/env python3
"""Migration script to transfer schemas and records from local MySQL to Neon PostgreSQL."""

from __future__ import annotations

import argparse
import sys
from typing import Any
import mysql.connector
import psycopg2

# Schema definitions translated to PostgreSQL dialect
POSTGRES_SCHEMAS = {
    "nifty_candles": """
    CREATE TABLE IF NOT EXISTS nifty_candles (
      id BIGSERIAL PRIMARY KEY,
      symbol VARCHAR(32) NOT NULL,
      timeframe VARCHAR(8) NOT NULL,
      market_time TIMESTAMP NOT NULL,
      market_date DATE NOT NULL,
      open DECIMAL(12,2) NOT NULL,
      high DECIMAL(12,2) NOT NULL,
      low DECIMAL(12,2) NOT NULL,
      close DECIMAL(12,2) NOT NULL,
      volume BIGINT DEFAULT 0,
      source VARCHAR(64),
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      CONSTRAINT uq_candle UNIQUE (symbol, timeframe, market_time)
    );
    CREATE INDEX IF NOT EXISTS idx_candle_date ON nifty_candles (symbol, timeframe, market_date);
    """,
    
    "ai_options": """
    CREATE TABLE IF NOT EXISTS ai_options (
      id BIGSERIAL PRIMARY KEY,
      signal_key VARCHAR(96) NOT NULL UNIQUE,
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      symbol VARCHAR(32) NOT NULL DEFAULT 'NIFTY50',
      timeframe VARCHAR(8) NOT NULL DEFAULT '5m',
      option_side VARCHAR(16) NOT NULL,
      option_name VARCHAR(64),
      status VARCHAR(16) NOT NULL DEFAULT 'OPEN',
      entry_price DECIMAL(12,2) NOT NULL,
      target_price DECIMAL(12,2) NOT NULL,
      stop_loss DECIMAL(12,2) NOT NULL,
      current_price DECIMAL(12,2),
      exit_price DECIMAL(12,2),
      confidence INT,
      pattern_name VARCHAR(128),
      reason TEXT,
      execution_quality VARCHAR(32),
      premium_entry DECIMAL(12,2),
      premium_target DECIMAL(12,2),
      premium_stop_loss DECIMAL(12,2),
      premium_current DECIMAL(12,2),
      premium_exit DECIMAL(12,2),
      pnl_premium DECIMAL(12,2),
      opened_market_time TIMESTAMP,
      closed_market_time TIMESTAMP,
      result_points DECIMAL(12,2),
      option_type VARCHAR(16),
      mfe DECIMAL(12,2),
      mae DECIMAL(12,2)
    );
    CREATE INDEX IF NOT EXISTS idx_ai_options_status ON ai_options (status, created_at);
    CREATE INDEX IF NOT EXISTS idx_ai_options_timeframe ON ai_options (symbol, timeframe, created_at);
    """,
    
    "opportunity_audit": """
    CREATE TABLE IF NOT EXISTS opportunity_audit (
      id BIGSERIAL PRIMARY KEY,
      audit_key VARCHAR(180) NOT NULL UNIQUE,
      market_time TIMESTAMP NOT NULL,
      symbol VARCHAR(32) NOT NULL DEFAULT 'NIFTY50',
      timeframe VARCHAR(8) NOT NULL DEFAULT '5m',
      signal_side VARCHAR(16) NOT NULL,
      pattern_name VARCHAR(128),
      entry_candidate DECIMAL(12,2),
      target_candidate DECIMAL(12,2),
      stop_candidate DECIMAL(12,2),
      confidence INT,
      pattern_confidence INT,
      oi_confidence INT,
      min_move_points DECIMAL(12,2),
      rejection_reasons TEXT,
      session_valid SMALLINT DEFAULT 0,
      premium_available SMALLINT DEFAULT 0,
      nse_confirmation_available SMALLINT DEFAULT 0,
      future_max_move_points DECIMAL(12,2),
      future_reached_50_points SMALLINT,
      future_reached_target SMALLINT,
      future_hit_stop_first SMALLINT,
      candles_to_reach_50 INT,
      candles_evaluated INT DEFAULT 0,
      final_verdict VARCHAR(32) NOT NULL DEFAULT 'PENDING',
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_opportunity_time ON opportunity_audit (symbol, timeframe, market_time);
    CREATE INDEX IF NOT EXISTS idx_opportunity_verdict ON opportunity_audit (final_verdict, created_at);
    CREATE INDEX IF NOT EXISTS idx_opportunity_pattern ON opportunity_audit (pattern_name);
    """,
    
    "PatternPerformance": """
    CREATE TABLE IF NOT EXISTS PatternPerformance (
      id BIGSERIAL PRIMARY KEY,
      pattern_name VARCHAR(128) NOT NULL UNIQUE,
      total_trades INT NOT NULL DEFAULT 0,
      wins INT NOT NULL DEFAULT 0,
      losses INT NOT NULL DEFAULT 0,
      accuracy DECIMAL(7,2) NOT NULL DEFAULT 0,
      net_profit_points DECIMAL(12,2) NOT NULL DEFAULT 0,
      average_win DECIMAL(12,2) NOT NULL DEFAULT 0,
      average_loss DECIMAL(12,2) NOT NULL DEFAULT 0,
      profit_factor DECIMAL(12,4) NOT NULL DEFAULT 0,
      max_drawdown DECIMAL(12,2) NOT NULL DEFAULT 0,
      weight_multiplier DECIMAL(8,4) NOT NULL DEFAULT 1,
      last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_pattern_perf_updated ON PatternPerformance (last_updated);
    """,
    
    "MarketRegime": """
    CREATE TABLE IF NOT EXISTS MarketRegime (
      id BIGSERIAL PRIMARY KEY,
      regime_name VARCHAR(64) NOT NULL,
      pattern_name VARCHAR(128) NOT NULL,
      total_trades INT NOT NULL DEFAULT 0,
      wins INT NOT NULL DEFAULT 0,
      losses INT NOT NULL DEFAULT 0,
      accuracy DECIMAL(7,2) NOT NULL DEFAULT 0,
      net_profit_points DECIMAL(12,2) NOT NULL DEFAULT 0,
      average_win DECIMAL(12,2) NOT NULL DEFAULT 0,
      average_loss DECIMAL(12,2) NOT NULL DEFAULT 0,
      profit_factor DECIMAL(12,4) NOT NULL DEFAULT 0,
      max_drawdown DECIMAL(12,2) NOT NULL DEFAULT 0,
      weight_multiplier DECIMAL(8,4) NOT NULL DEFAULT 1,
      last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      CONSTRAINT uq_regime_pattern UNIQUE (regime_name, pattern_name)
    );
    CREATE INDEX IF NOT EXISTS idx_regime_perf_updated ON MarketRegime (last_updated);
    """
}

def migrate_table(mysql_conn: Any, pg_conn: Any, table_name: str) -> None:
    mysql_cursor = mysql_conn.cursor(buffered=True)
    pg_cursor = pg_conn.cursor()
    
    # 1. Fetch column names from MySQL table
    try:
        mysql_cursor.execute(f"SELECT * FROM {table_name} LIMIT 0")
        columns = [desc[0] for desc in mysql_cursor.description if desc[0] != "id"]
    except mysql.connector.Error as e:
        print(f"Skipping table {table_name}: does not exist in local MySQL ({e}).")
        mysql_cursor.close()
        pg_cursor.close()
        return

    print(f"Migrating table {table_name}...")
    
    # 2. Fetch all rows from MySQL
    mysql_cursor.execute(f"SELECT {', '.join(columns)} FROM {table_name}")
    rows = mysql_cursor.fetchall()
    print(f"  Found {len(rows)} records in local MySQL.")
    
    if not rows:
        mysql_cursor.close()
        pg_cursor.close()
        return
        
    # 3. Create schema in PostgreSQL
    print(f"  Dropping and re-creating table in PostgreSQL to ensure correct schema...")
    pg_cursor.execute(f"DROP TABLE IF EXISTS {table_name} CASCADE")
    schema_sql = POSTGRES_SCHEMAS[table_name]
    pg_cursor.execute(schema_sql)
    pg_conn.commit()
    
    # 4. Insert rows into PostgreSQL using execute_values for high-speed bulk upload
    from psycopg2.extras import execute_values
    insert_sql = f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES %s ON CONFLICT DO NOTHING"
    
    print(f"  Uploading records to Neon PostgreSQL in high-speed batches...")
    execute_values(pg_cursor, insert_sql, rows, page_size=1000)
    pg_conn.commit()
        
    print(f"  Successfully migrated {table_name}!")
    
    mysql_cursor.close()
    pg_cursor.close()

def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate NIFTY MySQL data to Neon PostgreSQL.")
    parser.add_argument("--pg-url", required=True, help="Neon PostgreSQL Connection URL")
    args = parser.parse_args()
    
    # 1. Connect to MySQL
    sys.path.append("/Users/hkmanoj/Documents/Codex/2026-05-29/files-mentioned-by-the-user-nifty50")
    from mysql_config import mysql_settings
    mysql_conf = mysql_settings()
    
    print("Connecting to local MySQL...")
    try:
        mysql_conn = mysql.connector.connect(**mysql_conf)
    except Exception as e:
        print(f"Error connecting to MySQL: {e}")
        sys.exit(1)
        
    # 2. Connect to PostgreSQL
    print("Connecting to Neon PostgreSQL...")
    try:
        pg_conn = psycopg2.connect(args.pg_url)
    except Exception as e:
        print(f"Error connecting to PostgreSQL: {e}")
        mysql_conn.close()
        sys.exit(1)
        
    try:
        # Migrate all tables
        for table in POSTGRES_SCHEMAS.keys():
            migrate_table(mysql_conn, pg_conn, table)
        print("\nDatabase Migration completed successfully!")
    finally:
        mysql_conn.close()
        pg_conn.close()

if __name__ == "__main__":
    main()
