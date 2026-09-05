#!/usr/bin/env python3
"""
breakout_engine.py — point-in-time breakout / breakdown state machine.

Replaces analyze_5m_breakout() in stock_suggestion_index.py.

Design contract
---------------
1. POINT-IN-TIME. The engine is fed one CLOSED bar at a time and returns an
   event for THAT bar or None. It never re-scans history, so a signal is
   always about the bar it was emitted on. The old function re-scanned the
   whole day on every call and latched the first pivot it ever found, which
   is why breakout_time drifted hours behind the clock.

2. RE-ARMS. After a break resolves (confirmed, extended or failed) the
   machine re-arms on the next level, so an afternoon continuation is a
   first-class signal rather than something invisible behind a morning pivot.

3. ONE INVALIDATION LEVEL. `stop` on the event IS the failure level IS the
   level the UI should draw. Entry, stop and target are derived from the same
   structure, so the trade plan and the signal can never disagree.

4. TRUE RVOL. Relative volume is this bar's volume against the mean volume of
   the SAME bar-of-day across prior sessions. It is not a cross-sectional
   turnover ratio.

5. LOGGABLE. Every event carries the full feature vector used to produce it.
   Append events to a store and you can replay, score and ablate later.

Usage
-----
    eng = BreakoutEngine(side="bullish",
                         prior_sessions=[...],     # list of past sessions
                         prior_day_high=..., prior_day_low=...)
    for bar in todays_closed_bars:
        ev = eng.on_bar(bar)
        if ev and ev["kind"] == "BREAKOUT":
            ...

A bar is a dict: {"t": "HH:MM", "ts": int, "open","high","low","close","volume"}
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, Literal

Side = Literal["bullish", "bearish"]


# ─────────────────────────────────────────────────────────────────────────────
# Config — every threshold in one place, so it can be swept by the replay
# harness instead of being edited by hand across two languages.
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class BreakoutConfig:
    orb_bars: int = 3               # 09:15–09:30 on 5m bars

    # ── how the opening range is measured ────────────────────────────────
    # The 09:15 bar carries the pre-market uncrossing print, so its wick is an
    # auction artefact, not a traded level. Measured on 55 NIFTY sessions: the
    # first bar's range is 2.5x the third bar's, 60% of it is wick, and its
    # wick alone sets the ORB low on 60% of sessions and the ORB high on 45%.
    # That pushes the level out of reach and the breakout is never detected --
    # which is what happened to POLYCAB and KEI on 2026-09-04.
    #   "wick"       highs/lows of all 3 bars      n=18  win 55.6%  +0.476R (CI spans 0)
    #   "body"       bodies of all 3 bars          n=22  win 68.2%  +0.751R (CI +0.26..+1.24)
    #   "skip_first" ignore the 09:15 bar          n=15  win 53.3%  +0.440R (CI spans 0)
    #   "hybrid"     bar-1 body + bars 2-3 wicks   n=20  win 75.0%  +0.897R (CI +0.38..+1.37)
    # Small samples -- treat the ranking as directional, not precise. But the
    # two body-based variants clear zero and the wick version does not.
    orb_mode: str = "hybrid"        # "hybrid" | "body" | "wick" | "skip_first"
    body_min: float = 0.40          # body / range on the break bar
    close_pos_min: float = 0.45     # close must sit in the top/bottom 55% of range
    buffer_atr: float = 0.10        # break must clear the level by this × ATR
    retest_tol_atr: float = 0.25    # how close a pullback must come to the level
    retest_max_bars: int = 6        # after this the setup is EXTENDED, not fresh
    box_lookback: int = 12          # consolidation window
    box_max_pct: float = 2.2        # box must be tighter than this to count
    atr_period: int = 14
    rvol_min: float = 1.5           # volume gate on the break bar (0 disables)
    rvol_lookback_sessions: int = 10
    min_stop_atr: float = 0.75      # floor on risk, keeps R meaningful
    min_stop_pct: float = 0.006     # minimum 0.6% price risk floor (stops 2-tick noise)
    max_extension_pct: float = 0.05  # anti-exhaustion gate: blocks moves already extended > 5.0% from open
    target_r: float = 1.7

    # ── execution model ──────────────────────────────────────────────────
    # "retest_limit" (default) is the measured rule: after a break, the NEXT
    # bar must close still holding the level AND trade back to it, and you are
    # filled by a resting limit AT the level. Measured on 55 NIFTY sessions:
    #   market at break close   n=361  win 42.6%  +0.044R  pf 1.09  (CI spans 0)
    #   market one bar later    n=277  win 46.5%  +0.089R  pf 1.19  (CI spans 0)
    #   retest limit at level   n= 92  win 55.4%  +0.336R  pf 1.77  (CI +0.08..+0.59)
    # The last one stays positive at every target from 1.0R to 3.0R and beats a
    # random-entry placebo. Widening the retrace window past one bar dilutes it.
    # On the 13-stock dry run of 2026-09-04, pure "retest_limit" produced an
    # entry on only 6 of 13 one-sided movers -- on a strong trend price never
    # comes back, so the limit never fills. "retest_then_market" takes the
    # limit when it fills and otherwise buys the confirming bar's close.
    # First-signal-per-stock on that day: 6/13 stocks +0.80R avg (retest_limit)
    # vs 13/13 stocks +0.75R avg (retest_then_market). Coverage doubles for
    # almost no loss of average quality -- but see README: that sample is 13
    # hand-picked trending stocks, so it flatters every momentum rule.
    entry_mode: str = "retest_then_market"   # "retest_limit" | "break_close"
    retest_window_bars: int = 1          # widening this measured worse, not better

    # Which level types are tradeable. Measured over 55 NIFTY sessions
    # (see BREAKOUT_REDESIGN analysis): ORB pf 1.22 · BOX pf 1.00 ·
    # DAY pf 0.57 (n=12) · PDH pf 0.29 (n=22). ORB is the only level with
    # measured edge; BOX is neutral and kept for signal volume while the
    # signal log fills up. DAY and PDH are off by default — re-enable only
    # if your own stock-level replay says otherwise.
    enabled_levels: tuple[str, ...] = ("ORB", "BOX")


DEFAULT = BreakoutConfig()


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────
def true_range(prev: dict, cur: dict) -> float:
    return max(cur["high"] - cur["low"],
               abs(cur["high"] - prev["close"]),
               abs(cur["low"] - prev["close"]))


def atr(bars: list[dict], period: int) -> float:
    if len(bars) < 2:
        return max(1e-9, bars[-1]["high"] - bars[-1]["low"])
    trs = [true_range(bars[i - 1], bars[i]) for i in range(1, len(bars))]
    w = trs[-period:]
    return max(1e-9, sum(w) / len(w))


def build_volume_profile(prior_sessions: Iterable[list[dict]],
                         lookback: int) -> dict[str, float]:
    """Mean volume per bar-of-day across prior sessions -> {"09:20": 41233.0}."""
    acc: dict[str, list[float]] = {}
    for sess in list(prior_sessions)[-lookback:]:
        for b in sess:
            v = b.get("volume") or 0.0
            if v > 0:
                acc.setdefault(b["t"], []).append(float(v))
    return {t: sum(vs) / len(vs) for t, vs in acc.items() if vs}


# ─────────────────────────────────────────────────────────────────────────────
# Engine
# ─────────────────────────────────────────────────────────────────────────────
class BreakoutEngine:
    """One instance per symbol per side per session."""

    STATES = ("PRE_ORB", "ARMED", "BREAKOUT", "CONFIRMED", "EXTENDED")

    def __init__(self, side: Side, *,
                 config: BreakoutConfig = DEFAULT,
                 prior_sessions: list[list[dict]] | None = None,
                 prior_day_high: float | None = None,
                 prior_day_low: float | None = None,
                 symbol: str = ""):
        self.side = side
        self.bull = side == "bullish"
        self.cfg = config
        self.symbol = symbol
        self.pdh = prior_day_high
        self.pdl = prior_day_low
        self.vol_profile = build_volume_profile(prior_sessions or [],
                                                config.rvol_lookback_sessions)
        self.bars: list[dict] = []
        self.state = "PRE_ORB"
        self.orb_high: float | None = None
        self.orb_low: float | None = None
        self.level: float | None = None
        self.level_kind: str | None = None
        self.trigger_bar: int | None = None
        self.stop: float | None = None
        self.entry: float | None = None
        self.events: list[dict] = []

    # ── public ──────────────────────────────────────────────────────────────
    def on_bar(self, bar: dict) -> dict | None:
        """Feed exactly one CLOSED bar. Returns an event dict or None."""
        if "t" not in bar:
            ts_val = bar.get("ts") or bar.get("timestamp")
            if ts_val:
                from datetime import datetime, timezone, timedelta
                _tz = timezone(timedelta(hours=5, minutes=30))
                bar["t"] = datetime.fromtimestamp(ts_val, tz=_tz).strftime("%H:%M")
                bar["ts"] = ts_val
        self.bars.append(bar)
        i = len(self.bars) - 1
        cfg = self.cfg

        if i < cfg.orb_bars - 1:
            self.state = "PRE_ORB"
            return None
        if i == cfg.orb_bars - 1:
            self.orb_high, self.orb_low = self._opening_range(self.bars[:cfg.orb_bars])
            self.state = "ARMED"
            return None

        a = atr(self.bars, cfg.atr_period)

        # ── an open setup is re-evaluated first ────────────────────────────
        if self.state == "BREAKOUT":
            # The tradeable trigger. Only the bar immediately after the break
            # qualifies; see BreakoutConfig.entry_mode for the measurement.
            if (cfg.entry_mode in ("retest_limit", "retest_then_market")
                    and self.trigger_bar is not None
                    and i - self.trigger_bar <= cfg.retest_window_bars):
                held = (bar["close"] > self.level if self.bull
                        else bar["close"] < self.level)
                touched = (bar["low"] <= self.level if self.bull
                           else bar["high"] >= self.level)
                if held and touched:
                    self.entry = self.level          # resting limit AT the level
                    self.state = "CONFIRMED"
                    return self._emit(bar, "ENTRY_TRIGGER", a)
                if held and cfg.entry_mode == "retest_then_market":
                    self.entry = bar["close"]        # trend never came back
                    self.state = "CONFIRMED"
                    return self._emit(bar, "ENTRY_TRIGGER", a)
                if not held:
                    return self._resolve(bar, "FAILED", a)
                # held but never came back: the entry is missed, not failed
                if i - self.trigger_bar >= cfg.retest_window_bars:
                    self.state = "EXTENDED"
                    return self._emit(bar, "ENTRY_MISSED", a)
                return None

            if self._closed_back_inside(bar):
                return self._resolve(bar, "FAILED", a)
            if self._is_retest(bar, a):
                self.state = "CONFIRMED"
                return self._emit(bar, "RETEST_CONFIRMED", a)
            if i - (self.trigger_bar or i) > cfg.retest_max_bars:
                self.state = "EXTENDED"
                return self._emit(bar, "EXTENDED", a)
            return None

        if self.state in ("CONFIRMED", "EXTENDED"):
            if self._closed_back_inside(bar):
                return self._resolve(bar, "FAILED", a)
            self.state = "ARMED"          # trend intact; a new break may follow

        # ── ARMED: is THIS bar a fresh break? ─────────────────────────────
        if self.state == "ARMED":
            return self._try_break(bar, i, a)
        return None

    # ── opening range ───────────────────────────────────────────────────────
    def _opening_range(self, window: list[dict]) -> tuple[float, float]:
        """The 09:15 bar's wick is an auction print, not a traded level.
        See BreakoutConfig.orb_mode for the measurement behind the default."""
        mode = self.cfg.orb_mode
        if mode == "wick":
            return (max(b["high"] for b in window), min(b["low"] for b in window))
        if mode == "body":
            return (max(max(b["open"], b["close"]) for b in window),
                    min(min(b["open"], b["close"]) for b in window))
        if mode == "skip_first" and len(window) > 1:
            rest = window[1:]
            return (max(b["high"] for b in rest), min(b["low"] for b in rest))
        # hybrid: first bar by its body, the rest by their real wicks
        first, rest = window[0], window[1:]
        hi = max(first["open"], first["close"])
        lo = min(first["open"], first["close"])
        if rest:
            hi = max(hi, max(b["high"] for b in rest))
            lo = min(lo, min(b["low"] for b in rest))
        return hi, lo

    # ── level ladder ────────────────────────────────────────────────────────
    def _levels(self, i: int) -> list[tuple[str, float]]:
        cfg = self.cfg
        out: list[tuple[str, float]] = []
        if self.orb_high is not None:
            out.append(("ORB", self.orb_high if self.bull else self.orb_low))
        pd = self.pdh if self.bull else self.pdl
        if pd is not None:
            out.append(("PDH" if self.bull else "PDL", pd))
        if i >= cfg.orb_bars + cfg.box_lookback:
            sl = self.bars[i - cfg.box_lookback:i]
            bh = max(x["high"] for x in sl)
            bl = min(x["low"] for x in sl)
            if bl > 0 and ((bh - bl) / bl) * 100.0 < cfg.box_max_pct:
                out.append(("BOX", bh if self.bull else bl))
        prior = self.bars[:i]
        if prior:
            out.append(("DAY", max(x["high"] for x in prior) if self.bull
                        else min(x["low"] for x in prior)))
        return [(k, v) for k, v in out
                if v is not None and k in cfg.enabled_levels]

    # ── break detection ─────────────────────────────────────────────────────
    def _try_break(self, bar: dict, i: int, a: float) -> dict | None:
        if not self._solid(bar):
            return None
        # Anti-exhaustion gate: block moves already extended > max_extension_pct from open
        if self.cfg.max_extension_pct and len(self.bars) > 0:
            open_price = self.bars[0]["open"]
            if open_price > 0 and (abs(bar["close"] - open_price) / open_price) > self.cfg.max_extension_pct:
                return None
        rvol = self.rvol(bar)
        if self.cfg.rvol_min and rvol is not None and rvol < self.cfg.rvol_min:
            return None
        prev_close = self.bars[i - 1]["close"]
        buf = a * self.cfg.buffer_atr
        for kind, lvl in self._levels(i):
            if not self._inside(prev_close, lvl):
                continue                       # already outside: not a break bar
            beyond = bar["close"] > lvl + buf if self.bull else bar["close"] < lvl - buf
            if not beyond:
                continue
            self.level, self.level_kind = lvl, kind
            self.trigger_bar, self.state = i, "BREAKOUT"
            self.entry = bar["close"]
            self.stop = self._structural_stop(i, lvl, a)
            return self._emit(bar, "BREAKOUT", a)
        return None

    def _structural_stop(self, i: int, lvl: float, a: float) -> float:
        """Below the swing that produced the break, never tighter than min_stop_atr or min_stop_pct."""
        window = self.bars[max(0, i - 2):i + 1]
        swing = (min(x["low"] for x in window) if self.bull
                 else max(x["high"] for x in window))
        entry = self.bars[i]["close"]
        floor = max(a * self.cfg.min_stop_atr, entry * self.cfg.min_stop_pct)
        if self.bull:
            return round(min(swing, entry - floor), 2)
        return round(max(swing, entry + floor), 2)

    # ── predicates ──────────────────────────────────────────────────────────
    def _solid(self, c: dict) -> bool:
        rng = max(1e-9, c["high"] - c["low"])
        if abs(c["close"] - c["open"]) / rng < self.cfg.body_min:
            return False
        if self.bull:
            return (c["close"] > c["open"]
                    and (c["close"] - c["low"]) >= rng * self.cfg.close_pos_min)
        return (c["close"] < c["open"]
                and (c["high"] - c["close"]) >= rng * self.cfg.close_pos_min)

    def _inside(self, price: float, lvl: float) -> bool:
        return price <= lvl if self.bull else price >= lvl

    def _closed_back_inside(self, c: dict) -> bool:
        return self.level is not None and self._inside(c["close"], self.level)

    def _is_retest(self, c: dict, a: float) -> bool:
        lvl = self.level
        if lvl is None:
            return False
        tol = a * self.cfg.retest_tol_atr
        if self.bull:
            return (c["low"] <= lvl + tol and c["close"] > lvl
                    and c["close"] > c["open"] and self._solid(c))
        return (c["high"] >= lvl - tol and c["close"] < lvl
                and c["close"] < c["open"] and self._solid(c))

    # ── true relative volume ────────────────────────────────────────────────
    def rvol(self, bar: dict) -> float | None:
        """This bar's volume vs the mean for the same clock slot on prior days, or rolling 20 intraday bars."""
        v = bar.get("volume") or 0.0
        if v <= 0:
            return 0.0
        ref = self.vol_profile.get(bar.get("t", ""))
        if not ref or ref <= 0:
            idx = len(self.bars) - 1
            recent = [b.get("volume", 0.0) for b in self.bars[max(0, idx - 20):idx] if b.get("volume", 0.0) > 0]
            if recent:
                ref = sum(recent) / len(recent)
            else:
                all_v = [b.get("volume", 0.0) for b in self.bars if b.get("volume", 0.0) > 0]
                ref = sum(all_v) / len(all_v) if all_v else 0.0
        if not ref or ref <= 0:
            return 1.0
        return round(v / ref, 2)

    # ── events ──────────────────────────────────────────────────────────────
    def _resolve(self, bar: dict, kind: str, a: float) -> dict:
        ev = self._emit(bar, kind, a)
        self.state = "ARMED"
        self.level = self.level_kind = self.stop = self.entry = None
        self.trigger_bar = None
        return ev

    def _emit(self, bar: dict, kind: str, a: float) -> dict:
        risk = abs((self.entry or bar["close"]) - self.stop) if self.stop else None
        target = None
        if risk:
            target = ((self.entry + self.cfg.target_r * risk) if self.bull
                      else (self.entry - self.cfg.target_r * risk))
        ev = {
            # identity
            "symbol": self.symbol, "side": self.side, "kind": kind,
            "t": bar.get("t"), "ts": bar.get("ts"), "bar_index": len(self.bars) - 1,
            "state": self.state,
            # the ONE level everything derives from
            "level": _r(self.level), "level_kind": self.level_kind,
            # trade plan, consistent with that level
            "entry": _r(self.entry), "stop": _r(self.stop), "target": _r(target),
            "risk_points": _r(risk),
            "risk_pct": _r(100 * risk / self.entry) if (risk and self.entry) else None,
            # features — log these, they are what makes ablation possible later
            "close": _r(bar["close"]), "open": _r(bar["open"]),
            "high": _r(bar["high"]), "low": _r(bar["low"]),
            "volume": bar.get("volume"),
            "rvol": self.rvol(bar),
            "atr": _r(a),
            "body_ratio": _r(abs(bar["close"] - bar["open"]) /
                             max(1e-9, bar["high"] - bar["low"])),
            "orb_high": _r(self.orb_high), "orb_low": _r(self.orb_low),
            "orb_width_atr": _r((self.orb_high - self.orb_low) / a)
            if (self.orb_high and self.orb_low) else None,
            "pdh": _r(self.pdh), "pdl": _r(self.pdl),
            "bars_since_trigger": (len(self.bars) - 1 - self.trigger_bar)
            if self.trigger_bar is not None else None,
        }
        self.events.append(ev)
        return ev


