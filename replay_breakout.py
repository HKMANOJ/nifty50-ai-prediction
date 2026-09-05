#!/usr/bin/env python3
"""
replay_breakout.py — measure the breakout engine instead of guessing at it.

Point it at any candle source, get win rate, expectancy, profit factor,
bootstrap confidence intervals and a chronological out-of-sample split.
A configuration is only worth shipping if the CI excludes zero AND both
halves point the same way.

    # against the NIFTY 60-day file already in the repo
    python3 replay_breakout.py --source inputs/nifty50_intraday_60d.json

    # against your own signal log once it has a few weeks in it
    python3 replay_breakout.py --source signals/candles.jsonl --ablate

Expected input: either
  A) {"series": {"5m": [{"market_date","market_time","open","high","low","close","volume"}, ...]}}
  B) JSON lines of {"symbol","market_date","market_time","open","high","low","close","volume"}
"""

from __future__ import annotations

import argparse
import collections
import json
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from breakout_engine import BreakoutEngine, BreakoutConfig  # noqa: E402

random.seed(7)


# ── loading ─────────────────────────────────────────────────────────────────
def load(path: str) -> dict[tuple[str, str], list[dict]]:
    """-> {(symbol, date): [bars]}"""
    p = Path(path)
    out: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    if p.suffix == ".jsonl":
        rows = (json.loads(l) for l in p.open() if l.strip())
    else:
        d = json.loads(p.read_text())
        sym = d.get("symbol", "INDEX")
        rows = ({**b, "symbol": sym} for b in d["series"]["5m"])
    for b in rows:
        key = (b.get("symbol", "INDEX"), b["market_date"])
        out[key].append({
            "t": b["market_time"][11:16], "open": b["open"], "high": b["high"],
            "low": b["low"], "close": b["close"], "volume": b.get("volume") or 0.0,
        })
    return {k: v for k, v in out.items() if len(v) >= 60}


# ── outcome scoring ─────────────────────────────────────────────────────────
def score(bars, i, entry, stop, side, rr):
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    tgt = entry + rr * risk if side == "bullish" else entry - rr * risk
    for c in bars[i + 1:]:
        if side == "bullish":
            if c["low"] <= stop:
                return -1.0
            if c["high"] >= tgt:
                return rr
        else:
            if c["high"] >= stop:
                return -1.0
            if c["low"] <= tgt:
                return rr
    last = bars[-1]["close"]
    return (last - entry) / risk if side == "bullish" else (entry - last) / risk


# ── replay ──────────────────────────────────────────────────────────────────
def replay(sessions, cfg, *, require_retest=False, before=None, sides=("bullish", "bearish")):
    keys = sorted(sessions, key=lambda k: (k[0], k[1]))
    prior_by_symbol: dict[str, list[list[dict]]] = collections.defaultdict(list)
    rows = []
    for sym, date in keys:
        bars = sessions[(sym, date)]
        prev = prior_by_symbol[sym]
        pdh = max(x["high"] for x in prev[-1]) if prev else None
        pdl = min(x["low"] for x in prev[-1]) if prev else None
        for side in sides:
            eng = BreakoutEngine(side, config=cfg, prior_sessions=prev,
                                 prior_day_high=pdh, prior_day_low=pdl, symbol=sym)
            pend = None
            for b in bars:
                ev = eng.on_bar(b)
                if not ev:
                    continue
                use = None
                if ev["kind"] == "BREAKOUT":
                    pend = ev
                    if not require_retest:
                        use = ev
                elif ev["kind"] == "RETEST_CONFIRMED" and require_retest and pend:
                    use = {**pend, "bar_index": ev["bar_index"], "t": ev["t"],
                           "entry": ev["close"]}
                    pend = None
                if not use:
                    continue
                if before and _m(use["t"]) > _m(before):
                    continue
                r = score(bars, use["bar_index"], use["entry"], use["stop"], side, cfg.target_r)
                if r is not None:
                    rows.append({"symbol": sym, "date": date, "side": side,
                                 "t": use["t"], "level_kind": use["level_kind"],
                                 "rvol": use.get("rvol"), "r": r})
        prior_by_symbol[sym].append(bars)
    return rows


def _m(t):
    h, m = t.split(":")
    return int(h) * 60 + int(m)


# ── statistics ──────────────────────────────────────────────────────────────
def boot_ci(rs, n=4000):
    if len(rs) < 5:
        return None, None
    ms = sorted(statistics.fmean(random.choices(rs, k=len(rs))) for _ in range(n))
    return ms[int(n * .025)], ms[int(n * .975)]


def summarise(rows, label, split_key=None):
    if not rows:
        print(f"  {label:<34} —")
        return
    rs = [r["r"] for r in rows]
    w = sum(1 for x in rs if x > 0)
    l = sum(1 for x in rs if x < 0)
    gp = sum(x for x in rs if x > 0)
    gl = -sum(x for x in rs if x < 0)
    pf = gp / gl if gl else float("inf")
    lo, hi = boot_ci(rs)
    ci = f"[{lo:+.3f},{hi:+.3f}]" if lo is not None else "—"
    ship = "SHIP" if (lo is not None and lo > 0) else "hold"
    line = (f"  {label:<34} n={len(rs):<5} win={100*w/(w+l) if w+l else 0:5.1f}%  "
            f"exp={statistics.fmean(rs):+.3f}R  pf={pf:5.2f}  CI{ci}  {ship}")
    print(line)
    if split_key:
        a = [r["r"] for r in rows if r["date"] < split_key]
        b = [r["r"] for r in rows if r["date"] >= split_key]
        if a and b:
            print(f"  {'':<34} in-sample {statistics.fmean(a):+.3f}R (n={len(a)})"
                  f"   out-of-sample {statistics.fmean(b):+.3f}R (n={len(b)})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--rvol-min", type=float, default=0.0,
                    help="0 disables the volume gate (needed for index data)")
    ap.add_argument("--target-r", type=float, default=1.7)
    ap.add_argument("--ablate", action="store_true", help="run the filter sweep")
    a = ap.parse_args()

    sessions = load(a.source)
    dates = sorted({d for _, d in sessions})
    split = dates[len(dates) // 2]
    print(f"sessions={len(sessions)}  symbols={len({s for s,_ in sessions})}  "
          f"dates {dates[0]} → {dates[-1]}  split at {split}\n")

    base = BreakoutConfig(rvol_min=a.rvol_min, target_r=a.target_r)
    summarise(replay(sessions, base), "baseline", split)

    if not a.ablate:
        return
    print()
    for lv in ("ORB", "BOX", "DAY", "PDH"):
        cfg = BreakoutConfig(rvol_min=a.rvol_min, target_r=a.target_r,
                             enabled_levels=(lv,))
        summarise(replay(sessions, cfg), f"level = {lv}", split)
    print()
    summarise(replay(sessions, base, require_retest=True), "retest-confirmed only", split)
    summarise(replay(sessions, base, before="11:30"), "morning only (<=11:30)", split)
    summarise(replay(sessions, base, require_retest=True, before="11:30"),
              "retest + morning", split)
    print()
    for rv in (1.2, 1.5, 2.0):
        cfg = BreakoutConfig(rvol_min=rv, target_r=a.target_r)
        summarise(replay(sessions, cfg), f"rvol >= {rv}", split)
    print()
    print("  SHIP = bootstrap 95% CI excludes zero. Everything else is a")
    print("  hypothesis that needs more sessions, not a filter to turn on.")


if __name__ == "__main__":
    main()
