#!/usr/bin/env python3
"""Downloads and caches Upstox NSE & BSE instrument keys dynamically into inputs/upstox_instruments.json."""

import gzip
import json
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTPUT_FILE = ROOT / "inputs" / "upstox_instruments.json"
EXCHANGES = [
    ("NSE", "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"),
    ("BSE", "https://assets.upstox.com/market-quote/instruments/exchange/BSE.json.gz"),
]


def download_and_index():
    mapping = {}
    futures_best = {}  # base_name -> (ex_priority, expiry_ms, entry)
    fut_cutoff_ms = int((time.time() - 86400) * 1000)

    for ex_name, url in EXCHANGES:
        print(f"Downloading {ex_name} instruments from {url}...")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                compressed = resp.read()
            decompressed = gzip.decompress(compressed)
            instruments = json.loads(decompressed.decode("utf-8"))
            ex_priority = 0 if ex_name == "NSE" else 1
            for item in instruments:
                sym = item.get("trading_symbol")
                key = item.get("instrument_key")
                name = item.get("name")
                itype = item.get("instrument_type")
                if not sym or not key:
                    continue
                if itype in ("EQ", "INDEX"):
                    entry = {
                        "instrument_key": key,
                        "name": name,
                        "lot_size": item.get("lot_size", 1),
                        "instrument_type": itype,
                        "exchange": ex_name,
                    }
                    mapping[sym.upper()] = entry
                    if name:
                        mapping[name.upper()] = entry
                elif itype == "FUT":
                    expiry_ms = int(item.get("expiry") or 0)
                    base = (item.get("underlying_symbol") or name or "").upper().strip()
                    if not base or expiry_ms < fut_cutoff_ms:
                        continue
                    fut_entry = {
                        "instrument_key": key,
                        "name": name or base,
                        "trading_symbol": sym,
                        "lot_size": item.get("lot_size", 1),
                        "instrument_type": itype,
                        "exchange": ex_name,
                        "expiry": expiry_ms,
                    }
                    prev = futures_best.get(base)
                    if prev is None or (ex_priority, expiry_ms) < (prev[0], prev[1]):
                        futures_best[base] = (ex_priority, expiry_ms, fut_entry)
        except Exception as exc:
            print(f"Warning: Failed downloading {ex_name} instruments: {exc}")

    for base, (_, _, fut_entry) in futures_best.items():
        mapping[f"{base}_FUT"] = fut_entry

    # Aliases dynamically resolved to their parent index
    if "NIFTY" in mapping:
        mapping["NIFTY 50"] = mapping["NIFTY"]
        mapping["NIFTY50"] = mapping["NIFTY"]
    if "NIFTY_FUT" in mapping:
        mapping["NIFTY 50_FUT"] = mapping["NIFTY_FUT"]
        mapping["NIFTY50_FUT"] = mapping["NIFTY_FUT"]
    if "BSE SENSEX" in mapping:
        mapping["SENSEX"] = mapping["BSE SENSEX"]
        mapping["BSESENSEX"] = mapping["BSE SENSEX"]
    if "SENSEX_FUT" in mapping:
        mapping["BSE SENSEX_FUT"] = mapping["SENSEX_FUT"]
        mapping["BSESENSEX_FUT"] = mapping["SENSEX_FUT"]
    if "BANKNIFTY_FUT" in mapping:
        mapping["NIFTY BANK_FUT"] = mapping["BANKNIFTY_FUT"]

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
    print(f"Successfully saved {len(mapping)} dynamic instruments to {OUTPUT_FILE}")
    return mapping


if __name__ == "__main__":
    download_and_index()
