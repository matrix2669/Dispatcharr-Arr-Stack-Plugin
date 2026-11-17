import fcntl
import errno
import threading
import types
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen


PLUGIN_NAME = "Arr Stack Connector"
ROOT = Path(__file__).resolve().parent
PLUGIN_KEY = ROOT.name.replace(" ", "_").lower()
STATE_DIR = Path(os.environ.get("ARR_STACK_CONNECTOR_STATE_DIR") or "/data/arr_stack_connector")
STATE_DIR.mkdir(parents=True, exist_ok=True)
PID_FILE = STATE_DIR / ".servarr_service.pid"
LOCK_FILE = STATE_DIR / ".servarr_service.lock"
CONTROL_FILE = STATE_DIR / ".servarr_service.control.json"
LOG_FILE = STATE_DIR / "servarr_service.log"
BOOTSTRAP_LOG_FILE = STATE_DIR / "servarr_service_bootstrap.log"
SERVICE_SCRIPT = (ROOT / "service.py").resolve()
_UNSET = object()


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
    version = "0.2.0"
    description = "Arr stack bridge for raw Dispatcharr VOD providers backed by Mustarrd."
    author = "matrix2669"

    fields = []
    actions = []

    def __init__(self):
        if os.environ.get("ARR_STACK_CONNECTOR_SERVICE", "").lower() in {"1", "true", "yes"}:
            return
        try:
            epoch = self._control().get("epoch")
            if not self._ensure_service(epoch):
                self._queue_retry(epoch)
        except Exception:
            log.exception(
                "Detached Newznab/SAB service failed to start; plugin remains loaded for diagnostics"
            )

    @staticmethod
    def _native(name):
        primitive = getattr(threading, name)
        try:
            import gevent.monkey
            if gevent.monkey.is_module_patched("threading"):
                return gevent.monkey.get_original("threading", name)
        except ImportError:
            pass
        return primitive

    def _control(self):
        try:
            return json.loads(CONTROL_FILE.read_text())
        except FileNotFoundError:
            return {}

    def _configuration(self):
        # Read only lifecycle identity, never credentials. A disable transaction
        # may still be uncommitted in another worker, so the stop marker also
        # fences starts against its observed configuration revision.
        from apps.plugins.models import PluginConfig
        cfg = PluginConfig.objects.filter(key=PLUGIN_KEY).values("enabled", "updated_at").first()
        return (bool(cfg and cfg["enabled"]), str(cfg["updated_at"]) if cfg else "")

    def _enabled_after_disable(self, control):
        enabled, revision = self._configuration()
        if not enabled:
            return False
        barrier = control.get("disabled_before")
        return not barrier or datetime.fromisoformat(revision) > datetime.fromisoformat(barrier)

    def _may_start(self, epoch):
        control = self._control()
        return (control.get("epoch") == epoch and not control.get("stopped")
                and self._enabled_after_disable(control))

    def _request(self, stopped=None, disable=False):
        # Serialize read/modify/write independently of service startup. Restart
        # may clear an operator Stop, but cannot erase a pending Disable fence.
        with self._operation_lock(timeout=2, path=CONTROL_FILE.with_suffix(".lock")) as acquired:
            if not acquired:
                raise RuntimeError("Lifecycle request timed out updating control state")
            control = self._control()
            control["epoch"] = secrets.token_hex(16)
            if stopped is not None:
                control["stopped"] = stopped
            if disable:
                _, revision = self._configuration()
                cutoff = datetime.now(timezone.utc)
                for value in (revision, control.get("disabled_before")):
                    if value:
                        cutoff = max(cutoff, datetime.fromisoformat(value))
                control["disabled_before"] = cutoff.isoformat()
            temporary = CONTROL_FILE.with_name(CONTROL_FILE.name + "." + control["epoch"])
            try:
                with temporary.open("x") as out:
                    os.chmod(temporary, 0o600)
                    json.dump(control, out)
                os.replace(temporary, CONTROL_FILE)
            finally:
                temporary.unlink(missing_ok=True)
            return control["epoch"]

    @contextmanager
    def _operation_lock(self, timeout=0, path=None):
        # Never block in flock: another constructor can run in the same gevent
        # process while the owner is waiting for its local HTTP health check.
        with (path or LOCK_FILE).open("a+") as handle:
            deadline = time.monotonic() + timeout
            acquired = False
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.05)
            try:
                yield acquired
            finally:
                if acquired:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _queue_retry(self, epoch):
        # Plugin modules can be re-imported on reload. Keep only a tiny
        # process-local retry registry outside those module identities.
        key = "_arr_stack_start_retries"
        candidate = types.ModuleType(key)
        candidate.lock = self._native("RLock")()
        candidate.jobs = {}
        state = sys.modules.setdefault(key, candidate)
        identity = (os.getpid(), str(STATE_DIR))
        with state.lock:
            current = state.jobs.get(identity)
            if current and current[0] == epoch and current[1].is_alive():
                return
            def retry():
                deadline = time.monotonic() + 30
                try:
                    while time.monotonic() < deadline:
                        if not self._may_start(epoch):
                            return
                        if self._ensure_service(epoch):
                            return
                        time.sleep(0.1)
                    log.error("Deferred service startup timed out waiting for lifecycle ownership")
                except Exception:
                    log.exception("Deferred Newznab/SAB service startup failed")
                finally:
                    # Only this private retry thread owns these connections.
                    # Never close a caller's transactional connection in stop().
                    try:
                        from django.db import connections
                        connections.close_all()
                    except ImportError:
                        pass
            thread = self._native("Thread")(target=retry, daemon=True, name="arr-stack-start-retry")
            state.jobs[identity] = (epoch, thread)
            thread.start()

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

    def _read_pid(self):
        try:
            return int(PID_FILE.read_text().strip())
        except Exception:
            return None

    def _pid_is_service(self, pid):
        if not pid or pid <= 1:
            return False
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except Exception:
            return False
        return str(SERVICE_SCRIPT) in cmdline

    def _discover_service_pid(self):
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            pid = int(proc.name)
            if self._pid_is_service(pid):
                return pid
        return None

    def _service_pid(self):
        pid = self._read_pid()
        if self._pid_is_service(pid):
            return pid
        discovered = self._discover_service_pid()
        if discovered:
            try:
                PID_FILE.write_text(str(discovered))
            except Exception:
                pass
        return discovered

    def _health(self):
        try:
            with urlopen("http://127.0.0.1:9192/health", timeout=2) as response:
                if response.status != 200:
                    return None
                payload = json.loads(response.read().decode("utf-8"))
                return payload if isinstance(payload, dict) else None
        except Exception:
            return None

    def _stop_pid(self, pid):
        if not self._pid_is_service(pid):
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if not self._pid_is_service(pid):
                return
            time.sleep(0.2)
        if self._pid_is_service(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if not self._pid_is_service(pid):
                    return
                time.sleep(0.05)
            raise RuntimeError("Service process did not exit; refusing replacement")

    def _stop_service_locked(self):
        pid = self._service_pid()
        if pid:
            self._stop_pid(pid)
        PID_FILE.unlink(missing_ok=True)

    def _stop_service(self, replacement=False, disable=False):
        epoch = self._request(stopped=None if replacement or disable else True, disable=disable)
        with self._operation_lock(timeout=15) as acquired:
            if not acquired:
                raise RuntimeError("Service stop timed out waiting for lifecycle ownership")
            control = self._control()
            if control.get("epoch") == epoch or (disable and not self._enabled_after_disable(control)):
                self._stop_service_locked()
                return True
            return False

    def _start_service(self):
        interpreter = self._interpreter()
        app_roots = _dispatcharr_app_roots()
        env = os.environ.copy()
        env["ARR_STACK_CONNECTOR_PLUGIN_KEY"] = PLUGIN_KEY
        env["ARR_STACK_CONNECTOR_PLUGIN_DIR"] = str(ROOT)
        env["ARR_STACK_CONNECTOR_STATE_DIR"] = str(STATE_DIR)
        env["ARR_STACK_CONNECTOR_SERVICE"] = "1"
        env["DISPATCHARR_SKIP_PLUGIN_AUTODISCOVERY"] = "1"
        env["ARR_STACK_CONNECTOR_RUNNING_VERSION"] = self._desired_version()
        env["PYTHONPATH"] = self._child_pythonpath()
        if app_roots:
            env["DISPATCHARR_APP_ROOT"] = str(app_roots[0])

        with BOOTSTRAP_LOG_FILE.open("ab", buffering=0) as bootstrap:
            process = subprocess.Popen(
                [interpreter, str(SERVICE_SCRIPT)],
                cwd=str(ROOT),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=bootstrap,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        PID_FILE.write_text(str(process.pid))
        return process.pid

    def _ensure_service(self, epoch=_UNSET):
        if epoch is _UNSET:
            epoch = self._control().get("epoch")
        if not self._may_start(epoch):
            return True  # Cancelled or disabled; no pending startup remains.
        with self._operation_lock() as acquired:
            if not acquired:
                return False
            return self._ensure_service_locked(epoch)

    def _ensure_service_locked(self, epoch):
        if not self._may_start(epoch):
            return True
        desired_version = self._desired_version()
        pid = self._service_pid()
        health = self._health() if pid else None
        running_version = str((health or {}).get("version") or "")
        if pid and health and running_version == desired_version:
            return True
        if pid:
            self._stop_pid(pid)
            PID_FILE.unlink(missing_ok=True)
        if not self._may_start(epoch):
            return True
        child = self._start_service()
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if not self._may_start(epoch):
                self._stop_pid(child)
                PID_FILE.unlink(missing_ok=True)
                return True
            health = self._health()
            if health and str(health.get("version") or "") == desired_version:
                return True
            time.sleep(0.25)
        # Don't strand a half-started child after the winning attempt fails.
        self._stop_pid(child)
        PID_FILE.unlink(missing_ok=True)
        raise RuntimeError("Detached Newznab/SAB service failed health check after startup")

    def run(self, action_id, params=None, context=None):
        if action_id == "status":
            health = self._health()
            return {
                "status": bool(health),
                "service": health or {},
                "pid": self._service_pid(),
                "installed_version": self._desired_version(),
                "dispatcharr_app_roots": [str(path) for path in _dispatcharr_app_roots()],
                "log_file": str(LOG_FILE),
                "bootstrap_log_file": str(BOOTSTRAP_LOG_FILE),
            }
        if action_id == "restart":
            epoch = self._request(stopped=False)
            with self._operation_lock(timeout=15) as acquired:
                if not acquired:
                    raise RuntimeError("Service restart timed out waiting for lifecycle ownership")
                if self._control().get("epoch") != epoch:
                    return {"status": False, "message": "Restart superseded by a newer lifecycle request"}
                self._stop_service_locked()
                self._ensure_service_locked(epoch)
            health = self._health()
            return {"status": bool(health), "message": "Service restart completed", "service": health or {}}
        if action_id == "stop":
            stopped = self._stop_service()
            return {"status": stopped, "message": "Service stopped" if stopped else "Stop superseded by a newer lifecycle request"}
        raise ValueError(f"Unknown action: {action_id}")

    def stop(self, context=None):
        reason = (context or {}).get("reason")
        self._stop_service(
            replacement=reason in {"reload", "shutdown"},
            disable=reason in {"disable", "delete", "capability_update_denied"},
        )
