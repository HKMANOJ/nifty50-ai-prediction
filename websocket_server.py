#!/usr/bin/env python3
"""WebSocket Push Server for manojLabs AI Stock Hunter V2.
Streams live 5-minute candles to active TradingView charts and broadcasts 5-minute scanner updates.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
import time
from typing import Any

import websockets
from websockets.server import WebSocketServerProtocol

from stock_suggestion_index import fetch_5m_candles_for_symbol

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [WS] %(message)s")
logger = logging.getLogger("WebSocketServer")

WS_HOST = "0.0.0.0"
WS_PORT = 8765

CONNECTED_CLIENTS: set[WebSocketServerProtocol] = set()
CLIENT_WATCHLIST: dict[WebSocketServerProtocol, str] = {}
MAIN_LOOP: asyncio.AbstractEventLoop | None = None


async def register_client(ws: WebSocketServerProtocol) -> None:
    CONNECTED_CLIENTS.add(ws)
    CLIENT_WATCHLIST[ws] = "ATHERENERG"  # default symbol
    logger.info(f"Client connected: {ws.remote_address} (Total clients: {len(CONNECTED_CLIENTS)})")
    welcome = {
        "type": "connected",
        "status": "online",
        "message": "Connected to manojLabs Real-Time Push Engine",
        "default_symbol": "ATHERENERG",
        "server_time": int(time.time()),
    }
    await ws.send(json.dumps(welcome))

    # Reconnect catch-up: push unacknowledged alerts to reconnected client
    try:
        from datetime import datetime
        from stock_suggestion_index import (
            INDIA_TZ,
            INPUT_DIR,
            _load_lifecycle_state,
            _save_lifecycle_state,
        )
        state = _load_lifecycle_state()
        raw_cands = state.get("candidates", {})

        now_dt = datetime.now(INDIA_TZ)
        is_live_market = now_dt.weekday() < 5 and (9 * 60 + 15) <= (now_dt.hour * 60 + now_dt.minute) <= (15 * 60 + 35)
        now_epoch = time.time()

        # Load latest suggestions for metric enrichment
        latest_file = INPUT_DIR / "stock_suggestion_index.latest.json"
        latest_lookup: dict[str, Any] = {}
        if latest_file.exists():
            try:
                latest_data = json.loads(latest_file.read_text(encoding="utf-8"))
                for row in latest_data.get("bullish", []) + latest_data.get("bearish", []):
                    if isinstance(row, dict) and "symbol" in row:
                        latest_lookup[row["symbol"]] = row
            except Exception:
                pass

        pending = []
        needs_save = False
        for v in raw_cands.values():
            if not v.get("alert_event_id"):
                continue
            if v.get("alert_acknowledged_at"):
                continue

            created_epoch = v.get("alert_created_epoch") or v.get("claimed_at")
            is_stale = False
            if not is_live_market:
                is_stale = True
            elif created_epoch and (now_epoch - created_epoch > 900):
                is_stale = True

            if is_stale:
                v["alert_acknowledged_at"] = now_dt.strftime("%H:%M:%S")
                v["delivery_status"] = "ACKNOWLEDGED"
                needs_save = True
                continue

            # Genuine fresh unacknowledged alert during live market
            item_data = dict(v)
            if v.get("symbol") in latest_lookup:
                for k, val in latest_lookup[v["symbol"]].items():
                    if k not in item_data or item_data[k] is None:
                        item_data[k] = val
            pending.append(item_data)

        if needs_save:
            try:
                _save_lifecycle_state(state)
            except Exception:
                pass

        if pending:
            await ws.send(json.dumps({
                "type": "pending_alerts",
                "alerts": pending,
                "timestamp": int(time.time()),
            }))
    except Exception as exc:
        logger.warning(f"Failed to check pending alerts on connect: {exc}")


async def unregister_client(ws: WebSocketServerProtocol) -> None:
    CONNECTED_CLIENTS.discard(ws)
    CLIENT_WATCHLIST.pop(ws, None)
    logger.info(f"Client disconnected: {ws.remote_address} (Remaining: {len(CONNECTED_CLIENTS)})")


async def handle_message(ws: WebSocketServerProtocol, message: str) -> None:
    try:
        data = json.loads(message)
    except json.JSONDecodeError:
        return

    action = data.get("action")
    if action == "subscribe":
        symbol = str(data.get("symbol", "")).strip().upper()
        if symbol:
            CLIENT_WATCHLIST[ws] = symbol
            logger.info(f"Client {ws.remote_address} subscribed to candle stream for: {symbol}")
            await ws.send(json.dumps({
                "type": "subscribed",
                "symbol": symbol,
                "timestamp": int(time.time()),
            }))
            # Immediately push latest candle for responsive feedback
            asyncio.create_task(push_single_symbol_candle(symbol, target_ws=ws))

    elif action == "ping":
        await ws.send(json.dumps({"type": "pong", "timestamp": int(time.time())}))


async def push_single_symbol_candle(symbol: str, target_ws: WebSocketServerProtocol | None = None) -> None:
    """Fetches latest candle for symbol and pushes to target_ws (or all watching clients)."""
    try:
        loop = asyncio.get_running_loop()
        candles = await loop.run_in_executor(None, fetch_5m_candles_for_symbol, symbol, False)
        if not candles:
            return
        last = candles[-1]
        candle_payload = {
            "type": "candle_update",
            "symbol": symbol,
            "candle": {
                "time": last["timestamp"],
                "open": last["open"],
                "high": last["high"],
                "low": last["low"],
                "close": last["close"],
                "volume": last["volume"],
            },
        }
        msg = json.dumps(candle_payload)
        if target_ws and target_ws in CONNECTED_CLIENTS:
            await target_ws.send(msg)
        else:
            recipients = [ws for ws, sym in CLIENT_WATCHLIST.items() if sym == symbol and ws in CONNECTED_CLIENTS]
            if recipients:
                await asyncio.gather(*[ws.send(msg) for ws in recipients], return_exceptions=True)
    except Exception as e:
        logger.warning(f"Failed to push candle for {symbol}: {e}")


async def candle_streaming_loop() -> None:
    """Periodically streams candle updates to clients for their active stocks."""
    while True:
        try:
            if CONNECTED_CLIENTS:
                active_symbols = set(CLIENT_WATCHLIST.values())
                for symbol in active_symbols:
                    await push_single_symbol_candle(symbol)
        except Exception as e:
            logger.error(f"Error in candle streaming loop: {e}")
        await asyncio.sleep(15)


async def ws_handler(ws: WebSocketServerProtocol) -> None:
    await register_client(ws)
    try:
        async for message in ws:
            await handle_message(ws, message)
    except websockets.ConnectionClosed:
        pass
    finally:
        await unregister_client(ws)


def broadcast_scanner_update_sync(payload: dict[str, Any]) -> None:
    """Thread-safe broadcast called by 5-minute auto-scanner when a new scan finishes."""
    import scanner_trace
    import uuid
    from datetime import datetime
    from scanner_telemetry import IST
    if not MAIN_LOOP or not CONNECTED_CLIENTS:
        scanner_trace.submit_health("ws_delivery_skipped", snapshot_id=payload.get("snapshot_id"),
                                    reason="no_loop_or_connected_clients")
        return
    delivery = {"delivery_id": "delivery_"+uuid.uuid4().hex, "transport": "websocket",
                "sent_at_ist": datetime.now(IST).isoformat()}

    # Atomically claim alert events so only ONE worker transmits should_alert=True
    try:
        from stock_suggestion_index import claim_alert_delivery, mark_alert_delivered
        worker_id = delivery["delivery_id"]
        for side in ("bullish", "bearish"):
            for row in payload.get(side, []):
                event_id = row.get("alert_event_id")
                if event_id and row.get("should_alert"):
                    claimed = claim_alert_delivery(event_id, worker_id=worker_id)
                    if claimed:
                        mark_alert_delivered(event_id, worker_id=worker_id)
                    else:
                        # Another worker claimed it; suppress duplicate alert in this payload
                        row["should_alert"] = False
    except Exception as claim_err:
        logger.warning(f"Failed to claim alert delivery: {claim_err}")

    msg = json.dumps({"type": "scanner_update", "payload": {**payload, "_delivery": delivery}})

    async def _send_all():
        if CONNECTED_CLIENTS:
            clients = list(CONNECTED_CLIENTS)
            results = await asyncio.gather(*[ws.send(msg) for ws in clients], return_exceptions=True)
            scanner_trace.submit_health("ws_delivery_completed", snapshot_id=payload.get("snapshot_id"),
                **delivery, attempted_clients=len(clients), failed_clients=sum(isinstance(r, BaseException) for r in results),
                note="socket send completion, not browser display")
            logger.info(f"Broadcasted 5-min scanner update to {len(CONNECTED_CLIENTS)} clients.")

    asyncio.run_coroutine_threadsafe(_send_all(), MAIN_LOOP)


def broadcast_index_update_sync(index_context: dict[str, Any]) -> None:
    """Thread-safe broadcast of official NIFTY 50 and BANK NIFTY quotes."""
    if not MAIN_LOOP or not CONNECTED_CLIENTS:
        return
    msg = json.dumps({"type": "index_update", "index_context": index_context})

    async def _send_all():
        if CONNECTED_CLIENTS:
            await asyncio.gather(*[ws.send(msg) for ws in CONNECTED_CLIENTS], return_exceptions=True)

    asyncio.run_coroutine_threadsafe(_send_all(), MAIN_LOOP)


def start_websocket_server_thread(host: str = WS_HOST, port: int = WS_PORT) -> threading.Thread:
    """Starts the WebSocket server in a background daemon thread."""
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _main():
            global MAIN_LOOP
            # ping_interval/ping_timeout are generous so a browser busy repainting
            # the dashboard does not get its socket dropped (which used to cause a
            # reconnect storm). max_queue caps a slow client's backlog.
            async with websockets.serve(
                ws_handler, host, port,
                ping_interval=30, ping_timeout=90, max_queue=48, close_timeout=5,
            ):
                MAIN_LOOP = loop  # only publish the loop once the bind succeeded
                logger.info(f"WebSocket push engine listening on ws://{host}:{port}")
                asyncio.create_task(candle_streaming_loop())
                await asyncio.Future()

        try:
            loop.run_until_complete(_main())
        except Exception as e:
            MAIN_LOOP = None
            logger.error(f"WebSocket server exited: {e}")

    thread = threading.Thread(target=_run, name="WebSocketServerThread", daemon=True)
    thread.start()
    return thread


if __name__ == "__main__":
    print(f"Starting standalone WebSocket server on ws://{WS_HOST}:{WS_PORT}...")
    t = start_websocket_server_thread(WS_HOST, WS_PORT)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nWebSocket server stopped.")
