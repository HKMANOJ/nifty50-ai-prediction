#!/usr/bin/env python3
"""Auto-fetch Most Active Puts and Calls from NSE website."""

import json
import csv
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError

ROOT = Path(__file__).resolve().parent
OUTPUTS = {
    'puts': ROOT / "inputs" / "most_active_puts.latest.json",
    'calls': ROOT / "inputs" / "most_active_calls.latest.json",
    'combined': ROOT / "inputs" / "most_active.latest.json",
}

DOWNLOADS_CSV = Path.home() / "Downloads" / "ED-Most-Active-puts-index-volume-18-Jun-2026.csv"

USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'


def fetch_from_csv_fallback():
    """Fallback: Load from CSV file if network unavailable."""
    try:
        if not DOWNLOADS_CSV.exists():
            print(f"✗ CSV fallback file not found: {DOWNLOADS_CSV}")
            return [], []

        print(f"📂 Using CSV fallback: {DOWNLOADS_CSV.name}")

        puts = []
        calls = []

        with open(DOWNLOADS_CSV, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if not row or all(v is None or v.strip() == '' for v in row.values()):
                    continue

                # Clean column names
                row_clean = {k.strip().lower(): v.strip() if isinstance(v, str) else v for k, v in row.items() if k}

                normalized = {}
                for key, value in row_clean.items():
                    if 'symbol' in key:
                        normalized['symbol'] = value
                    elif 'strike' in key:
                        try:
                            normalized['strikeOrExpiry'] = float(value.replace(',', ''))
                        except:
                            normalized['strikeOrExpiry'] = value
                    elif 'option type' in key:
                        normalized['type'] = value.upper()
                    elif 'ltp' in key:
                        try:
                            normalized['ltp'] = float(value.replace(',', ''))
                        except:
                            normalized['ltp'] = 0
                    elif 'volume' in key and 'contracts' in key:
                        try:
                            normalized['volume'] = int(value.replace(',', ''))
                        except:
                            normalized['volume'] = 0
                    elif 'open interest' in key:
                        try:
                            normalized['oi'] = int(value.replace(',', ''))
                        except:
                            normalized['oi'] = 0
                    elif 'underlying' in key:
                        try:
                            normalized['underlying'] = float(value.replace(',', ''))
                        except:
                            normalized['underlying'] = 0

                if normalized.get('symbol'):
                    if normalized.get('type') == 'PUT':
                        puts.append(normalized)
                    elif normalized.get('type') == 'CALL':
                        calls.append(normalized)

        # Sort by volume and get top 15
        puts = sorted(puts, key=lambda x: x.get('volume', 0), reverse=True)[:15]
        calls = sorted(calls, key=lambda x: x.get('volume', 0), reverse=True)[:15]

        print(f"✓ Loaded {len(puts)} PUTs and {len(calls)} CALLs from CSV")
        return puts, calls

    except Exception as e:
        print(f"✗ CSV fallback error: {e}")
        return [], []


def fetch_nse_most_active_puts():
    """Fetch most active PUT contracts from NSE."""
    try:
        print("📡 Fetching Most Active PUTS from NSE...")

        # Try NSE option chain API
        url = 'https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY'

        headers = {
            'User-Agent': USER_AGENT,
            'Referer': 'https://www.nseindia.com/',
            'Accept': 'application/json',
            'Accept-Language': 'en-US,en;q=0.9'
        }

        req = Request(url, headers=headers)
        response = urlopen(req, timeout=10)
        data = json.loads(response.read().decode('utf-8'))

        if not data or 'records' not in data:
            print("⚠ API response incomplete, using CSV fallback...")
            return None

        # Extract PUT data
        puts = []
        for record in data.get('records', {}).get('data', []):
            pe = record.get('PE', {})
            if pe and pe.get('totalTradedVolume', 0) > 0:
                puts.append({
                    'symbol': 'NIFTY',
                    'strikeOrExpiry': pe.get('strikePrice', 0),
                    'type': 'PUT',
                    'ltp': pe.get('lastPrice', 0),
                    'volume': pe.get('totalTradedVolume', 0),
                    'oi': pe.get('openInterest', 0),
                    'underlying': data.get('records', {}).get('underlyingValue', 0),
                })

        # Sort by volume descending, take top 15
        puts = sorted(puts, key=lambda x: x['volume'], reverse=True)[:15]
        print(f"✓ Fetched {len(puts)} PUT contracts from NSE API")
        return puts

    except Exception as e:
        print(f"⚠ NSE API error: {e}, using CSV fallback...")
        return None


def fetch_nse_most_active_calls():
    """Fetch most active CALL contracts from NSE."""
    try:
        print("📡 Fetching Most Active CALLS from NSE...")

        # Try NSE option chain API
        url = 'https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY'

        headers = {
            'User-Agent': USER_AGENT,
            'Referer': 'https://www.nseindia.com/',
            'Accept': 'application/json',
            'Accept-Language': 'en-US,en;q=0.9'
        }

        req = Request(url, headers=headers)
        response = urlopen(req, timeout=10)
        data = json.loads(response.read().decode('utf-8'))

        if not data or 'records' not in data:
            print("⚠ API response incomplete, using CSV fallback...")
            return None

        # Extract CALL data
        calls = []
        for record in data.get('records', {}).get('data', []):
            ce = record.get('CE', {})
            if ce and ce.get('totalTradedVolume', 0) > 0:
                calls.append({
                    'symbol': 'NIFTY',
                    'strikeOrExpiry': ce.get('strikePrice', 0),
                    'type': 'CALL',
                    'ltp': ce.get('lastPrice', 0),
                    'volume': ce.get('totalTradedVolume', 0),
                    'oi': ce.get('openInterest', 0),
                    'underlying': data.get('records', {}).get('underlyingValue', 0),
                })

        # Sort by volume descending, take top 15
        calls = sorted(calls, key=lambda x: x['volume'], reverse=True)[:15]
        print(f"✓ Fetched {len(calls)} CALL contracts from NSE API")
        return calls

    except Exception as e:
        print(f"⚠ NSE API error: {e}, using CSV fallback...")
        return None


def save_json(data, path):
    """Save data to JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    print(f"✓ Saved to {path.name}")


def fetch_all_most_active():
    """Fetch all most active data (puts + calls) automatically."""
    print("\n" + "="*60)
    print("🚀 AUTO-FETCHING MOST ACTIVE DATA FROM NSE")
    print("="*60 + "\n")

    # Try NSE API first
    puts = fetch_nse_most_active_puts()
    calls = fetch_nse_most_active_calls()

    # If API fails, use CSV fallback
    if puts is None or calls is None:
        print("\n⚠ NSE API unavailable, using CSV fallback...\n")
        puts_csv, calls_csv = fetch_from_csv_fallback()
        puts = puts_csv if puts is None else puts
        calls = calls_csv if calls is None else calls

    # Save PUTS
    if puts:
        puts_data = {
            'rows': puts,
            'total_contracts': len(puts),
            'generated_at': datetime.now().isoformat(),
            'data_scope': 'puts',
            'source': 'NSE (API or CSV fallback)',
            'symbol': 'NIFTY'
        }
        save_json(puts_data, OUTPUTS['puts'])

    # Save CALLS
    if calls:
        calls_data = {
            'rows': calls,
            'total_contracts': len(calls),
            'generated_at': datetime.now().isoformat(),
            'data_scope': 'calls',
            'source': 'NSE (API or CSV fallback)',
            'symbol': 'NIFTY'
        }
        save_json(calls_data, OUTPUTS['calls'])

    # Combined data (for UI)
    if puts or calls:
        combined = {
            'rows': puts + calls,  # All data in one list for UI
            'puts': puts,
            'calls': calls,
            'total_puts': len(puts),
            'total_calls': len(calls),
            'total_contracts': len(puts) + len(calls),
            'generated_at': datetime.now().isoformat(),
            'data_scope': 'puts_and_calls',
            'source': 'NSE (API or CSV fallback)',
            'symbol': 'NIFTY'
        }
        save_json(combined, OUTPUTS['combined'])

        print("\n" + "="*60)
        print("📊 MOST ACTIVE DATA SUMMARY")
        print("="*60)

        if puts:
            print(f"\n📈 TOP PUTS (by volume):")
            for i, put in enumerate(puts[:5], 1):
                print(f"  {i}. Strike {put['strikeOrExpiry']:8.0f} | LTP: {put['ltp']:8.2f} | Vol: {put['volume']:>10}")

        if calls:
            print(f"\n📉 TOP CALLS (by volume):")
            for i, call in enumerate(calls[:5], 1):
                print(f"  {i}. Strike {call['strikeOrExpiry']:8.0f} | LTP: {call['ltp']:8.2f} | Vol: {call['volume']:>10}")

        print(f"\n✅ Successfully fetched and saved!")
        print(f"   PUTS: {len(puts)} contracts")
        print(f"   CALLS: {len(calls)} contracts")
        print(f"   TOTAL: {len(puts) + len(calls)} contracts")
        return True

    print("\n✗ Failed to fetch data from both NSE API and CSV fallback")
    return False


if __name__ == '__main__':
    success = fetch_all_most_active()
    exit(0 if success else 1)

