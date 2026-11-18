#!/usr/bin/env python3
"""Downloads and caches Upstox NSE & BSE instrument keys dynamically into inputs/upstox_instruments.json."""

import gzip
import json
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
    for ex_name, url in EXCHANGES:
        print(f"Downloading {ex_name} instruments from {url}...")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                compressed = resp.read()
            decompressed = gzip.decompress(compressed)
            instruments = json.loads(decompressed.decode("utf-8"))
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
        except Exception as exc:
            print(f"Warning: Failed downloading {ex_name} instruments: {exc}")

    # Aliases dynamically resolved to their parent index
    if "NIFTY" in mapping:
        mapping["NIFTY 50"] = mapping["NIFTY"]
        mapping["NIFTY50"] = mapping["NIFTY"]
    if "BSE SENSEX" in mapping:
        mapping["SENSEX"] = mapping["BSE SENSEX"]
        mapping["BSESENSEX"] = mapping["BSE SENSEX"]

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
    print(f"Successfully saved {len(mapping)} dynamic instruments to {OUTPUT_FILE}")
    return mapping


if __name__ == "__main__":
    download_and_index()
