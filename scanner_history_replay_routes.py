"""Isolated read-only history routes. Never use the live delivery JSON helper.

Production requires explicit enablement, a dedicated read DSN and an access
token. A server-injected local preview is loopback-only, never a DB fallback.
"""
from __future__ import annotations

import hmac
from http import HTTPStatus
import json
import os
from pathlib import Path
import posixpath
import threading
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parent
PREFIX = "/api/scanner_replay"
ASSETS = {"/scanner_replay.html": ("scanner_replay.html", "text/html; charset=utf-8"),
          "/scanner_replay.js": ("scanner_replay.js", "text/javascript; charset=utf-8"),
          "/scanner_replay.css": ("scanner_replay.css", "text/css; charset=utf-8")}
LOCK = threading.Lock()
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")


def is_replay_path(path):
    return path == PREFIX or path.startswith(PREFIX + "/") or path in ASSETS


def _canonical_path(path):
    return "/" + posixpath.normpath(unquote(path, errors="replace")).lstrip("/")


def deny_private_static(handler, parsed):
    """Do not let generic static GET/HEAD bypass archive access controls.

    Existing public input snapshots remain available. Raw telemetry, hidden
    paths and Python sources are not public website assets.
    """
    decoded = unquote(parsed.path, errors="replace")
    normalized = "/" + posixpath.normpath(decoded).lstrip("/")
    normalized_lower = normalized.lower()
    parts = normalized.split("/")
    private = (any(part.startswith(".") and part not in (".", "..") for part in decoded.split("/"))
               or normalized_lower == "/inputs/scanner_telemetry"
               or normalized_lower.startswith("/inputs/scanner_telemetry/")
               or any(part in ("__pycache__", "venv", ".venv") for part in parts)
               or normalized_lower.endswith((".py", ".pyc", ".pyo")))
    if not private:
        return False
    respond(handler, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
    return True


def respond(handler, status, payload, mime="application/json; charset=utf-8"):
    content = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
    if len(content) > 4 * 1024 * 1024:
        status, content = HTTPStatus.SERVICE_UNAVAILABLE, b'{"ok":false,"error":"replay_response_too_large"}'
    # NiftyHandler.end_headers checks this to suppress its live wildcard CORS.
    handler._history_replay_response = True
    handler.send_response(int(status))
    handler.send_header("Content-Type", mime)
    handler.send_header("Content-Length", str(len(content)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("X-Frame-Options", "DENY")
    handler.send_header("Content-Security-Policy", CSP)
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.end_headers()
    if getattr(handler, "command", "GET") != "HEAD":
        handler.wfile.write(content)


def _header_values(headers, name):
    if hasattr(headers, "get_all"):
        return headers.get_all(name, [])
    value = headers.get(name)
    return [] if value is None else [value]


def _access(handler):
    from scanner_history_replay import ReplayError
    preview = getattr(handler.server, "scanner_replay_preview", False) is True
    if not preview and os.environ.get("SCANNER_REPLAY_ENABLED") != "1":
        raise ReplayError("replay_disabled", 404)
    hosts = _header_values(handler.headers, "Host")
    if len(hosts) != 1:
        raise ReplayError("replay_origin_rejected", 403)
    host = hosts[0]
    origins = _header_values(handler.headers, "Origin")
    sites = _header_values(handler.headers, "Sec-Fetch-Site")
    if sites and (len(sites) != 1 or sites[0] not in ("same-origin", "none")):
        raise ReplayError("replay_origin_rejected", 403)
    if preview:
        port = handler.server.server_port
        if handler.client_address[0] not in ("127.0.0.1", "::1") or host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            raise ReplayError("replay_origin_rejected", 403)
        expected_origin = "http://" + host
    else:
        expected_origin = os.environ.get("SCANNER_REPLAY_ORIGIN", "").rstrip("/")
        parsed = urlsplit(expected_origin)
        if (parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query
                or parsed.fragment or parsed.username or parsed.password or parsed.netloc != host):
            raise ReplayError("replay_origin_not_configured", 503)
    if origins and (len(origins) != 1 or origins[0] != expected_origin):
        raise ReplayError("replay_origin_rejected", 403)
    if preview:
        return
    token = os.environ.get("SCANNER_REPLAY_TOKEN", "")
    if len(token) < 24:
        raise ReplayError("replay_access_not_configured", 503)
    values = _header_values(handler.headers, "Authorization")
    supplied = values[0] if len(values) == 1 else ""
    if not hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
        raise ReplayError("replay_auth_required", 401)


def _service(handler):
    from scanner_history_replay import PostgresStore, ReplayService
    with LOCK:
        if not hasattr(handler.server, "scanner_replay_service"):
            handler.server.scanner_replay_service = ReplayService(PostgresStore())
        return handler.server.scanner_replay_service


def handle_get(handler, parsed):
    """Return whether this route was handled; call before the live API branches."""
    path = _canonical_path(parsed.path)
    if not is_replay_path(path):
        return False
    from scanner_history_replay import ReplayError
    try:
        params = parse_qs(parsed.query, keep_blank_values=True)
        if any(len(v) != 1 for v in params.values()):
            raise ReplayError("invalid_replay_parameters", 400)
        if path in ASSETS:
            if params:
                raise ReplayError("invalid_replay_parameters", 400)
            name, mime = ASSETS[path]
            asset = ROOT / name
            if asset.resolve().parent != ROOT.resolve() or not asset.is_file():
                raise ReplayError("replay_asset_unavailable", 404)
            respond(handler, HTTPStatus.OK, asset.read_bytes(), mime)
            return True
        _access(handler)
        if path == PREFIX + "/sessions" and not params:
            payload = _service(handler).sessions()
        elif path == PREFIX + "/snapshots" and set(params) == {"date"}:
            payload = _service(handler).snapshots(params["date"][0])
        elif path == PREFIX + "/snapshot" and set(params) == {"date", "snapshot_id"}:
            payload = _service(handler).snapshot(params["date"][0], params["snapshot_id"][0])
        else:
            raise ReplayError("invalid_replay_parameters", 400)
        respond(handler, HTTPStatus.OK, payload)
    except ReplayError as exc:
        respond(handler, exc.status, {"ok": False, "error": exc.code, "historical": True})
    except Exception:
        respond(handler, HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "replay_unavailable", "historical": True})
    return True


def reject_write(handler, parsed):
    if not is_replay_path(_canonical_path(parsed.path)):
        return False
    handler.close_connection = True
    respond(handler, HTTPStatus.METHOD_NOT_ALLOWED, {"ok": False, "error": "replay_read_only"})
    return True
