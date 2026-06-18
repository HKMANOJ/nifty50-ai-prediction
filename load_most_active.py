#!/usr/bin/env python3
"""Load Most Active Puts/Index/Volume data from CSV and integrate into predictions."""

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

# CSV file locations
DOWNLOADS_PATH = Path.home() / "Downloads"
WORKSPACE_INPUTS = Path(__file__).resolve().parent / "inputs"

# Look for the most active CSV in multiple locations
POSSIBLE_LOCATIONS = [
    DOWNLOADS_PATH / "ED-Most-Active-puts-index-volume-18-Jun-2026.csv",
    WORKSPACE_INPUTS / "ED-Most-Active-puts-index-volume-18-Jun-2026.csv",
    Path(__file__).resolve().parent / "ED-Most-Active-puts-index-volume-18-Jun-2026.csv",
]


def find_most_active_csv() -> Path | None:
    """Find the Most Active CSV file in known locations."""
    for path in POSSIBLE_LOCATIONS:
        if path.exists():
            print(f"✓ Found Most Active CSV: {path}")
            return path
    print("✗ Most Active CSV not found in any location")
    return None


def parse_most_active_csv(csv_path: Path) -> list[dict[str, Any]]:
    """Parse the Most Active CSV file into structured data.

    Expected CSV format (NSE):
    INSTRUMENT TYPE, SYMBOL, EXPIRY DATE, OPTION TYPE, STRIKE PRICE, LTP, %CHNG, VOLUME, VALUE, OI, UNDERLYING
    """
    rows = []
    try:
        with open(csv_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for idx, row in enumerate(reader):
                if not row or all(v is None or v.strip() == '' for v in row.values()):
                    continue

                # Handle messy column names with trailing spaces
                normalized = {}
                for key, value in row.items():
                    if key is None or value is None:
                        continue

                    key_clean = key.strip().lower() if key else ''
                    value_clean = value.strip() if isinstance(value, str) else str(value)

                    if not value_clean:
                        continue

                    # Map columns
                    if 'symbol' in key_clean:
                        normalized['symbol'] = value_clean
                    elif 'option type' in key_clean or 'opttype' in key_clean:
                        normalized['type'] = value_clean.upper()
                    elif 'strike' in key_clean:
                        try:
                            normalized['strikeOrExpiry'] = float(value_clean.replace(',', ''))
                        except (ValueError, AttributeError):
                            normalized['strikeOrExpiry'] = value_clean
                    elif 'expiry' in key_clean:
                        if 'strikeOrExpiry' not in normalized:
                            normalized['strikeOrExpiry'] = value_clean
                    elif 'ltp' in key_clean or (key_clean and 'ltp' in key_clean):
                        try:
                            normalized['ltp'] = float(value_clean.replace(',', ''))
                        except (ValueError, AttributeError):
                            normalized['ltp'] = None
                    elif 'volume' in key_clean:
                        try:
                            normalized['volume'] = int(value_clean.replace(',', ''))
                        except (ValueError, AttributeError):
                            normalized['volume'] = None
                    elif 'open interest' in key_clean or 'oi' in key_clean:
                        try:
                            normalized['oi'] = int(value_clean.replace(',', ''))
                        except (ValueError, AttributeError):
                            normalized['oi'] = None
                    elif 'underlying' in key_clean:
                        try:
                            normalized['underlying'] = float(value_clean.replace(',', ''))
                        except (ValueError, AttributeError):
                            normalized['underlying'] = None

                if normalized.get('symbol'):
                    rows.append(normalized)

        print(f"✓ Parsed {len(rows)} most active contracts from CSV")
        return rows

    except Exception as e:
        print(f"✗ Error parsing CSV: {e}")
        import traceback
        traceback.print_exc()
        return []


def get_top_most_active(rows: list[dict[str, Any]], limit: int = 15) -> list[dict[str, Any]]:
    """Get top N most active contracts by volume."""
    # Sort by volume descending
    sorted_rows = sorted(rows, key=lambda x: x.get('volume', 0) or 0, reverse=True)
    return sorted_rows[:limit]


def format_for_ui(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Format data for UI consumption."""
    return {
        'rows': rows,
        'total_contracts': len(rows),
        'generated_at': datetime.now().isoformat(),
        'data_scope': 'puts_and_index',
        'note': 'Top most active contracts from NSE (Puts, Index, Volume tracked)'
    }


def load_and_serve_most_active() -> dict[str, Any]:
    """Main function: load CSV and return formatted data."""
    csv_path = find_most_active_csv()

    if not csv_path:
        print("⚠ Most Active CSV not found")
        return {
            'rows': [],
            'error': 'CSV file not found in Downloads or workspace',
            'generated_at': datetime.now().isoformat()
        }

    # Parse CSV
    rows = parse_most_active_csv(csv_path)

    if not rows:
        print("⚠ No data parsed from CSV")
        return {
            'rows': [],
            'error': 'CSV file is empty or has parsing errors',
            'generated_at': datetime.now().isoformat()
        }

    # Get top 15 by volume
    top_rows = get_top_most_active(rows, limit=15)

    # Format for UI
    result = format_for_ui(top_rows)

    print(f"✓ Successfully loaded and formatted {len(top_rows)} most active contracts")
    return result


def save_most_active_json(data: dict[str, Any], output_path: Path | None = None) -> Path | None:
    """Save parsed data as JSON for quick loading."""
    if output_path is None:
        output_path = Path(__file__).resolve().parent / "inputs" / "most_active.latest.json"

    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        print(f"✓ Saved to {output_path}")
        return output_path
    except Exception as e:
        print(f"✗ Error saving JSON: {e}")
        return None


if __name__ == '__main__':
    # Load data
    data = load_and_serve_most_active()

    # Display sample
    if data.get('rows'):
        print("\n📊 Top Most Active Contracts:")
        print("-" * 80)
        for i, row in enumerate(data['rows'][:5], 1):
            print(f"{i}. {row['symbol']:12} {row['type']:6} {row['strikeOrExpiry']:8} | "
                  f"LTP: {row.get('ltp', 'N/A'):8} | "
                  f"Vol: {row.get('volume', 'N/A'):>10} | "
                  f"OI: {row.get('oi', 'N/A'):>10}")

    # Save to JSON
    save_most_active_json(data)

    print("\n✅ Ready for UI integration!")