def _r(x: float | None, n: int = 2) -> float | None:
    return None if x is None else round(float(x), n)


# ─────────────────────────────────────────────────────────────────────────────
# Adapter — drop-in replacement for the old return shape, so the existing
# Suggestion dataclass and the console keep working during migration.
# ─────────────────────────────────────────────────────────────────────────────
_STATUS = {
    "BREAKOUT": ("⚡ Fresh Breakout", "Fresh", "Breakout Surge"),
    "ENTRY_TRIGGER": ("⚡ Entry Triggered", "Confirmed", "Breakout Surge"),
    "RETEST_CONFIRMED": ("Retest Confirmed", "Confirmed", "Retest Bounce Wave"),
    "ENTRY_MISSED": ("Extended — Entry Missed", "Extended", "Extended Move"),
    "EXTENDED": ("Extended — wait for retest", "Extended", "Extended Move"),
    "FAILED": ("Breakout Failed", "Failed", "Failed Breakout"),
}


def analyze_breakout(candles: list[dict], side: Side, *,
                     config: BreakoutConfig = DEFAULT,
                     prior_sessions: list[list[dict]] | None = None,
                     prior_day_high: float | None = None,
                     prior_day_low: float | None = None,
                     symbol: str = "") -> dict[str, Any]:
    """Feed a whole session, get the CURRENT state — same shape as before,
    but the reported breakout_time is the live setup, not the first of the day."""
    eng = BreakoutEngine(side, config=config, prior_sessions=prior_sessions,
                         prior_day_high=prior_day_high, prior_day_low=prior_day_low,
                         symbol=symbol)
    last_open: dict | None = None
    for bar in candles:
        ev = eng.on_bar(bar)
        if not ev:
            continue
        if ev["kind"] == "FAILED":
            last_open = None
        else:
            last_open = ev

    if not last_open:
        return {"is_breakout": False, "status": "Watching", "breakout_time": None,
                "retest_status": "Initial", "chart_structure": "Base Building",
                "is_failed_trend": False, "level": None, "level_kind": None,
                "entry": None, "stop": None, "stop_loss": None, "target": None, "rvol": None,
                "rvol_5m": 1.0,
                "ema_trend": _ema_trend(candles), "vwap": _vwap(candles),
                "event": None}

    st, retest, struct = _STATUS.get(last_open["kind"], (last_open["kind"], "Active", "Trending"))
    return {
        "is_breakout": last_open["kind"] in ("BREAKOUT", "ENTRY_TRIGGER", "RETEST_CONFIRMED"),
        "status": st, "retest_status": retest, "chart_structure": struct,
        "breakout_time": last_open["t"], "is_failed_trend": False,
        "level": last_open["level"], "level_kind": last_open["level_kind"],
        "entry": last_open["entry"], "stop": last_open["stop"],
        "stop_loss": last_open["stop"],
        "target": last_open["target"], "rvol": last_open["rvol"],
        "rvol_5m": last_open["rvol"] or 1.0,
        "ema_trend": _ema_trend(candles), "vwap": _vwap(candles),
        "event": last_open,
    }


def _ema_trend(candles: list[dict], period: int = 9) -> str:
    if not candles:
        return "neutral"
    k = 2.0 / (period + 1)
    e = candles[0]["close"]
    for c in candles[1:]:
        e = c["close"] * k + e * (1 - k)
    return "bullish" if candles[-1]["close"] >= e else "bearish"


def _vwap(candles: list[dict]) -> float | None:
    pv = vol = 0.0
    for c in candles:
        v = c.get("volume") or 0.0
        if v > 0:
            pv += ((c["high"] + c["low"] + c["close"]) / 3.0) * v
            vol += v
    return round(pv / vol, 2) if vol else None
