#!/usr/bin/env python3
"""
CLI wrapper for accuracy reporting and trade recalculation.
"""

from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

# Add root directory to python path
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from accuracy_dashboard import get_accuracy_dashboard_data
from trade_accuracy_analyzer import recalculate_all_trades

def main() -> None:
    parser = argparse.ArgumentParser(description="Accuracy API command-line interface.")
    sub = parser.add_subparsers(dest="command", required=True)
    
    sub.add_parser("summary", help="Get historical accuracy summary.")
    sub.add_parser("recalculate", help="Recalculate trade accuracy outcomes.")
    
    args = parser.parse_args()
    
    if args.command == "summary":
        try:
            data = get_accuracy_dashboard_data()
            print(json.dumps(data, indent=2))
        except Exception as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            sys.exit(1)
            
    elif args.command == "recalculate":
        try:
            res = recalculate_all_trades()
            print(json.dumps(res, indent=2))
        except Exception as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            sys.exit(1)

if __name__ == "__main__":
    main()
