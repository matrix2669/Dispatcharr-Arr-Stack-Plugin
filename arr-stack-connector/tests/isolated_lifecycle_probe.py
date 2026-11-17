"""Opt-in real PostgreSQL/host-watchdog validation in disposable copies only."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen


def main():
    if os.environ.get("ARR_STACK_ISOLATED_TEST") != "1":
        raise RuntimeError("Requires explicit disposable-test opt in")
    if set(name for _, name in socket.if_nameindex()) != {"lo"}:
        raise RuntimeError("Requires network-none container")
    if b"sleep\x00infinity" not in Path("/proc/1/cmdline").read_bytes():
        raise RuntimeError("Requires disposable diagnostic container entrypoint")
    root = Path("/data/plugins/arr_stack_connector")
    state_dir = Path("/data/arr_stack_connector")
    sys.path.insert(0, str(root))
    from servarr_bridge import lifecycle

    def state():
        return lifecycle.owner_status(state_dir)
    def health():
        try:
            with urlopen("http://127.0.0.1:9192/health", timeout=0.5) as response:
                value = json.load(response)
            return value if value.get("owner") == state().get("owner") else None
        except Exception:
            return None
    def wait_for(predicate, message, timeout=35):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.1)
        raise AssertionError(message + "; phase=" + str(state().get("phase")))
    def services():
        target = str(root / "service.py").encode()
        result = []
        for path in Path("/proc").iterdir():
            if path.name.isdigit():
                try:
                    if target in (path / "cmdline").read_bytes().split(b"\0"):
                        result.append(int(path.name))
                except OSError:
                    pass
        return result
    def off(phase="disabled"):
        return not health() and state().get("phase") == phase

    wait_for(health, "initial owner unavailable")
    # IMPORTANT: no Django initialization or Plugin construction in this process
    # yet. Recovery here proves actual uWSGI/Celery/host watchdog availability.
    previous = state()
    fd = os.pidfd_open(previous["pid"])
    try:
        assert lifecycle.process_start(previous["pid"]) == previous["process_start"]
        signal.pidfd_send_signal(fd, signal.SIGKILL)
    finally:
        os.close(fd)
    wait_for(lambda: health() and state().get("owner") != previous["owner"], "host did not recover dead owner")
    assert len(services()) == 1
    print("PASS host-only owner-death recovery and singleton", flush=True)

    os.environ["DJANGO_SETTINGS_MODULE"] = "dispatcharr.settings"
    os.environ["DISPATCHARR_SKIP_PLUGIN_AUTODISCOVERY"] = "1"
    sys.path.insert(0, "/app")
    import django
    django.setup()
    from django.db import connection, transaction
    from apps.plugins.models import PluginConfig
    spec = importlib.util.spec_from_file_location("diagnostic_arr_plugin", root / "plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plugin = module.Plugin.__new__(module.Plugin)
    key = "arr_stack_connector"
    def enabled(value):
        cfg = PluginConfig.objects.get(key=key)
        cfg.enabled = value
        cfg.save(update_fields=["enabled", "updated_at"])
        return cfg

    # Exercise unchanged public protocols using a disposable key, never sending
    # or logging the copied production API key in test HTTP requests.
    cfg = PluginConfig.objects.get(key=key)
    original_settings = dict(cfg.settings or {})
    cfg.settings = dict(original_settings, api_key="isolated-test-key-not-a-user-secret")
    cfg.save(update_fields=["settings", "updated_at"])
    try:
        with urlopen("http://127.0.0.1:9192/api?t=caps&apikey=isolated-test-key-not-a-user-secret", timeout=5) as response:
            assert b"<caps" in response.read()
        with urlopen("http://127.0.0.1:9192/api?mode=version&apikey=isolated-test-key-not-a-user-secret", timeout=5) as response:
            assert json.load(response).get("version")
        from urllib.error import HTTPError
        try:
            urlopen("http://127.0.0.1:9192/api?t=caps&apikey=wrong-isolated-key", timeout=5)
            raise AssertionError("Invalid API key was accepted")
        except HTTPError as exc:
            assert exc.code == 401
    finally:
        cfg.settings = original_settings
        cfg.save(update_fields=["settings", "updated_at"])
    print("PASS Newznab caps, SAB version and authentication", flush=True)

    # Exact normal host ordering: hook under row lock before enabled=False save.
    # Owner must continue seeing committed true until commit, then converge off.
    with transaction.atomic():
        cfg = PluginConfig.objects.select_for_update().get(key=key)
        plugin.stop({"reason": "disable"})
        cfg.enabled = False
        cfg.save(update_fields=["enabled", "updated_at"])
        time.sleep(1.2)
        assert health(), "uncommitted disable was treated as committed"
    wait_for(off, "committed disable did not stop listener")
    enabled(True)
    wait_for(health, "enable required manual intervention")
    print("PASS row-locked disable/commit/automatic re-enable", flush=True)

    class Rollback(Exception): pass
    try:
        with transaction.atomic():
            cfg = PluginConfig.objects.select_for_update().get(key=key)
            plugin.stop({"reason": "disable"})
            cfg.enabled = False
            cfg.save(update_fields=["enabled", "updated_at"])
            time.sleep(1.2)
            assert health()
            raise Rollback()
    except Rollback:
        pass
    cfg = PluginConfig.objects.get(key=key)
    assert cfg.enabled
    cfg.name += " [isolated probe]"
    cfg.save(update_fields=["name", "updated_at"])
    cfg.settings = dict(cfg.settings or {}, _isolated_lifecycle_probe=True)
    cfg.save(update_fields=["settings", "updated_at"])
    wait_for(health, "rollback/unrelated edits stranded enabled service")
    print("PASS rollback plus unrelated metadata/settings", flush=True)

    # Capability denial hook is AUTOCOMMIT and precedes the saved false value.
    plugin.stop({"reason": "capability_update_denied"})
    time.sleep(1.2)
    assert health()
    enabled(False)
    wait_for(off, "capability denial committed false was ignored")
    enabled(True)
    wait_for(health, "capability-denial re-enable stranded service")
    # Rapid false/true commits need no intermediate false observation.
    enabled(False); enabled(True)
    wait_for(health, "rapid re-enable stranded service")
    print("PASS capability denial and rapid committed re-enable", flush=True)

    assert plugin.run("stop")["status"]
    wait_for(lambda: off("stopped"), "manual Stop failed")
    for reason in ("reload", "shutdown"):
        plugin.stop({"reason": reason})
        module.Plugin()
    enabled(False); enabled(True)
    time.sleep(1.2)
    assert off("stopped"), "manual Stop lost across reload/shutdown/enable"
    stopped_owner = state()
    fd = os.pidfd_open(stopped_owner["pid"])
    try:
        signal.pidfd_send_signal(fd, signal.SIGKILL)
    finally:
        os.close(fd)
    wait_for(lambda: state().get("alive") and state().get("owner") != stopped_owner["owner"] and off("stopped"),
             "manual Stop was lost after owner crash")
    assert plugin.run("restart")["status"]
    wait_for(health, "manual Restart failed")
    print("PASS persistent manual Stop and explicit Restart", flush=True)

    # Fault injection: occupied port causes startup failure. Releasing it must
    # recover automatically, without another constructor/Restart after release.
    assert plugin.run("stop")["status"]
    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("0.0.0.0", 9192)); blocker.listen()
    try:
        result = plugin.run("restart")
        assert not result["status"]
        assert not health()
    finally:
        blocker.close()
    wait_for(health, "bind failure did not recover automatically", timeout=40)
    print("PASS failed startup automatically recovered after port release", flush=True)

    before = state()["owner"]
    marker = root / "servarr_bridge" / "_isolated_generation_probe.py"
    marker.write_text("# isolated source generation probe\n")
    try:
        wait_for(lambda: health() and state()["owner"] != before, "source handoff failed")
        assert len(services()) == 1
        current = state()["owner"]
    finally:
        marker.unlink(missing_ok=True)
    wait_for(lambda: health() and state()["owner"] != current, "source rollback handoff failed")
    print("PASS source-generation handoff and rollback, one owner", flush=True)

    cfg = PluginConfig.objects.get(key=key)
    # Preserve all physical columns, including 0.34 columns absent from the
    # rolled-back 0.33 ORM model in this copied database. No values are printed.
    with connection.cursor() as cursor:
        cursor.execute("SELECT row_to_json(c)::text FROM plugins_pluginconfig c WHERE key=%s", [key])
        saved = cursor.fetchone()[0]
    plugin.stop({"reason": "delete"})
    cfg.delete()
    wait_for(off, "deleted row did not stop listener")
    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO plugins_pluginconfig SELECT (json_populate_record(NULL::plugins_pluginconfig, %s::json)).* "
                       "ON CONFLICT (key) DO UPDATE SET enabled=EXCLUDED.enabled, settings=EXCLUDED.settings", [saved])
    wait_for(health, "recreated enabled row did not recover")
    print("PASS missing row and recreated enabled row", flush=True)

    # Physical removal retires owner. Reinstall invokes ordinary constructor,
    # as Dispatcharr installation/discovery does; no manual service repair.
    temporary = Path(tempfile.mkdtemp(prefix=".arr-stack-removed-", dir=root.parent))
    moved = temporary / "plugin"
    root.rename(moved)
    try:
        wait_for(lambda: not state().get("alive"), "source removal left owner running")
        assert not health()
    finally:
        moved.rename(root)
        temporary.rmdir()
    module.Plugin()
    wait_for(health, "reinstall discovery did not restore owner")
    assert len(services()) == 1
    print("PASS source removal and reinstall discovery", flush=True)

    # Verify genuine native watchdog progress while main thread deliberately
    # does not drive a gevent hub. Real host recovery above covers host contexts.
    code = r'''
import gevent.monkey
gevent.monkey.patch_all()
import os,sys,tempfile,pathlib,threading
sys.path.insert(0, "/data/plugins/arr_stack_connector")
from servarr_bridge import lifecycle
sleep=lifecycle.native("time","sleep")
with tempfile.TemporaryDirectory() as tmp:
 p=pathlib.Path(tmp);(p/"service.py").write_text("import time\nwhile True: time.sleep(1)\n")
 (p/"plugin.py").write_text("");(p/"plugin.json").write_text("{}")
 seen=[];main=threading.get_native_id();spawn=lifecycle._spawn
 def observed(*args):
  seen.append(threading.get_native_id())
  return spawn(*args)
 lifecycle._spawn=observed
 job=lifecycle.ensure_watchdog(p,p,sys.executable,dict(os.environ))
 try:
  deadline=__import__('time').monotonic()+4
  while not job["children"] and __import__('time').monotonic()<deadline: sleep(0.05)
  assert seen and seen[0]!=main, "watchdog did not run in a native OS thread"
  assert len(job["children"])==1, "native-thread real spawn failed"
 finally:
  job["stop"]=True
  children=tuple(job["children"])
  import signal
  for pid in children:
   try:
    fd=os.pidfd_open(pid)
    try: signal.pidfd_send_signal(fd,signal.SIGKILL)
    finally: os.close(fd)
   except ProcessLookupError: pass
  sleep(1.2)
  for pid in children:
   try: lifecycle.native("os","waitpid")(pid,0)
   except ChildProcessError: pass
  assert not job["alive"]
print("GENUINE_NATIVE_REAL_SPAWN_WATCHDOG_PASS")
'''
    native = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=15)
    assert native.returncode == 0, native.stderr
    print(native.stdout.strip(), flush=True)
    print("RECONCILIATION_INTEGRATION_PASS", flush=True)


if __name__ == "__main__":
    main()
