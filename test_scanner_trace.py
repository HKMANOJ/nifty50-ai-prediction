"""Offline trace tests; temporary state and mocked mirroring/recovery only."""
import copy
import asyncio
import json
import tempfile
import types
import unittest
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import scanner_telemetry as log
import scanner_trace as trace
import stock_suggestion_index as scanner


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for p in (patch.object(log, "ROOT", self.root), patch.object(trace, "_recover", return_value=None)):
            p.start()
            self.addCleanup(p.stop)
        self.day = "2026-10-07"
        self.sid = log.signal_id(self.day, "TEST", "bullish", "Long buildup")
        self.scan = log.new_scan()
        self.row = {"signal_id": self.sid, "symbol": "TEST", "side": "bullish", "setup": "Long buildup",
                    "qualified": True, "included_in_payload": True, "is_breakout": True,
                    "rank": 7, "score": 88, "ltp": 100, "stop_loss": 95, "target": 105,
                    "breakout_time": "09:20 AM", "reason": "included", "percent_change": 1.5}
        self.payload = {"session_date": self.day, "generated_at_ist":"2026-10-07T09:25:00+05:30"}

    def events(self, rows, when, replay=None):
        return trace.trace_events(self.payload, rows, self.scan, when, replay or {"candles_by_symbol":{},"sources":{}})

    def test_lifecycle_rank_and_independent_wave_identity(self):
        first = self.events([self.row], "2026-10-07T09:26:00+05:30")
        wave = self.row["wave_id"]
        self.assertTrue(wave.startswith("wave_"))
        self.assertEqual(next(e for e in first if e["event_type"]=="signal_lifecycle")["payload"]["transition"], "first_observed")
        self.row["rank"] = 3
        second = self.events([self.row], "2026-10-07T09:31:00+05:30")
        rank = next(e["payload"] for e in second if e["event_type"]=="rank_transition")
        self.assertEqual((rank["previous_rank"],rank["rank"]),(7,3))
        self.assertTrue(rank["entered_top5"])
        self.assertEqual(self.row["wave_id"],wave)
        self.assertEqual(self.row["first_qualified_at_ist"],"2026-10-07T09:26:00+05:30")
        self.row.update(qualified=False,is_breakout=False,rank=None,reason="rally_gate_failed")
        third = self.events([self.row], "2026-10-07T09:36:00+05:30")
        self.assertTrue(next(e["payload"] for e in third if e["event_type"]=="rank_transition")["exited_top5"])
        self.row.update(qualified=True,is_breakout=True,rank=2,breakout_time="09:40 AM")
        self.events([self.row], "2026-10-07T09:46:00+05:30")
        self.assertNotEqual(self.row["wave_id"],wave)

    def test_missing_rows_are_not_inferred_invalidated(self):
        self.events([self.row], "2026-10-07T09:26:00+05:30")
        events = self.events([], "2026-10-07T09:31:00+05:30")
        self.assertFalse(any(e["event_type"]=="signal_lifecycle" for e in events))
        self.assertEqual(events[-1]["payload"]["unobserved_previous_candidate_count"],1)

    def test_outcomes_exclude_forming_and_origin_straddling_candles_and_mark_ambiguity(self):
        self.events([self.row], "2026-10-07T09:26:00+05:30")
        def bar(clock, low=94,high=106):
            return {"timestamp":int(datetime.fromisoformat(f"2026-10-07T{clock}:00+05:30").timestamp()),
                    "open":100,"close":100,"high":high,"low":low,"volume":1}
        replay = {"candles_by_symbol":{"TEST":[bar("09:25"),bar("09:30"),bar("09:35",50,150)]}}
        events = self.events([self.row], "2026-10-07T09:36:00+05:30",replay)
        result = next(e["payload"] for e in events if e["event_type"]=="wave_outcome")
        self.assertEqual(result["completed_bar_count"],1)
        self.assertEqual(result["mfe_pct_observed"],6)
        self.assertEqual(result["mae_pct_observed"],6)
        self.assertEqual(result["first_touch_observed"]["order"],"ambiguous_same_bar")
        events = self.events([self.row], "2026-10-07T09:46:00+05:30",{"candles_by_symbol":{"TEST":[bar("09:40")]}})
        result = next(e["payload"] for e in events if e["event_type"]=="wave_outcome")
        self.assertEqual(result["coverage"],"partial")

    def test_replay_captures_inputs_and_configs_without_credentials(self):
        fake = types.SimpleNamespace(INDEX_LIKE_SYMBOLS={"INDEX"}, MIN_SCORE=65, DATABASE_URL="secret",
                                     load_fo_lot_sizes=lambda:{"TEST":100})
        inputs = trace.replay_inputs(fake,[{"symbol":"TEST","ltp":100}],[],{"TEST":{"oi_change":1}},{},{},{},[])
        self.assertEqual(inputs["effective_numeric_config"],{"MIN_SCORE":65})
        self.assertNotIn("secret",json.dumps(inputs))
        events = self.events([self.row],"2026-10-07T09:26:00+05:30",inputs)
        replay = next(e["payload"] for e in events if e["event_type"]=="scan_replay")
        self.assertEqual(replay["inputs"]["gainers"][0]["ltp"],100)
        self.assertEqual(len(replay["input_hash"]),64)

    def test_source_capture_does_not_copy_query_credentials(self):
        trace.begin(self.scan,"2026-10-07T09:26:00+05:30")
        trace.safe_source("test",endpoint=trace.endpoint_name("https://feed.example/quote?token=secret"),status="fetch_failed")
        result=trace.finish_capture()
        self.assertNotIn("secret",json.dumps(result))
        self.assertEqual(result["sources"][0]["status"],"fetch_failed")
        fields=trace.supplied_timestamps({"FOSec":{"timestamp":"07-Oct-2026 09:20","data":[{"lastUpdateTime":"09:19"}]},"secret":"private"})
        self.assertEqual(fields["$.FOSec.timestamp"],"07-Oct-2026 09:20")
        self.assertNotIn("private",json.dumps(fields))

    def test_state_recovers_first_qualification_and_wave_after_restart(self):
        self.events([self.row],"2026-10-07T09:26:00+05:30")
        state=log._load(self.root/f"trace-state-{self.day}.json",{})
        wave=self.row["wave_id"]
        (self.root/f"trace-state-{self.day}.json").unlink()
        with patch.object(trace,"_recover",return_value=state):
            self.events([self.row],"2026-10-07T09:31:00+05:30")
        self.assertEqual(self.row["wave_id"],wave)
        self.assertEqual(self.row["first_qualified_at_ist"],"2026-10-07T09:26:00+05:30")

    def test_display_rank_receipts_track_return_to_previous_rank(self):
        today=datetime.now(log.IST).date().isoformat()
        log._atomic_json(self.root/f"issued-{today}-{self.scan}.json",{
            "signals":{self.sid:{"symbol":"TEST","side":"bullish","wave_id":"wave_test"}},
            "prepared_at_ist":datetime.now(log.IST).isoformat()})
        receipt={"session_date":today,"snapshot_id":self.scan,"signal_id":self.sid,"panel":"ranking",
                 "side":"bullish","display_rank":3,"client_session_id":"browser_session_12345",
                 "rendered_at":datetime.now(log.IST).isoformat(),"page_visible":True,"in_viewport":True}
        with patch.object(log,"write_events") as write:
            for rank in (3,2,3):
                receipt["display_rank"]=rank
                receipt["snapshot_id"]=log.new_scan()
                log._atomic_json(self.root/f"issued-{today}-{receipt['snapshot_id']}.json",
                    log._load(self.root/f"issued-{today}-{self.scan}.json",{}))
                self.assertEqual(log.record_display_receipts({"receipts":[dict(receipt)]})["accepted"],1)
            self.assertEqual(write.call_args.args[0][0]["payload"]["previous_display_rank"],2)
            self.assertEqual(write.call_args.args[0][0]["event_type"],"display_rank_observed")

    def test_client_events_are_validated_deduplicated_and_do_not_store_raw_errors(self):
        today=datetime.now(log.IST).date().isoformat()
        row={"kind":"load_failed","session_date":today,"client_session_id":"browser_session_12345",
             "client_event_id":str(uuid.uuid4()),"client_at":datetime.now(log.IST).isoformat(),
             "reason":"network_error","transport":"http","elapsed_ms":100,"raw_error":"secret"}
        with patch.object(log,"write_events") as write:
            self.assertEqual(log.record_client_events({"events":[row,row]})["accepted"],1)
            self.assertEqual(log.record_client_events({"events":[row]})["accepted"],0)
            self.assertNotIn("secret",json.dumps(write.call_args_list[0].args))
            with self.assertRaises(ValueError):
                log.record_client_events({"events":[{**row,"kind":"arbitrary"}]})

    def test_cache_provenance_does_not_change_candles(self):
        candles=[{"timestamp":1,"close":100}]
        trace.begin(self.scan,"2026-10-07T09:26:00+05:30")
        with patch.object(scanner,"_load_candle_cache"), patch.object(scanner,"_CANDLE_CACHE",{"TEST":(scanner._time.time(),candles)}):
            data={}
            scanner.bulk_fetch_candles(["TEST"],data)
        self.assertEqual(data,{"TEST":candles})
        self.assertEqual(trace.finish_capture()["sources"][0]["status"],"cache_hit")

    def test_prepare_snapshot_integrates_wave_ids_and_preserves_ranking(self):
        payload={**self.payload,"bullish":[{**self.row}],"bearish":[],"instant_triggers":[{**self.row}]}
        before=payload["bullish"][0]["rank"]
        with patch.object(log,"write_events") as write, patch.object(log,"_restore_candidate_outcomes",return_value={}):
            log.prepare_snapshot(payload,[dict(self.row)],self.scan,replay={"candles_by_symbol":{},"sources":{}})
        self.assertEqual(payload["bullish"][0]["rank"],before)
        wave=payload["bullish"][0]["wave_id"]
        self.assertTrue(wave.startswith("wave_"))
        registry=log._load(self.root/f"issued-{self.day}-{self.scan}.json",{})
        self.assertEqual(registry["signals"][self.sid]["wave_id"],wave)
        types_found={e["event_type"] for e in write.call_args.args[0]}
        self.assertTrue({"scan_replay","signal_lifecycle","rank_transition","trace_state"}<=types_found)

    def test_http_delivery_metadata_does_not_mutate_cached_snapshot(self):
        from unittest.mock import Mock
        from serve_nifty50_app import NiftyHandler
        handler=NiftyHandler.__new__(NiftyHandler)
        handler.send_response=Mock();handler.send_header=Mock();handler.end_headers=Mock()
        handler.wfile=Mock()
        payload={"ok":True,"snapshot_id":self.scan,"bullish":[]}
        from http import HTTPStatus
        with patch.object(trace,"submit_health") as health:
            handler._send_json(HTTPStatus.OK,payload)
        sent=json.loads(handler.wfile.write.call_args.args[0])
        self.assertNotIn("_delivery",payload)
        self.assertEqual(sent["_delivery"]["transport"],"http")
        self.assertEqual(health.call_args.args[0],"delivery_written")

    def test_websocket_delivery_counts_failures_without_mutating_snapshot(self):
        import websocket_server as ws
        messages=[]
        class Client:
            async def send(self,message): messages.append(json.loads(message))
        payload={"snapshot_id":self.scan,"bullish":[]}
        def dispatch(coro,loop): return asyncio.run(coro)
        with patch.object(ws,"MAIN_LOOP",object()), patch.object(ws,"CONNECTED_CLIENTS",{Client()}), \
             patch.object(ws.asyncio,"run_coroutine_threadsafe",side_effect=dispatch), patch.object(trace,"submit_health") as health:
            ws.broadcast_scanner_update_sync(payload)
        self.assertNotIn("_delivery",payload)
        self.assertEqual(messages[0]["payload"]["_delivery"]["transport"],"websocket")
        self.assertEqual(health.call_args.kwargs["attempted_clients"],1)

    def test_client_http_endpoint_checks_batch_and_reports_acceptance(self):
        import threading
        import urllib.request
        import urllib.error
        from http.server import ThreadingHTTPServer
        from serve_nifty50_app import NiftyHandler
        today=datetime.now(log.IST).date().isoformat()
        row={"kind":"load_failed","session_date":today,"client_session_id":"browser_session_12345",
             "client_event_id":str(uuid.uuid4()),"client_at":datetime.now(log.IST).isoformat(),"reason":"network_error"}
        server=ThreadingHTTPServer(("127.0.0.1",0),NiftyHandler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            url=f"http://127.0.0.1:{server.server_port}/api/stock_suggestions/client_events"
            with patch.object(log,"write_events"):
                request=urllib.request.Request(url,data=json.dumps({"events":[row]}).encode(),headers={"Content-Type":"application/json"})
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(json.load(response)["accepted"],1)
                request=urllib.request.Request(url,data=b'{"events":"bad"}',headers={"Content-Type":"application/json"})
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request)
                self.assertEqual(caught.exception.code,400)
                caught.exception.close()
        finally:
            server.shutdown();server.server_close();thread.join()


if __name__=="__main__":
    unittest.main()
