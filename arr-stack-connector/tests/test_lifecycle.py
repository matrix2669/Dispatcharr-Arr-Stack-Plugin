"""Startup contention regression tests; no claims of lifecycle modernization."""
import fcntl
import importlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class StartupContentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {"ARR_STACK_CONNECTOR_STATE_DIR": self.tmp.name}):
            self.module = importlib.import_module("plugin")
        self.patches = [patch.object(self.module, name, Path(self.tmp.name) / value) for name, value in (
            ("STATE_DIR", "."), ("LOCK_FILE", "lock"), ("PID_FILE", "pid"))]
        for item in self.patches:
            item.start()
        self.instance = self.module.Plugin.__new__(self.module.Plugin)

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def test_same_pid_contention_returns_without_scheduling_future_start(self):
        with self.module.LOCK_FILE.open("a+") as owner, \
             patch.object(self.instance, "_start_service") as start:
            fcntl.flock(owner.fileno(), fcntl.LOCK_EX)
            before = time.monotonic()
            self.assertFalse(self.instance._ensure_service())
            self.assertLess(time.monotonic() - before, 0.2)
            fcntl.flock(owner.fileno(), fcntl.LOCK_UN)
            time.sleep(0.05)
            start.assert_not_called()

    def test_multiprocess_contention_returns_and_releases_for_later_start(self):
        script = "import fcntl,sys; f=open(sys.argv[1],'a+'); fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); sys.stdin.readline()"
        owner = subprocess.Popen([sys.executable, "-c", script, str(self.module.LOCK_FILE)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(owner.stdout.readline().strip(), "locked")
            before = time.monotonic()
            self.assertFalse(self.instance._ensure_service())
            self.assertLess(time.monotonic() - before, 0.2)
        finally:
            owner.communicate("release\n", timeout=5)
        with patch.object(self.instance, "_service_pid", return_value=123), \
             patch.object(self.instance, "_health", return_value={"version": "0.2.0"}):
            self.assertTrue(self.instance._ensure_service())

    def test_concurrent_initializers_launch_one_child(self):
        entered, release = threading.Event(), threading.Event()
        results = []
        def start():
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test barrier timed out")
            return 123
        with patch.object(self.instance, "_service_pid", return_value=None), \
             patch.object(self.instance, "_start_service", side_effect=start) as spawn, \
             patch.object(self.instance, "_health", return_value={"version": "0.2.0"}):
            owner = threading.Thread(target=lambda: results.append(self.instance._ensure_service()))
            owner.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertFalse(self.instance._ensure_service())
            finally:
                release.set()
                owner.join(3)
            self.assertFalse(owner.is_alive())
            self.assertEqual(results, [True])
            self.assertEqual(spawn.call_count, 1)

    def test_busy_restart_does_not_stop_winning_initializer(self):
        with self.module.LOCK_FILE.open("a+") as owner, \
             patch.object(self.instance, "_stop_service") as stop:
            fcntl.flock(owner.fileno(), fcntl.LOCK_EX)
            self.assertFalse(self.instance.run("restart")["status"])
            stop.assert_not_called()

    def test_restart_holds_startup_lock_before_stopping(self):
        def stop():
            with self.module.LOCK_FILE.open("a+") as other:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with patch.object(self.instance, "_stop_service", side_effect=stop), \
             patch.object(self.instance, "_service_pid", return_value=123), \
             patch.object(self.instance, "_health", return_value={"version": "0.2.0"}):
            self.assertTrue(self.instance.run("restart")["status"])

    def test_winning_constructor_failure_is_logged_then_restart_recovers(self):
        with patch.object(self.module.Plugin, "_service_pid", return_value=None), \
             patch.object(self.module.Plugin, "_start_service", side_effect=[RuntimeError("spawn failed"), 123]), \
             patch.object(self.module.Plugin, "_health", return_value={"version": "0.2.0"}), \
             patch.object(self.module.Plugin, "_stop_service") as stop:
            with self.assertLogs("apps.plugins.loader", level="ERROR") as logs:
                self.module.Plugin()
            self.assertIn("spawn failed", "\n".join(logs.output))
            self.assertTrue(self.instance.run("restart")["status"])
            stop.assert_called_once()

    def test_winning_timeout_releases_lock_and_subsequent_restart_recovers(self):
        with patch.object(self.instance, "_service_pid", return_value=None), \
             patch.object(self.instance, "_start_service", return_value=123) as start, \
             patch.object(self.instance, "_health", return_value=None), \
             patch.object(self.module.time, "time", side_effect=[0, 13]):
            with self.assertRaisesRegex(RuntimeError, "failed health check"):
                self.instance._ensure_service()
            start.assert_called_once()
        with patch.object(self.instance, "_stop_service") as stop, \
             patch.object(self.instance, "_service_pid", return_value=None), \
             patch.object(self.instance, "_start_service", return_value=124), \
             patch.object(self.instance, "_health", return_value={"version": "0.2.0"}):
            self.assertTrue(self.instance.run("restart")["status"])
            stop.assert_called_once()

    def test_non_contention_lock_errors_propagate(self):
        with patch.object(self.module.fcntl, "flock", side_effect=OSError(5, "I/O failure")):
            with self.assertRaises(OSError):
                self.instance._ensure_service()

    def test_healthy_service_is_reused(self):
        with patch.object(self.instance, "_service_pid", return_value=123), \
             patch.object(self.instance, "_health", return_value={"version": "0.2.0"}), \
             patch.object(self.instance, "_start_service") as start:
            self.assertTrue(self.instance._ensure_service())
            start.assert_not_called()

    def test_stop_reasons_preserve_baseline_behavior(self):
        with patch.object(self.instance, "_stop_service") as stop:
            for reason in ("disable", "delete", "reload", "shutdown"):
                self.instance.stop({"reason": reason})
            self.assertEqual(stop.call_count, 4)
