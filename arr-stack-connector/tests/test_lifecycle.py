"""Portable concurrency regressions: real flock, no Dispatcharr/HTTP required."""
import importlib
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {"ARR_STACK_CONNECTOR_STATE_DIR": self.tmp.name}):
            self.module = importlib.import_module("plugin")
        self.patches = [patch.object(self.module, name, Path(self.tmp.name) / value) for name, value in (
            ("STATE_DIR", "."), ("LOCK_FILE", "lock"), ("CONTROL_FILE", "control.json"), ("PID_FILE", "pid"))]
        for item in self.patches:
            item.start()
        self.cfg = patch.object(self.module.Plugin, "_configuration", return_value=(True, "revision-1"))
        self.cfg.start()
        self.instance = self.module.Plugin.__new__(self.module.Plugin)

    def tearDown(self):
        self.cfg.stop()
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def test_same_process_lock_contention_never_blocks_discovery(self):
        with self.instance._operation_lock() as acquired:
            self.assertTrue(acquired)
            started = time.monotonic()
            self.assertFalse(self.instance._ensure_service())
            self.assertLess(time.monotonic() - started, 0.2)

    def test_busy_constructor_queues_deferred_attempt_without_waiting(self):
        with self.instance._operation_lock(), patch.object(self.module.Plugin, "_queue_retry") as retry:
            started = time.monotonic()
            self.module.Plugin()
            self.assertLess(time.monotonic() - started, 0.2)
            retry.assert_called_once_with(None)

    def test_real_deferred_attempt_starts_after_contention_clears(self):
        done = threading.Event()
        with patch.object(self.module.Plugin, "_ensure_service_locked", side_effect=lambda epoch: done.set() or True):
            with self.instance._operation_lock():
                self.instance._queue_retry(None)
                self.assertFalse(done.wait(0.05))
            self.assertTrue(done.wait(2))
            state = sys.modules["_arr_stack_start_retries"]
            state.jobs[(os.getpid(), str(self.module.STATE_DIR))][1].join(2)

    def test_stop_fences_a_queued_start_and_requires_new_config_or_restart(self):
        epoch = self.instance._request(True)
        self.assertFalse(self.instance._may_start(None))
        self.assertFalse(self.instance._may_start(epoch))
        with patch.object(self.module.Plugin, "_configuration", return_value=(False, "revision-2")):
            self.assertFalse(self.instance._may_start(epoch))
        with patch.object(self.module.Plugin, "_configuration", return_value=(True, "revision-3")):
            self.assertTrue(self.instance._may_start(epoch))
        new_epoch = self.instance._request(False)
        self.assertTrue(self.instance._may_start(new_epoch))
        self.assertFalse(self.instance._may_start(epoch))

    def test_explicit_none_epoch_is_not_rebased_to_new_request(self):
        self.instance._request(False)
        with patch.object(self.instance, "_start_service") as start:
            self.assertTrue(self.instance._ensure_service(None))
            start.assert_not_called()

    def test_start_in_flight_is_cancelled_and_child_cleaned_before_unlock(self):
        with patch.object(self.instance, "_service_pid", return_value=None), \
             patch.object(self.instance, "_start_service", side_effect=lambda: self.instance._request(True) and 123), \
             patch.object(self.instance, "_stop_pid") as stop:
            self.assertTrue(self.instance._ensure_service(None))
            stop.assert_called_once_with(123)

    def test_restart_holds_one_lock_across_stop_and_start(self):
        def ensure(epoch):
            with self.instance._operation_lock() as acquired:
                self.assertFalse(acquired)
            return True
        with patch.object(self.instance, "_stop_service_locked") as stop, \
             patch.object(self.instance, "_ensure_service_locked", side_effect=ensure), \
             patch.object(self.instance, "_health", return_value={"version": "0.2.0"}):
            self.assertTrue(self.instance.run("restart")["status"])
            stop.assert_called_once()

    def test_reload_fences_old_retries_but_allows_new_discovery(self):
        with patch.object(self.instance, "_stop_service_locked"):
            self.instance.stop({"reason": "reload"})
        self.assertFalse(self.instance._may_start(None))
        self.assertTrue(self.instance._may_start(self.instance._control()["epoch"]))

    def test_stop_is_persistent_and_idempotent(self):
        with patch.object(self.instance, "_stop_service_locked") as stop:
            self.instance.stop()
            self.instance.stop()
            self.assertEqual(stop.call_count, 2)
        self.assertFalse(self.instance._may_start(self.instance._control()["epoch"]))

    def test_disabled_configuration_never_starts_service(self):
        with patch.object(self.instance, "_configuration", return_value=(False, "r")), \
             patch.object(self.instance, "_start_service") as start:
            self.assertTrue(self.instance._ensure_service())
            start.assert_not_called()

    def test_non_contention_lock_error_is_not_hidden(self):
        with patch.object(self.module.fcntl, "flock", side_effect=OSError(5, "I/O failure")):
            with self.assertRaises(OSError):
                with self.instance._operation_lock():
                    pass

    def test_reload_does_not_overwrite_newer_stop_request(self):
        def concurrent_stop():
            self.instance._request(stopped=True)
        with patch.object(self.instance, "_stop_service_locked", side_effect=concurrent_stop):
            self.instance.stop({"reason": "reload"})
        control = self.instance._control()
        self.assertTrue(control["stopped"])
        self.assertFalse(self.instance._may_start(control["epoch"]))

    def test_shutdown_fences_old_retry_without_blocking_next_boot(self):
        with patch.object(self.instance, "_stop_service_locked"):
            self.instance.stop({"reason": "shutdown"})
        self.assertFalse(self.instance._may_start(None))
        self.assertTrue(self.instance._may_start(self.instance._control()["epoch"]))

    def test_lock_timeout_is_bounded(self):
        with self.instance._operation_lock():
            started = time.monotonic()
            with self.instance._operation_lock(timeout=0.05) as acquired:
                self.assertFalse(acquired)
            self.assertLess(time.monotonic() - started, 0.3)

    def test_malformed_control_fails_closed(self):
        self.module.CONTROL_FILE.write_text("{broken")
        with patch.object(self.instance, "_start_service") as start:
            with self.assertRaises(ValueError):
                self.instance._ensure_service()
            start.assert_not_called()

    def test_failed_process_exit_refuses_replacement(self):
        with patch.object(self.instance, '_pid_is_service', return_value=True), \
             patch.object(self.module.os, 'kill') as kill, \
             patch.object(self.module.time, 'monotonic', side_effect=[0, 9, 10, 13]):
            with self.assertRaisesRegex(RuntimeError, 'refusing replacement'):
                self.instance._stop_pid(123)
            self.assertEqual(kill.call_count, 2)

    def test_stop_failure_does_not_launch_second_service(self):
        with patch.object(self.instance, '_service_pid', return_value=123), \
             patch.object(self.instance, '_health', return_value=None), \
             patch.object(self.instance, '_stop_pid', side_effect=RuntimeError('still running')), \
             patch.object(self.instance, '_start_service') as start:
            with self.assertRaisesRegex(RuntimeError, 'still running'):
                self.instance._ensure_service()
            start.assert_not_called()
