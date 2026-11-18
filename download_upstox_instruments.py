#!/usr/bin/env python3
"""Downloads and caches Upstox NSE instrument keys into inputs/upstox_instruments.json."""

import gzip
import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTPUT_FILE = ROOT / "inputs" / "upstox_instruments.json"
NSE_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"


def download_and_index():
    print(f"Downloading NSE instruments from {NSE_URL}...")
    req = urllib.request.Request(
        NSE_URL,
        headers={"User-Agent": "Mozilla/5.0"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        compressed = resp.read()
    
    decompressed = gzip.decompress(compressed)
    instruments = json.loads(decompressed.decode("utf-8"))
    
    mapping = {}
    for item in instruments:
        # e.g., {"trading_symbol": "RELIANCE", "instrument_key": "NSE_EQ|INE002A01018", "instrument_type": "EQ"}
        sym = item.get("trading_symbol")
        key = item.get("instrument_key")
        itype = item.get("instrument_type")
        if sym and key and itype in ("EQ", "INDEX"):
            mapping[sym.upper()] = {
                "instrument_key": key,
                "name": item.get("name"),
                "lot_size": item.get("lot_size", 1),
            }
            
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
    print(f"Successfully saved {len(mapping)} NSE instruments to {OUTPUT_FILE}")
    return mapping


if __name__ == "__main__":
    download_and_index()
