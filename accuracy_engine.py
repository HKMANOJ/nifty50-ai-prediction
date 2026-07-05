from typing import Dict, Any, List
import json
from datetime import datetime, timedelta


class AccuracyEngine:
    """
    Calculates and tracks the accuracy of trading signals over various periods.
    Stores daily statistics in a daily_stats.json file.

    Tracked per-day:
      - signals: total signals generated
      - wins: signals that hit target1 or target2
      - losses: signals that hit stop-loss
      - total_rr: cumulative risk-reward units won/lost
      - total_winner_points: cumulative points gained on winners
      - total_loser_points: cumulative points lost on losers
    """

    def __init__(self, stats_file: str = "daily_stats.json"):
        self.stats_file = stats_file
        self.daily_stats: Dict[str, Dict[str, Any]] = self._load_daily_stats()

    def _load_daily_stats(self) -> Dict[str, Dict[str, Any]]:
        """Loads daily statistics from the JSON file."""
        try:
            with open(self.stats_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_daily_stats(self):
        """Saves the current daily statistics to the JSON file."""
        with open(self.stats_file, "w", encoding="utf-8") as f:
            json.dump(self.daily_stats, f, indent=2)

    def _ensure_day(self, trade_date: str) -> None:
        """Ensure the daily stats entry exists with all required fields."""
        if trade_date not in self.daily_stats:
            self.daily_stats[trade_date] = {
                "signals": 0,
                "wins": 0,
                "losses": 0,
                "total_rr": 0.0,
                "total_winner_points": 0.0,
                "total_loser_points": 0.0,
            }
        # Back-fill missing keys for older records
        day = self.daily_stats[trade_date]
        day.setdefault("total_winner_points", 0.0)
        day.setdefault("total_loser_points", 0.0)

    def record_trade_result(
        self,
        trade_timestamp: str,
        signal: str,
        status: str,
        rr: float = 0.0,
        points_gained: float = 0.0,
    ):
        """
        Records the result of a trade for accuracy calculation.

        Parameters:
          trade_timestamp: ISO timestamp of the trade
          signal: CALL / PUT / STRONG_CALL / STRONG_PUT / etc.
          status: TARGET1_HIT, TARGET2_HIT, STOPLOSS_HIT, EXPIRED
          rr: risk-reward ratio achieved (positive for winners)
          points_gained: absolute points gained (positive) or lost (positive value representing loss)
        """
        trade_date = datetime.fromisoformat(trade_timestamp).date().isoformat()
        self._ensure_day(trade_date)

        day = self.daily_stats[trade_date]
        day["signals"] += 1

        if status in ("TARGET1_HIT", "TARGET2_HIT"):
            day["wins"] += 1
            day["total_rr"] += abs(rr)
            day["total_winner_points"] += abs(points_gained)
        elif status == "STOPLOSS_HIT":
            day["losses"] += 1
            day["total_rr"] -= 1.0  # Standard 1R loss
            day["total_loser_points"] += abs(points_gained)

        self._save_daily_stats()

    # ---- Period-based statistics ----

    def _calculate_period_stats(self, days: int) -> Dict[str, Any]:
        """
        Calculates aggregated statistics for a given number of past days.
        """
        total_signals = 0
        total_wins = 0
        total_losses = 0
        total_rr = 0.0
        total_winner_points = 0.0
        total_loser_points = 0.0

        today = datetime.now().date()
        for i in range(days):
            current_date = (today - timedelta(days=i)).isoformat()
            if current_date in self.daily_stats:
                stats = self.daily_stats[current_date]
                total_signals += stats["signals"]
                total_wins += stats["wins"]
                total_losses += stats["losses"]
                total_rr += stats["total_rr"]
                total_winner_points += stats.get("total_winner_points", 0.0)
                total_loser_points += stats.get("total_loser_points", 0.0)

        win_rate = (total_wins / total_signals) * 100 if total_signals > 0 else 0.0
        avg_rr = total_rr / total_wins if total_wins > 0 else 0.0
        avg_winner = total_winner_points / total_wins if total_wins > 0 else 0.0
        avg_loser = total_loser_points / total_losses if total_losses > 0 else 0.0
        profit_factor = (total_winner_points / total_loser_points) if total_loser_points > 0 else 0.0

        return {
            "signals": total_signals,
            "wins": total_wins,
            "losses": total_losses,
            "win_rate": round(win_rate, 2),
            "avg_rr": round(avg_rr, 2),
            "avg_winner_points": round(avg_winner, 2),
            "avg_loser_points": round(avg_loser, 2),
            "profit_factor": round(profit_factor, 2),
        }

    def get_today_accuracy(self) -> Dict[str, Any]:
        """Returns accuracy statistics for today."""
        return self._calculate_period_stats(1)

    def get_last_7_days_accuracy(self) -> Dict[str, Any]:
        """Returns accuracy statistics for the last 7 days."""
        return self._calculate_period_stats(7)

    def get_last_30_days_accuracy(self) -> Dict[str, Any]:
        """Returns accuracy statistics for the last 30 days."""
        return self._calculate_period_stats(30)

    def get_overall_accuracy(self) -> Dict[str, Any]:
        """Returns accuracy statistics for all recorded data."""
        total_signals = sum(stats["signals"] for stats in self.daily_stats.values())
        total_wins = sum(stats["wins"] for stats in self.daily_stats.values())
        total_losses = sum(stats["losses"] for stats in self.daily_stats.values())
        total_rr = sum(stats["total_rr"] for stats in self.daily_stats.values())
        total_winner_points = sum(stats.get("total_winner_points", 0.0) for stats in self.daily_stats.values())
        total_loser_points = sum(stats.get("total_loser_points", 0.0) for stats in self.daily_stats.values())

        win_rate = (total_wins / total_signals) * 100 if total_signals > 0 else 0.0
        avg_rr = total_rr / total_wins if total_wins > 0 else 0.0
        avg_winner = total_winner_points / total_wins if total_wins > 0 else 0.0
        avg_loser = total_loser_points / total_losses if total_losses > 0 else 0.0
        profit_factor = (total_winner_points / total_loser_points) if total_loser_points > 0 else 0.0

        return {
            "signals": total_signals,
            "wins": total_wins,
            "losses": total_losses,
            "win_rate": round(win_rate, 2),
            "avg_rr": round(avg_rr, 2),
            "avg_winner_points": round(avg_winner, 2),
            "avg_loser_points": round(avg_loser, 2),
            "profit_factor": round(profit_factor, 2),
        }
