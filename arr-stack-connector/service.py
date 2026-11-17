"""Lease-owning reconciler and HTTP listener in one recoverable process."""
import json
import logging
import os
import signal
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(os.environ.get("ARR_STACK_CONNECTOR_PLUGIN_DIR") or Path(__file__).resolve().parent)
STATE_DIR = Path(os.environ.get("ARR_STACK_CONNECTOR_STATE_DIR") or "/data/arr_stack_connector")
STATE_DIR.mkdir(parents=True, exist_ok=True)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def configure_logging():
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    handler = RotatingFileHandler(STATE_DIR / "servarr_service.log", maxBytes=5 * 1024 * 1024, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.captureWarnings(True)
    logging.getLogger("django.db.backends").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def startup_timeout(seconds=30):
    """Bound owner initialization; watchdog retries after kernel closes lease."""
    previous = signal.getsignal(signal.SIGALRM)
    # Default kernel action also terminates C-extension/GIL stalls; a Python
    # signal callback alone would not give that guarantee.
    os.write(2, b"Arr Stack owner initialization deadline armed; watchdog recovers failure.\n")
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    def cancel():
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    return cancel


def main():
    from servarr_bridge import lifecycle
    try:
        lease_fd = lifecycle.acquire_owner(STATE_DIR)
    except BlockingIOError:
        return 0
    # The fd intentionally remains open until process exit, never before
    # all HTTP threads die. It is not inherited by unrelated execs/fork children.
    cancel_startup_timeout = startup_timeout()
    configure_logging()
    logger = logging.getLogger("arr_stack_connector.service")
    os.environ.update(ARR_STACK_CONNECTOR_SERVICE="1", DISPATCHARR_SKIP_PLUGIN_AUTODISCOVERY="1",
                      ARR_STACK_CONNECTOR_STATE_DIR=str(STATE_DIR), ARR_STACK_CONNECTOR_PLUGIN_DIR=str(ROOT))
    os.environ.setdefault("ARR_STACK_CONNECTOR_PLUGIN_KEY", ROOT.name.replace(" ", "_").lower())
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dispatcharr.settings")
    try:
        lifecycle.retire_legacy(ROOT)
        manifest = json.loads((ROOT / "plugin.json").read_text())
        os.environ["ARR_STACK_CONNECTOR_RUNNING_VERSION"] = str(manifest.get("version") or "unknown")
        import django
        from django.conf import settings
        database = settings.DATABASES.get("default", {})
        if "postgresql" in database.get("ENGINE", ""):
            database.setdefault("OPTIONS", {}).setdefault("connect_timeout", 5)
        django.setup()
        configure_logging()
        from django.db import connection
        from apps.plugins.models import PluginConfig
        from servarr_bridge.server import create_server
        from servarr_bridge.reconcile import Controller

        def desired():
            try:
                if not connection.get_autocommit():
                    raise RuntimeError("Reconciler requires an independent autocommit connection")
                if connection.vendor == "postgresql":
                    with connection.cursor() as cursor:
                        cursor.execute("SET statement_timeout = '2000ms'")
                cfg = PluginConfig.objects.filter(key=os.environ["ARR_STACK_CONNECTOR_PLUGIN_KEY"]).values("enabled", "settings").first()
                if not cfg or not cfg["enabled"]:
                    return False, None
                values = cfg["settings"] or {}
                return True, (str(values.get("listen_host") or "0.0.0.0"), int(values.get("listen_port") or 9192))
            except Exception:
                connection.close()
                raise

        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        controller = Controller(ROOT, STATE_DIR, lease_fd, desired, create_server)
        cancel_startup_timeout()
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        logger.info("Committed-state reconciler started pid=%s", os.getpid())
        while not stop.is_set():
            # Bound even a stalled driver/filesystem operation. A dead owner
            # closes listener+lease together; host watchdogs retry automatically.
            signal.setitimer(signal.ITIMER_REAL, 15)
            keep_running = controller.step()
            signal.setitimer(signal.ITIMER_REAL, 0)
            if not keep_running:
                break
            stop.wait(0.5)
        signal.setitimer(signal.ITIMER_REAL, 5)
        controller.stop_web()
        controller.emit("exiting")
        return 0
    except BaseException as exc:
        logger.exception("Service owner exited; host watchdog will recover automatically")
        lifecycle.atomic_json(lifecycle.status_path(STATE_DIR), {
            "phase": "error", "error": type(exc).__name__, "pid": os.getpid(),
            "process_start": lifecycle.process_start(os.getpid()), "listening": False,
            "retry_after": __import__("time").time() + 3,
        })
        return 1


if __name__ == "__main__":
    code = main()
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_REAL, 5)
    logging.shutdown()
    # Kill any wedged/daemon HTTP or third-party startup threads together with
    # the owner. Kernel closes listener and lease as one process teardown.
    os._exit(code)
