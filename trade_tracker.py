from typing import Any, Dict, List, Optional
import json
import uuid
from datetime import datetime


class TradeTracker:
    """
    Tracks open and closed trades, storing them in a trade_history.json file.
    Each trade tracks: entry, target, stop, exit_price, pnl, result, and status.
    """

    def __init__(self, history_file: str = "trade_history.json"):
        self.history_file = history_file
        self.trade_history: List[Dict[str, Any]] = self._load_trade_history()

    def _load_trade_history(self) -> List[Dict[str, Any]]:
        """Loads trade history from the JSON file."""
        try:
            with open(self.history_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _save_trade_history(self):
        """Saves the current trade history to the JSON file."""
        with open(self.history_file, "w", encoding="utf-8") as f:
            json.dump(self.trade_history, f, indent=2)

    def record_trade(
        self,
        signal: str,
        entry: float,
        target1: float,
        target2: float,
        stoploss: float,
        pattern: str = "",
        confidence: float = 0.0,
        risk_reward: float = 0.0,
    ) -> Dict[str, Any]:
        """
        Records a new trade with 'OPEN' status.
        Returns the created trade record.
        """
        trade = {
            "trade_id": str(uuid.uuid4())[:8],
            "timestamp": datetime.now().isoformat(),
            "signal": signal,
            "entry": round(entry, 2),
            "target1": round(target1, 2),
            "target2": round(target2, 2),
            "stoploss": round(stoploss, 2),
            "pattern": pattern,
            "confidence": round(confidence, 1),
            "risk_reward": round(risk_reward, 2),
            "status": "OPEN",
            "exit_price": None,
            "exit_timestamp": None,
            "result": None,      # "TARGET1_HIT", "TARGET2_HIT", "STOPLOSS_HIT", "EXPIRED"
            "pnl_points": None,  # Points gained/lost (positive = profit)
            "pnl_rr": None,      # P&L in risk-reward units
        }
        self.trade_history.append(trade)
        self._save_trade_history()
        return trade

    def close_trade(
        self,
        trade_id: str,
        exit_price: float,
        result: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Closes an existing trade with exit price, result, and P&L calculation.

        Parameters:
          trade_id: unique trade identifier (trade_id field or timestamp for legacy)
          exit_price: price at which the trade was closed
          result: "TARGET1_HIT", "TARGET2_HIT", "STOPLOSS_HIT", "EXPIRED"

        Returns the updated trade record, or None if trade not found.
        """
        trade = self._find_trade(trade_id)
        if trade is None:
            return None

        entry = float(trade["entry"])
        stoploss = float(trade["stoploss"])
        signal = str(trade["signal"]).upper()

        # Calculate points P&L
        if "CALL" in signal:
            pnl_points = exit_price - entry
        elif "PUT" in signal:
            pnl_points = entry - exit_price
        else:
            pnl_points = 0.0

        # Calculate risk-reward P&L
        risk = abs(entry - stoploss)
        pnl_rr = (pnl_points / risk) if risk > 0 else 0.0

        trade["status"] = "CLOSED"
        trade["exit_price"] = round(exit_price, 2)
        trade["exit_timestamp"] = datetime.now().isoformat()
        trade["result"] = result
        trade["pnl_points"] = round(pnl_points, 2)
        trade["pnl_rr"] = round(pnl_rr, 2)

        self._save_trade_history()
        return trade

    def update_trade_status(self, trade_id: str, status: str) -> bool:
        """
        Updates the status of an existing trade (legacy method).
        Prefer close_trade() for closing trades with P&L calculation.
        """
        trade = self._find_trade(trade_id)
        if trade is None:
            return False
        trade["status"] = status
        self._save_trade_history()
        return True

    def _find_trade(self, trade_id: str) -> Optional[Dict[str, Any]]:
        """Find a trade by trade_id or timestamp (legacy support)."""
        for trade in self.trade_history:
            if trade.get("trade_id") == trade_id:
                return trade
            if trade.get("timestamp") == trade_id:
                return trade
        return None

    def get_open_trades(self) -> List[Dict[str, Any]]:
        """Returns a list of all currently open trades."""
        return [trade for trade in self.trade_history if trade.get("status") == "OPEN"]

    def get_closed_trades(self) -> List[Dict[str, Any]]:
        """Returns a list of all closed trades."""
        return [trade for trade in self.trade_history if trade.get("status") != "OPEN"]

    def get_all_trades(self) -> List[Dict[str, Any]]:
        """Returns the entire trade history."""
        return self.trade_history

    def get_trade_stats(self) -> Dict[str, Any]:
        """
        Returns aggregate trade statistics:
        total trades, wins, losses, win rate, profit factor,
        average winner, average loser, total P&L.
        """
        closed = self.get_closed_trades()
        if not closed:
            return {
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
                "profit_factor": 0.0,
                "avg_winner_points": 0.0,
                "avg_loser_points": 0.0,
                "total_pnl_points": 0.0,
                "total_pnl_rr": 0.0,
            }

        winners = [t for t in closed if (t.get("pnl_points") or 0) > 0]
        losers = [t for t in closed if (t.get("pnl_points") or 0) < 0]

        total_win_points = sum(float(t.get("pnl_points") or 0) for t in winners)
        total_loss_points = abs(sum(float(t.get("pnl_points") or 0) for t in losers))

        win_count = len(winners)
        loss_count = len(losers)
        total = len(closed)

        win_rate = (win_count / total * 100) if total > 0 else 0.0
        profit_factor = (total_win_points / total_loss_points) if total_loss_points > 0 else 0.0
        avg_winner = (total_win_points / win_count) if win_count > 0 else 0.0
        avg_loser = (total_loss_points / loss_count) if loss_count > 0 else 0.0
        total_pnl = total_win_points - total_loss_points
        total_rr = sum(float(t.get("pnl_rr") or 0) for t in closed)

        return {
            "total_trades": total,
            "wins": win_count,
            "losses": loss_count,
            "win_rate": round(win_rate, 2),
            "profit_factor": round(profit_factor, 2),
            "avg_winner_points": round(avg_winner, 2),
            "avg_loser_points": round(avg_loser, 2),
            "total_pnl_points": round(total_pnl, 2),
            "total_pnl_rr": round(total_rr, 2),
        }
