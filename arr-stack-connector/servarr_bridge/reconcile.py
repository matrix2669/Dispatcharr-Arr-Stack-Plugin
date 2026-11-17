"""Single-process committed-state HTTP reconciliation, independent of loaders."""
import logging
import os
from pathlib import Path
import secrets
import threading
import time

from . import lifecycle

log = logging.getLogger("arr_stack_connector.reconcile")


class StopTimeout(RuntimeError):
    """The owner must exit rather than release its lease beside a live listener."""


class Web:
    def __init__(self, server):
        self.server = server
        self.server.lifecycle_heartbeat = time.monotonic()
        self.thread = threading.Thread(target=self._run, daemon=True, name="arr-stack-http")
        self.thread.start()

    def _run(self):
        try:
            self.server.serve_forever(poll_interval=0.2)
        except Exception:
            log.exception("HTTP listener failed; owner will recover automatically")

    def alive(self):
        return self.thread.is_alive() and time.monotonic() - self.server.lifecycle_heartbeat < 5

    def stop(self):
        if self.thread.is_alive():
            # BaseServer.shutdown may block if its serve loop is wedged. Never
            # release ownership while such a listener remains in this process.
            shutdown = threading.Thread(target=self.server.shutdown, daemon=True)
            shutdown.start()
            shutdown.join(3)
            self.thread.join(1)
            if shutdown.is_alive() or self.thread.is_alive():
                raise StopTimeout("HTTP shutdown timed out; owner must exit")
        self.server.server_close()


class Controller:
    def __init__(self, root, state, lease_fd, read_desired, server_factory,
                 clock=time.monotonic, web_factory=Web, replace=None):
        self.root, self.state = Path(root), Path(state)
        self.lease_fd = lease_fd
        self.read_desired = read_desired
        self.server_factory = server_factory
        self.clock, self.web_factory = clock, web_factory
        self.replace = replace or self._replace
        self.boot_source = os.environ.get("ARR_STACK_CONNECTOR_SOURCE_GENERATION") or lifecycle.source_generation(self.root)
        self.boot_restart = lifecycle.read_json(lifecycle.control_path(self.state)).get("restart")
        self.owner = os.environ.get("ARR_STACK_CONNECTOR_OWNER") or secrets.token_hex(16)
        os.environ["ARR_STACK_CONNECTOR_OWNER"] = self.owner
        self.version = os.environ.get("ARR_STACK_CONNECTOR_RUNNING_VERSION", "unknown")
        self.web = None
        self.bind = None
        self.retry_at = 0
        self.failures = 0
        self.last_status = None
        self.last_write = 0
        (self.state / ".servarr_service.pid").write_text(str(os.getpid()))

    def stop_web(self):
        if self.web is not None:
            self.web.stop()
            self.web = None
            self.bind = None

    def emit(self, phase, control=None, error=""):
        status = {
            "phase": phase, "pid": os.getpid(), "process_start": lifecycle.process_start(os.getpid()),
            "owner": self.owner, "version": self.version, "source": self.boot_source,
            "command": (control or {}).get("command"),
            "restart": self.boot_restart,
            "listening": bool(self.web and self.web.alive()),
            "port": self.bind[1] if self.bind else None,
            "host": self.bind[0] if self.bind else None,
            "error": error,
        }
        now = self.clock()
        if status != self.last_status or now - self.last_write >= 5:
            lifecycle.atomic_json(lifecycle.status_path(self.state), dict(status, updated=time.time()))
            self.last_status, self.last_write = status, now

    def _replace(self):
        # step returns False and the process exits with its lease still held.
        # Watchdogs can acquire only after kernel teardown closes every listener
        # and descriptor. No exec handoff or orphan child can split ownership.
        log.info("Owner generation retiring; watchdog will launch current source")

    def step(self):
        control = {}
        try:
            source = lifecycle.source_generation(self.root)
            if source is None:
                self.stop_web()
                self.emit("removed")
                return False
            control = lifecycle.read_json(lifecycle.control_path(self.state))
            if source != self.boot_source or control.get("restart") != self.boot_restart:
                self.stop_web()
                self.emit("restarting", control)
                self.replace()
                return False
            if control.get("manual_stop"):
                self.stop_web()
                self.failures = 0
                self.retry_at = 0
                self.emit("stopped", control)
                return True
            if self.web is None and self.clock() < self.retry_at:
                self.emit("retrying", control)
                return True
            enabled, bind = self.read_desired()
            if not enabled:
                self.stop_web()
                self.failures = 0
                self.retry_at = 0
                self.emit("disabled", control)
                return True
            if self.web and (not self.web.alive() or bind != self.bind):
                self.stop_web()
            if self.web is None:
                if self.clock() < self.retry_at:
                    self.emit("retrying", control)
                    return True
                self.web = self.web_factory(self.server_factory())
                self.bind = bind
                # Slow bind/key initialization may overlap a new command or DB
                # commit. Re-read authoritative state rather than trust our plan.
                latest = lifecycle.read_json(lifecycle.control_path(self.state))
                current_enabled, current_bind = self.read_desired()
                if (latest != control or not current_enabled or current_bind != bind or
                        lifecycle.source_generation(self.root) != source):
                    self.stop_web()
                    self.emit("reconciling", latest)
                    return True
                if not self.web.alive():
                    raise RuntimeError("HTTP thread exited during startup")
            self.failures = 0
            self.retry_at = 0
            self.emit("running", control)
            return True
        except StopTimeout:
            raise
        except Exception as exc:
            # Database failures and malformed controls fail closed. Retrying
            # cannot silently continue serving stale desired state indefinitely.
            self.stop_web()
            self.failures += 1
            self.retry_at = self.clock() + min(15, 2 ** min(self.failures - 1, 4))
            log.exception("Service reconciliation failed; automatic recovery remains active")
            self.emit("error", control, type(exc).__name__)
            return True
