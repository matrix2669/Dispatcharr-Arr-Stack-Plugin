import json
import logging
import os
import time
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from django.db import close_old_connections

from .config import get_settings, normalized_api_key
from .newznab import caps_xml, grab_nzb, rss_xml, search_movies, search_tv
from .recent import recent_movie_results, recent_tv_results
from . import sab


logger = logging.getLogger(__name__)
SERVICE_VERSION = str(os.environ.get("ARR_STACK_CONNECTOR_RUNNING_VERSION") or "unknown")


def _one(params, name, default=None):
    values = params.get(name)
    return values[-1] if values else default


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _multipart_file(content_type, body, field_name="name"):
    if "multipart/form-data" not in str(content_type or "").lower():
        raise ValueError("addfile requires multipart/form-data")
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + content_type.encode("latin-1") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    )
    if not message.is_multipart():
        raise ValueError("Invalid multipart request")
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name == field_name:
            return part.get_payload(decode=True) or b""
    raise ValueError(f"Missing multipart field: {field_name}")


class Handler(BaseHTTPRequestHandler):
    server_version = "ArrStackConnector/0.2"

    def log_message(self, fmt, *args):
        if self.path == "/health":
            logger.debug("%s - %s", self.address_string(), fmt % args)
            return
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _send(self, status, body, content_type="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, separators=(",", ":")).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _settings_and_auth(self, params):
        settings = get_settings()
        expected = normalized_api_key(settings)
        provided = str(_one(params, "apikey", "") or "")
        import hmac
        if not expected or not provided or not hmac.compare_digest(expected, provided):
            raise PermissionError("Invalid API key")
        return settings

    def _base_url(self):
        proto = (self.headers.get("X-Forwarded-Proto") or "http").split(",", 1)[0].strip()
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "127.0.0.1"
        return f"{proto}://{host}".rstrip("/")

    def _handle_newznab(self, params, settings):
        mode = str(_one(params, "t", "search") or "search").lower()
        if mode == "caps":
            return self._send(200, caps_xml(), "application/xml")
        if mode == "movie":
            tmdbid = _one(params, "tmdbid")
            query = _one(params, "q")
            if not str(tmdbid or "").strip() and not str(query or "").strip():
                results = recent_movie_results(settings)
            else:
                results = search_movies(tmdbid, query, settings)
        elif mode == "tvsearch":
            season_raw = _one(params, "season")
            ep_raw = _one(params, "ep")
            if season_raw in {None, ""}:
                results = recent_tv_results(settings)
            else:
                results = search_tv(
                    _one(params, "tmdbid"),
                    _one(params, "q"),
                    _int(season_raw),
                    _int(ep_raw) if ep_raw not in {None, ""} else None,
                    settings,
                    tvdbid=_one(params, "tvdbid"),
                )
        elif mode == "search":
            results = []
        else:
            raise ValueError(f"Unsupported Newznab search type: {mode}")
        offset = max(0, _int(_one(params, "offset"), 0))
        limit = min(100, max(1, _int(_one(params, "limit"), 100)))
        xml = rss_xml(results, self._base_url(), settings["api_key"], offset=offset, limit=limit)
        return self._send(200, xml, "application/xml")

    def _handle_sab(self, params, settings, body=None):
        mode = str(_one(params, "mode", "") or "").lower()
        if mode == "version":
            result = sab.version()
        elif mode == "get_config":
            result = sab.get_config(settings)
        elif mode == "fullstatus":
            result = sab.fullstatus(settings)
        elif mode == "queue":
            if _one(params, "name") == "delete":
                result = sab.delete_job(settings, _one(params, "value"), history=False)
            else:
                result = sab.queue(
                    settings,
                    category=_one(params, "category"),
                    start=_int(_one(params, "start"), 0),
                    limit=_int(_one(params, "limit"), 100),
                )
        elif mode == "history":
            if _one(params, "name") == "delete":
                result = sab.delete_job(settings, _one(params, "value"), history=True)
            else:
                result = sab.history(
                    settings,
                    category=_one(params, "category"),
                    start=_int(_one(params, "start"), 0),
                    limit=_int(_one(params, "limit"), 100),
                )
        elif mode == "retry":
            result = sab.retry_job(settings, _one(params, "value"))
        elif mode == "addfile":
            if body is None:
                raise ValueError("addfile requires POST")
            nzb = _multipart_file(self.headers.get("Content-Type", ""), body)
            result = sab.addfile(nzb, _one(params, "cat"), settings)
        else:
            raise ValueError(f"Unsupported SAB mode: {mode}")
        return self._send(200, result)

    def _dispatch(self, body=None):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query, keep_blank_values=True)
        if parsed.path == "/health":
            return self._send(200, {
                "status": "ok",
                "service": "arr-stack-connector",
                "version": SERVICE_VERSION,
                "pid": os.getpid(),
                "owner": os.environ.get("ARR_STACK_CONNECTOR_OWNER", ""),
            })

        settings = self._settings_and_auth(params)
        if parsed.path.startswith("/grab/") and parsed.path.endswith(".nzb"):
            token = unquote(parsed.path[len("/grab/"):-len(".nzb")])
            nzb = grab_nzb(token, settings)
            return self._send(
                200,
                nzb,
                "application/x-nzb",
                {"Content-Disposition": 'attachment; filename="mustarrd.nzb"'},
            )
        if parsed.path != "/api":
            return self._send(404, {"status": False, "error": "Not found"})
        if "mode" in params:
            return self._handle_sab(params, settings, body=body)
        return self._handle_newznab(params, settings)

    def do_GET(self):
        try:
            self._dispatch()
        except PermissionError as exc:
            self._send(401, {"status": False, "error": str(exc)})
        except Exception as exc:
            logger.exception("Request failed")
            self._send(400, {"status": False, "error": str(exc)})
        finally:
            close_old_connections()

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        try:
            length = _int(self.headers.get("Content-Length"), 0)
            if length < 0 or length > 10 * 1024 * 1024:
                raise ValueError("Request body too large")
            body = self.rfile.read(length) if length else b""
            self._dispatch(body=body)
        except PermissionError as exc:
            self._send(401, {"status": False, "error": str(exc)})
        except Exception as exc:
            logger.exception("Request failed")
            self._send(400, {"status": False, "error": str(exc)})
        finally:
            close_old_connections()


class ManagedHTTPServer(ThreadingHTTPServer):
    def service_actions(self):
        self.lifecycle_heartbeat = time.monotonic()


def create_server(bind=None):
    """Construct only; the reconciliation owner controls threads and shutdown."""
    # Startup also initializes a blank API key; an explicit bind must not
    # skip that side effect or use a second configuration read for its address.
    settings = get_settings()
    if bind is None:
        bind = (str(settings.get("listen_host") or "0.0.0.0"),
                int(settings.get("listen_port") or 9192))
    server = ManagedHTTPServer(bind, Handler)
    server.daemon_threads = True
    return server


def run_server():
    # Compatibility for direct internal callers; service.py uses create_server.
    server = create_server()
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
