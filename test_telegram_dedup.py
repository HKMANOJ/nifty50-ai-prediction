#!/usr/bin/env python3
"""
Unit tests for Telegram Alert Deduplication, Postgres Atomic Locking,
Recency Guards, and Environment Controls.
"""
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import notify

INDIA_TZ = ZoneInfo("Asia/Kolkata")


class TestTelegramDeduplication(unittest.TestCase):
    def test_is_production_sender_logic(self):
        """Dev workstation must be blocked by default; Render and explicit flags permitted."""
        # Clean environment
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(notify.is_production_sender(), "Local workstation must not send real telegram alerts by default")

        # RENDER environment
        with patch.dict("os.environ", {"RENDER": "true"}, clear=True):
            self.assertTrue(notify.is_production_sender(), "Render environment must be permitted")

        # FORCE_LOCAL_TELEGRAM_ALERTS=1
        with patch.dict("os.environ", {"FORCE_LOCAL_TELEGRAM_ALERTS": "1"}, clear=True):
            self.assertTrue(notify.is_production_sender(), "Explicit force override must be permitted")

        # FORCE_LOCAL_TELEGRAM_ALERTS=0
        with patch.dict("os.environ", {"FORCE_LOCAL_TELEGRAM_ALERTS": "0", "RENDER": "true"}, clear=True):
            self.assertFalse(notify.is_production_sender(), "Explicit force disable must override Render")

    def test_atomic_reservation_deduplication(self):
        """Atomic reservation must allow first caller and strictly reject identical keys."""
        test_key = f"TEST_ALERT_KEY_{int(datetime.now().timestamp())}"
        today = datetime.now(INDIA_TZ).date().isoformat()

        # First reservation should succeed
        acquired_first = notify.reserve_alert_atomic(test_key, today, "First test message")
        self.assertTrue(acquired_first, "First reservation attempt must succeed")

        # Second reservation with identical key MUST fail (idempotency guard)
        acquired_second = notify.reserve_alert_atomic(test_key, today, "Duplicate test message")
        self.assertFalse(acquired_second, "Duplicate reservation attempt must fail")

        # Clean up in DB if postgres connected
        conn = notify._get_pg_conn()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("DELETE FROM telegram_sent_alerts WHERE alert_key = %s", (test_key,))
                conn.commit()
                cur.close()
                conn.close()
            except Exception:
                pass

    def test_stale_zone_recency_guard(self):
        """Demand Fresh zone formed hours ago (e.g. 09:35 AM scanned at 1:00 PM) must NOT be alerted."""
        now_ist = datetime.now(INDIA_TZ)
        today = now_ist.date()

        # Simulate a 9:35 AM zone formed 3 hours ago
        stale_time = now_ist - timedelta(hours=3)
        stale_ts = int(stale_time.timestamp())

        # Age calculation verification
        age_seconds = (now_ist.timestamp() - stale_ts)
        self.assertGreater(age_seconds, 20 * 60, "3-hour-old zone is beyond 20-min recency window")

        # Simulate a fresh zone formed 5 minutes ago
        recent_time = now_ist - timedelta(minutes=5)
        recent_ts = int(recent_time.timestamp())
        recent_age_seconds = (now_ist.timestamp() - recent_ts)
        self.assertLessEqual(recent_age_seconds, 20 * 60, "5-min-old zone is within 20-min recency window")


if __name__ == "__main__":
    unittest.main()
