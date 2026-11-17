"""Deterministic reconciliation, ownership and command regressions."""
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from servarr_bridge import lifecycle
from servarr_bridge.reconcile import Controller, StopTimeout, Web


class FakeWeb:
    def __init__(self, server):
        self.server = server
        self.up = True
        self.stops = 0
    def alive(self):
        return self.up
    def stop(self):
        self.up = False
        self.stops += 1


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "plugin"
        self.state = Path(self.tmp.name) / "state"
        self.root.mkdir(); self.state.mkdir()
        (self.root / "servarr_bridge").mkdir()
        for name in ("plugin.py", "service.py", "plugin.json"):
            (self.root / name).write_text("baseline")
        self.enabled = True
        self.bind = ("0.0.0.0", 9192)
        self.now = 100
        self.factory = Mock(return_value=object())
        self.replace = Mock()
        self.controller = Controller(self.root, self.state, -1,
                                     lambda: (self.enabled, self.bind), self.factory,
                                     clock=lambda: self.now, web_factory=FakeWeb, replace=self.replace)
    def tearDown(self):
        self.tmp.cleanup()
    def status(self):
        return lifecycle.read_json(lifecycle.status_path(self.state))
    def tick(self):
        self.now += 1
        return self.controller.step()

    def test_startup_and_healthy_reuse(self):
        self.tick(); self.tick()
        self.assertEqual(self.factory.call_count, 1)
        self.assertEqual(self.status()["phase"], "running")

    def test_committed_disable_and_enable_need_no_new_initializer(self):
        self.tick(); original = self.controller.web
        self.enabled = False; self.tick()
        self.assertFalse(original.alive())
        self.assertEqual(self.status()["phase"], "disabled")
        self.enabled = True; self.tick()
        self.assertTrue(self.controller.web.alive())
        self.assertEqual(self.factory.call_count, 2)

    def test_rollback_or_rapid_reenable_keeps_latest_committed_state(self):
        self.tick(); original = self.controller.web
        # No intermediate false value is required or inferred from timestamps.
        self.enabled = False; self.enabled = True; self.tick()
        self.assertIs(self.controller.web, original)
        self.assertEqual(original.stops, 0)

    def test_missing_row_stops_and_recreated_enabled_row_starts(self):
        self.tick(); self.enabled = False; self.tick()
        self.assertIsNone(self.controller.web)
        self.enabled = True; self.tick()
        self.assertIsNotNone(self.controller.web)

    def test_manual_stop_persists_without_clearing_on_enable_changes(self):
        self.tick()
        lifecycle.request(self.state, "stop"); self.tick()
        self.assertEqual(self.status()["phase"], "stopped")
        self.enabled = False; self.tick(); self.enabled = True; self.tick()
        self.assertIsNone(self.controller.web)
        self.assertTrue(lifecycle.read_json(lifecycle.control_path(self.state))["manual_stop"])

    def test_manual_stop_survives_new_owner_instance(self):
        lifecycle.request(self.state, "stop")
        fresh = Controller(self.root, self.state, -1, lambda: (True, self.bind), self.factory,
                           web_factory=FakeWeb, replace=self.replace)
        self.assertTrue(fresh.step())
        self.assertEqual(self.status()["phase"], "stopped")
        self.factory.assert_not_called()

    def test_restart_is_generation_change_not_ignored_stale_action(self):
        self.tick(); old = self.controller.web
        token = lifecycle.request(self.state, "restart")
        self.assertFalse(self.tick())
        self.assertFalse(old.alive())
        self.replace.assert_called_once()
        self.assertEqual(self.status()["command"], token)

    def test_restart_boot_does_not_reexec_infinite_loop(self):
        lifecycle.request(self.state, "restart")
        fresh = Controller(self.root, self.state, -1, lambda: (True, self.bind), self.factory,
                           web_factory=FakeWeb, replace=self.replace)
        self.assertTrue(fresh.step())
        self.replace.assert_not_called()

    def test_disabled_commit_during_slow_start_is_rechecked(self):
        def slow():
            self.enabled = False
            return object()
        self.factory.side_effect = slow
        self.tick()
        self.assertIsNone(self.controller.web)
        self.assertNotEqual(self.status()["phase"], "running")

    def test_manual_stop_during_slow_start_is_rechecked(self):
        def slow():
            lifecycle.request(self.state, "stop")
            return object()
        self.factory.side_effect = slow
        self.tick()
        self.assertIsNone(self.controller.web)
        self.tick()
        self.assertEqual(self.status()["phase"], "stopped")

    def test_failed_start_retries_without_constructor_or_user_action(self):
        self.factory.side_effect = [OSError("bind failed"), object()]
        with self.assertLogs("arr_stack_connector.reconcile", level="ERROR"):
            self.tick()
        self.assertEqual(self.status()["phase"], "error")
        self.tick()
        self.assertEqual(self.status()["phase"], "running")

    def test_dead_http_thread_is_replaced_automatically(self):
        self.tick(); old = self.controller.web; old.up = False
        self.tick()
        self.assertIsNot(self.controller.web, old)
        self.assertEqual(self.factory.call_count, 2)

    def test_database_failure_fails_closed_then_recovers(self):
        self.tick(); old = self.controller.web
        self.controller.read_desired = Mock(side_effect=RuntimeError("DB unavailable"))
        with self.assertLogs("arr_stack_connector.reconcile", level="ERROR"):
            self.tick()
        self.assertFalse(old.alive())
        self.controller.read_desired = lambda: (True, self.bind)
        self.tick()
        self.assertEqual(self.status()["phase"], "running")

    def test_manual_stop_does_not_need_working_database(self):
        lifecycle.request(self.state, "stop")
        self.controller.read_desired = Mock(side_effect=RuntimeError("DB unavailable"))
        self.tick()
        self.assertEqual(self.status()["phase"], "stopped")
        self.controller.read_desired.assert_not_called()

    def test_bind_setting_change_replaces_listener(self):
        self.tick(); old = self.controller.web
        self.bind = ("127.0.0.1", 9292); self.tick()
        self.assertFalse(old.alive())
        self.assertEqual(self.status()["port"], 9292)

    def test_source_change_requires_owner_exec_with_no_listener(self):
        self.tick(); old = self.controller.web
        (self.root / "service.py").write_text("changed")
        self.assertFalse(self.tick())
        self.assertFalse(old.alive())
        self.replace.assert_called_once()

    def test_spawn_snapshot_prevents_mixed_source_startup(self):
        old = lifecycle.source_generation(self.root)
        (self.root / "service.py").write_text("updated while child loaded")
        with patch.dict(os.environ, {"ARR_STACK_CONNECTOR_SOURCE_GENERATION": old}):
            fresh = Controller(self.root, self.state, -1, lambda: (True, self.bind), self.factory,
                               web_factory=FakeWeb, replace=self.replace)
        self.assertFalse(fresh.step())
        self.replace.assert_called_once()
        self.factory.assert_not_called()

    def test_stalled_http_loop_is_not_reported_alive(self):
        web = Web.__new__(Web)
        web.thread = Mock()
        web.thread.is_alive.return_value = True
        web.server = Mock()
        web.server.lifecycle_heartbeat = 1
        with patch("servarr_bridge.reconcile.time.monotonic", return_value=10):
            self.assertFalse(web.alive())

    def test_source_removal_stops_owner(self):
        self.tick(); old = self.controller.web
        (self.root / "service.py").unlink()
        self.assertFalse(self.tick())
        self.assertFalse(old.alive())
        self.assertEqual(self.status()["phase"], "removed")

    def test_wedged_listener_is_fatal_not_duplicate_takeover(self):
        self.tick()
        self.controller.web.stop = Mock(side_effect=StopTimeout("wedged"))
        self.enabled = False
        with self.assertRaises(StopTimeout):
            self.tick()
        self.assertEqual(self.factory.call_count, 1)

    def test_malformed_control_fails_closed(self):
        self.tick(); old = self.controller.web
        lifecycle.control_path(self.state).write_text("broken")
        with self.assertLogs("arr_stack_connector.reconcile", level="ERROR"):
            self.tick()
        self.assertFalse(old.alive())


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()

    def test_same_process_lease_contention_is_nonblocking(self):
        a = os.open(lifecycle.lease_path(self.state), os.O_RDWR | os.O_CREAT, 0o600)
        b = os.open(lifecycle.lease_path(self.state), os.O_RDWR)
        try:
            fcntl.flock(a, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                fcntl.flock(b, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(a); os.close(b)

    def test_parent_close_not_unlock_preserves_inherited_lease(self):
        a = os.open(lifecycle.lease_path(self.state), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(a, fcntl.LOCK_EX | fcntl.LOCK_NB)
        inherited = os.dup(a); os.close(a)
        b = os.open(lifecycle.lease_path(self.state), os.O_RDWR)
        try:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(b, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.close(inherited)
            fcntl.flock(b, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(b)

    def test_commands_are_serialized_and_last_record_is_coherent(self):
        results = {}
        def write(action):
            token = lifecycle.request(self.state, action)
            results[token] = action
        workers = [threading.Thread(target=write, args=(a,)) for a in ("stop", "restart") * 8]
        for worker in workers: worker.start()
        for worker in workers: worker.join(3)
        record = lifecycle.read_json(lifecycle.control_path(self.state))
        self.assertEqual(len(results), 16)
        self.assertEqual(record["manual_stop"], results[record["command"]] == "stop")

    def test_watchdog_deduplicates_module_aliases_and_constructor_calls(self):
        registry = lifecycle._registry()
        real = lifecycle.native
        with patch.object(lifecycle, "native", wraps=real) as native:
            starter = Mock(return_value=123)
            native.side_effect = lambda mod, name: starter if (mod, name) == ("_thread", "start_new_thread") else real(mod, name)
            first = lifecycle.ensure_watchdog(self.state, self.state, sys.executable, {})
            second = lifecycle.ensure_watchdog(self.state, self.state, sys.executable, {})
            self.assertIs(first, second)
            starter.assert_called_once()
            first["alive"] = False
        registry.jobs.pop((str(self.state), str(self.state)), None)

    @unittest.skipUnless(hasattr(os, "fork"), "requires fork")
    def test_fork_child_resets_registry_and_drops_transient_parent_lease(self):
        registry = lifecycle._registry()
        fd = os.open(lifecycle.lease_path(self.state), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with registry.lock:
            registry.leases.add(fd)
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            try:
                os.close(r)
                child = lifecycle._registry()
                try:
                    os.fstat(fd)
                    closed = False
                except OSError:
                    closed = True
                os.write(w, json.dumps([child.pid == os.getpid(), not child.jobs, closed]).encode())
            finally:
                os._exit(0)
        os.close(w)
        try:
            self.assertEqual(json.loads(os.read(r, 1000)), [True, True, True])
            os.waitpid(pid, 0)
        finally:
            os.close(r)
            with registry.lock:
                registry.leases.discard(fd)
                os.close(fd)

    @unittest.skipUnless(hasattr(os, "POSIX_SPAWN_CLOSEFROM"), "requires supported Linux spawn primitives")
    def test_dead_owner_recovers_without_any_new_initializer(self):
        import signal
        root = self.state / "plugin"
        root.mkdir()
        (root / "plugin.py").write_text("")
        (root / "plugin.json").write_text("{}")
        marker = self.state / "starts"
        (root / "service.py").write_text(
            "import os,time\n"
            "with open(os.environ['TEST_STARTS'],'a') as out: out.write(str(os.getpid())+'\\n')\n"
            "while True: time.sleep(1)\n")
        env = dict(os.environ, TEST_STARTS=str(marker))
        job = lifecycle.ensure_watchdog(root, self.state, sys.executable, env)
        def starts():
            return marker.read_text().splitlines() if marker.exists() else []
        def wait_for(count):
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if len(starts()) >= count:
                    return
                time.sleep(0.05)
            self.fail("watchdog did not recover owner")
        try:
            wait_for(1)
            pid = int(starts()[0])
            fd = os.pidfd_open(pid)
            try:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            finally:
                os.close(fd)
            wait_for(2)
            self.assertEqual(len(starts()), 2)
            self.assertNotEqual(starts()[0], starts()[1])
        finally:
            job["stop"] = True
            for value in starts():
                try:
                    fd = os.pidfd_open(int(value))
                    try:
                        signal.pidfd_send_signal(fd, signal.SIGKILL)
                    finally:
                        os.close(fd)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + 3
            while job["alive"] and time.monotonic() < deadline:
                time.sleep(0.05)
            for value in starts():
                try:
                    os.waitpid(int(value), 0)
                except ChildProcessError:
                    pass
            self.assertFalse(job["alive"])

    @unittest.skipUnless(hasattr(os, "pidfd_open"), "requires Linux pidfd")
    def test_legacy_retirement_does_not_signal_unrelated_process(self):
        import subprocess
        root = self.state / "plugin"
        root.mkdir()
        script = root / "service.py"
        script.write_text("import time\nwhile True: time.sleep(1)\n")
        unrelated = self.state / "other.py"
        unrelated.write_text(script.read_text())
        old = subprocess.Popen([sys.executable, str(script)])
        other = subprocess.Popen([sys.executable, str(unrelated)])
        try:
            with lifecycle.lease_path(self.state).open("a+") as lease:
                fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                lifecycle.retire_legacy(root)
            old.wait(timeout=3)
            self.assertIsNone(other.poll())
        finally:
            if old.poll() is None:
                old.terminate()
            other.terminate()
            old.wait(timeout=3); other.wait(timeout=3)

    @unittest.skipUnless(hasattr(os, "POSIX_SPAWN_CLOSEFROM"), "requires supported Linux spawn primitives")
    def test_startup_timeout_recovers_without_another_constructor(self):
        import signal
        root = self.state / "plugin"
        root.mkdir()
        (root / "plugin.py").write_text("")
        (root / "plugin.json").write_text("{}")
        marker = self.state / "timeout-starts"
        (root / "service.py").write_text(
            "import os,time,importlib.util\n"
            "p=os.environ['TEST_STARTS']\n"
            "first=not os.path.exists(p)\n"
            "with open(p,'a') as out: out.write(str(os.getpid())+'\\n')\n"
            "if first:\n"
            " spec=importlib.util.spec_from_file_location('real_service',os.environ['TEST_SERVICE'])\n"
            " module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)\n"
            " module.startup_timeout(0.5)\n"
            "while True: time.sleep(1)\n")
        env = dict(os.environ, TEST_STARTS=str(marker), TEST_SERVICE=str(ROOT / "service.py"),
                   ARR_STACK_CONNECTOR_STATE_DIR=str(self.state))
        job = lifecycle.ensure_watchdog(root, self.state, sys.executable, env)
        def starts():
            return marker.read_text().splitlines() if marker.exists() else []
        try:
            deadline = time.monotonic() + 8
            while len(starts()) < 2 and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(len(starts()), 2)
            self.assertIsNotNone(lifecycle.process_start(int(starts()[1])))
            self.assertIn("initialization deadline armed", (self.state / "servarr_service_bootstrap.log").read_text())
        finally:
            job["stop"] = True
            for value in starts():
                try:
                    fd = os.pidfd_open(int(value))
                    try: signal.pidfd_send_signal(fd, signal.SIGKILL)
                    finally: os.close(fd)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + 3
            while job["alive"] and time.monotonic() < deadline:
                time.sleep(0.05)
            for value in starts():
                try: os.waitpid(int(value), 0)
                except ChildProcessError: pass
            self.assertFalse(job["alive"])

    @unittest.skipUnless(hasattr(os, "POSIX_SPAWN_CLOSEFROM"), "requires supported Linux spawn primitives")
    def test_spawn_does_not_leak_unrelated_inheritable_descriptors(self):
        root = self.state / "plugin"
        root.mkdir()
        (root / "plugin.py").write_text("")
        (root / "plugin.json").write_text("{}")
        marker = self.state / "leaked"
        (root / "service.py").write_text(
            "import os,pathlib\n"
            "leaked=False\n"
            "for p in pathlib.Path('/proc/self/fd').iterdir():\n"
            " try:\n"
            "  st=os.fstat(int(p.name))\n"
            "  leaked |= (st.st_dev,st.st_ino)==(int(os.environ['TEST_DEV']),int(os.environ['TEST_INO']))\n"
            " except OSError: pass\n"
            "pathlib.Path(os.environ['TEST_MARKER']).write_text(str(leaked))\n")
        unrelated = os.open(self.state / "unrelated", os.O_RDWR | os.O_CREAT, 0o600)
        lease = os.open(lifecycle.lease_path(self.state), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.set_inheritable(unrelated, True)
            st = os.fstat(unrelated)
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            env = dict(os.environ, TEST_DEV=str(st.st_dev), TEST_INO=str(st.st_ino), TEST_MARKER=str(marker))
            pid = lifecycle._spawn(root, self.state, sys.executable, env, lease)
            os.waitpid(pid, 0)
            self.assertEqual(marker.read_text(), "False")
        finally:
            os.close(lease); os.close(unrelated)
