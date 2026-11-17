import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from urllib.request import urlopen


PLUGIN_NAME = "Arr Stack Connector"
ROOT = Path(__file__).resolve().parent
PLUGIN_KEY = ROOT.name.replace(" ", "_").lower()
STATE_DIR = Path(os.environ.get("ARR_STACK_CONNECTOR_STATE_DIR") or "/data/arr_stack_connector")
STATE_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = STATE_DIR / "servarr_service.log"
BOOTSTRAP_LOG_FILE = STATE_DIR / "servarr_service_bootstrap.log"


class _PluginLogAdapter(logging.LoggerAdapter):
    """Prefix messages while retaining Dispatcharr's plugin logger namespace."""

    def process(self, msg, kwargs):
        return f"[{PLUGIN_NAME}] {msg}", kwargs


log = _PluginLogAdapter(logging.getLogger("apps.plugins.loader"), {})


def _installed_version():
    try:
        payload = json.loads((ROOT / "plugin.json").read_text())
        return str(payload.get("version") or "").strip()
    except Exception:
        return ""


def _dispatcharr_app_roots():
    """Return import roots that contain Dispatcharr's ``dispatcharr`` package.

    Dispatcharr images have used more than one application root (notably
    ``/app`` and ``/opt/dispatcharr``). The detached service cannot rely on the
    parent's in-memory imports, so discover the real root while still inside the
    Dispatcharr process and pass it through PYTHONPATH.
    """
    candidates = []

    module = sys.modules.get("dispatcharr")
    module_file = getattr(module, "__file__", None) if module else None
    if module_file:
        try:
            candidates.append(Path(module_file).resolve().parent.parent)
        except (OSError, RuntimeError):
            pass

    try:
        candidates.append(Path.cwd())
    except OSError:
        pass

    for entry in sys.path:
        if entry:
            candidates.append(Path(entry))

    candidates.extend((Path("/app"), Path("/opt/dispatcharr")))

    roots = []
    seen = set()
    for candidate in candidates:
        try:
            path = candidate.resolve()
        except (OSError, RuntimeError):
            continue

        if path.name == "dispatcharr" and (path / "settings.py").is_file():
            path = path.parent
        if not (path / "dispatcharr" / "settings.py").is_file():
            continue

        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        roots.append(path)
    return roots


def _dedupe_path_entries(entries):
    output = []
    seen = set()
    for entry in entries:
        text = str(entry or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        output.append(text)
    return output


class Plugin:
    name = PLUGIN_NAME
    version = "0.2.1-beta.1"
    description = "Arr stack bridge for raw Dispatcharr VOD providers backed by Mustarrd."
    author = "matrix2669"

    fields = []
    actions = []

    def __init__(self):
        if os.environ.get("ARR_STACK_CONNECTOR_SERVICE", "").lower() in {"1", "true", "yes"}:
            return
        try:
            self._ensure_service()
        except Exception:
            log.exception(
                "Detached Newznab/SAB service failed to start; plugin remains loaded for diagnostics"
            )

    def _desired_version(self):
        return _installed_version() or self.version

    def _interpreter(self):
        candidates = [
            os.environ.get("DISPATCHARR_PYTHON"),
            "/dispatcharrpy/bin/python",
            str(Path(sys.prefix) / "bin" / "python"),
            shutil.which("python3"),
            shutil.which("python"),
        ]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return str(candidate)
        raise RuntimeError("Unable to locate Python interpreter for detached service")

    def _child_pythonpath(self):
        roots = [str(ROOT)]
        roots.extend(str(path) for path in _dispatcharr_app_roots())
        current = os.environ.get("PYTHONPATH")
        if current:
            roots.extend(current.split(os.pathsep))
        return os.pathsep.join(_dedupe_path_entries(roots))

    def _lifecycle(self):
        try:
            from .servarr_bridge import lifecycle
        except ImportError:
            from servarr_bridge import lifecycle
        return lifecycle

    def _ensure_service(self):
        env = os.environ.copy()
        env.update(ARR_STACK_CONNECTOR_PLUGIN_KEY=PLUGIN_KEY,
                   ARR_STACK_CONNECTOR_PLUGIN_DIR=str(ROOT),
                   ARR_STACK_CONNECTOR_STATE_DIR=str(STATE_DIR),
                   ARR_STACK_CONNECTOR_SERVICE="1", DISPATCHARR_SKIP_PLUGIN_AUTODISCOVERY="1",
                   PYTHONPATH=self._child_pythonpath())
        roots = _dispatcharr_app_roots()
        if roots:
            env["DISPATCHARR_APP_ROOT"] = str(roots[0])
        return self._lifecycle().ensure_watchdog(ROOT, STATE_DIR, self._interpreter(), env)

    def _health(self):
        state = self._lifecycle().owner_status(STATE_DIR)
        if not state.get("alive") or not state.get("listening"):
            return None
        host = state.get("host") or "127.0.0.1"
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1" if host == "0.0.0.0" else "[::1]"
        elif ":" in host and not host.startswith("["):
            host = "[" + host + "]"
        try:
            with urlopen(f"http://{host}:{int(state.get('port') or 9192)}/health", timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if response.status == 200 and payload.get("owner") == state.get("owner"):
                    return payload
        except Exception:
            pass
        return None

    def run(self, action_id, params=None, context=None):
        lifecycle = self._lifecycle()
        if action_id == "status":
            state = lifecycle.owner_status(STATE_DIR)
            health = self._health()
            return {
                "status": bool(health), "service": health or {},
                "pid": state.get("pid") if state.get("alive") else None,
                "controller": state, "installed_version": self._desired_version(),
                "dispatcharr_app_roots": [str(path) for path in _dispatcharr_app_roots()],
                "log_file": str(LOG_FILE), "bootstrap_log_file": str(BOOTSTRAP_LOG_FILE),
            }
        if action_id not in {"restart", "stop"}:
            raise ValueError(f"Unknown action: {action_id}")
        token = lifecycle.request(STATE_DIR, action_id)
        self._ensure_service()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if lifecycle.read_json(lifecycle.control_path(STATE_DIR)).get("command") != token:
                return {"status": False, "message": "Superseded by a newer service command"}
            state = lifecycle.owner_status(STATE_DIR)
            if state.get("command") == token and state.get("alive"):
                if action_id == "stop" and state.get("phase") == "stopped":
                    return {"status": True, "message": "Service stopped; automatic starts inhibited until Restart"}
                if action_id == "restart" and state.get("phase") == "running":
                    health = self._health()
                    if health:
                        return {"status": True, "message": "Service restarted", "service": health}
                if action_id == "restart" and state.get("phase") == "disabled":
                    return {"status": False, "message": "Plugin is disabled; service will start automatically when enabled"}
            time.sleep(0.1)
        return {"status": False, "message": "Command saved; automatic reconciliation is still pending", "controller": lifecycle.owner_status(STATE_DIR)}

    def stop(self, context=None):
        # Host callbacks precede commits in some paths and follow them in others.
        # They are availability/wake hints, NEVER guessed desired-state changes.
        # The independent owner reads the actual committed row (missing=off).
        self._ensure_service()
