"""Process-safe ownership and commands for the detached service reconciler.

Only the lease-owning service process manipulates its HTTP listener. Host workers
run cheap availability watchdogs; callbacks never decide database commit outcome.
"""
import errno
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import signal
import sys
import time
import types

log = logging.getLogger("apps.plugins.loader")
REGISTRY_KEY = "_arr_stack_connector_watchdogs_v1"
LEASE_FD = 3


def native(module, name):
    imported = __import__(module)
    try:
        import gevent.monkey
        if gevent.monkey.is_module_patched(module):
            return gevent.monkey.get_original(module, name)
    except ImportError:
        pass
    return getattr(imported, name)


def read_json(path, default=None):
    try:
        value = json.loads(Path(path).read_text())
        if not isinstance(value, dict):
            raise ValueError("Lifecycle record must be an object")
        return value
    except FileNotFoundError:
        return {} if default is None else default


def atomic_json(path, value, durable=False):
    path = Path(path)
    temporary = path.with_name(path.name + "." + secrets.token_hex(12))
    try:
        with temporary.open("x") as out:
            os.chmod(temporary, 0o600)
            json.dump(value, out, separators=(",", ":"))
            if durable:
                out.flush()
                os.fsync(out.fileno())
        os.replace(temporary, path)
        if durable:
            fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def control_path(state):
    return Path(state) / ".servarr_control.json"


def status_path(state):
    return Path(state) / ".servarr_status.json"


def lease_path(state):
    # Never replace/unlink this inode, including on disable/reload/delete.
    return Path(state) / ".servarr_owner.lock"


def request(state, action):
    if action not in {"stop", "restart"}:
        raise ValueError("Unknown lifecycle command")
    state = Path(state)
    with (state / ".servarr_control.lock").open("a+") as lock:
        deadline = time.monotonic() + 2
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                if time.monotonic() >= deadline:
                    raise RuntimeError("Timed out saving service command") from exc
                native("time", "sleep")(0.02)
        value = read_json(control_path(state))
        token = secrets.token_hex(16)
        value.update(command=token, manual_stop=action == "stop")
        if action == "restart":
            value["restart"] = token
        atomic_json(control_path(state), value, durable=True)
        return token


def process_start(pid):
    try:
        # comm may contain spaces/parentheses. Fields after the final ')' start
        # with field 3 (state), making field 22 (starttime) index 19.
        text = Path(f"/proc/{int(pid)}/stat").read_text()
        fields = text.rsplit(")", 1)[1].split()
        return fields[19] if fields[0] not in {"Z", "X"} else None
    except (OSError, ValueError, IndexError):
        return None


def owner_status(state):
    value = read_json(status_path(state))
    pid = value.get("pid")
    value["alive"] = bool(pid and value.get("process_start") and
                          process_start(pid) == value["process_start"])
    return value


def source_generation(root):
    root = Path(root)
    paths = [root / "plugin.json", root / "plugin.py", root / "service.py"]
    paths += sorted((root / "servarr_bridge").rglob("*.py"))
    digest = hashlib.sha256()
    try:
        for path in paths:
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    except FileNotFoundError:
        return None
    return digest.hexdigest()


def _registry():
    candidate = types.ModuleType(REGISTRY_KEY)
    candidate.pid = os.getpid()
    candidate.lock = native("_thread", "allocate_lock")()
    candidate.jobs = {}
    candidate.leases = set()
    state = sys.modules.setdefault(REGISTRY_KEY, candidate)
    # Check PID before acquiring an inherited lock. The fork child has no copy
    # of the thread that might have held it.
    if state.pid != os.getpid():
        for fd in state.leases:
            try:
                os.close(fd)
            except OSError:
                pass
        state.pid = os.getpid()
        state.lock = native("_thread", "allocate_lock")()
        state.jobs = {}
        state.leases = set()
    with state.lock:
        if not getattr(state, "fork_registered", False):
            def child():
                for fd in state.leases:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                state.pid = os.getpid()
                state.lock = native("_thread", "allocate_lock")()
                state.jobs = {}
                state.leases = set()
            os.register_at_fork(before=lambda: state.lock.acquire(),
                                after_in_parent=lambda: state.lock.release(),
                                after_in_child=child)
            state.fork_registered = True
    return state


def _spawn(root, state_dir, interpreter, env, fd):
    # CLOSEFROM is available in supported Dispatcharr Linux/Python 3.13 images.
    # Do not silently fall back to leaking another app's inheritable descriptors.
    if not hasattr(os, "POSIX_SPAWN_CLOSEFROM"):
        raise RuntimeError("Runtime lacks safe posix_spawn CLOSEFROM support")
    generation = source_generation(root)
    if generation is None:
        raise RuntimeError("Installed service source is incomplete; waiting for update completion")
    child_env = dict(env)
    child_env["ARR_STACK_CONNECTOR_SOURCE_GENERATION"] = generation
    child_env.update(ARR_STACK_CONNECTOR_LEASE_FD=str(LEASE_FD),
                     ARR_STACK_CONNECTOR_OWNER=secrets.token_hex(16))
    bootstrap = Path(state_dir) / "servarr_service_bootstrap.log"
    if bootstrap.exists() and bootstrap.stat().st_size > 5 * 1024 * 1024:
        for number in (3, 2, 1):
            older = bootstrap.with_name(bootstrap.name + "." + str(number))
            if number == 3:
                older.unlink(missing_ok=True)
            elif older.exists():
                os.replace(older, bootstrap.with_name(bootstrap.name + "." + str(number + 1)))
        os.replace(bootstrap, bootstrap.with_name(bootstrap.name + ".1"))
    with open(os.devnull, "rb") as null, \
         bootstrap.open("ab", buffering=0) as output:
        actions = [(os.POSIX_SPAWN_DUP2, null.fileno(), 0),
                   (os.POSIX_SPAWN_DUP2, output.fileno(), 1),
                   (os.POSIX_SPAWN_DUP2, output.fileno(), 2),
                   (os.POSIX_SPAWN_DUP2, fd, LEASE_FD),
                   (os.POSIX_SPAWN_CLOSEFROM, LEASE_FD + 1)]
        return native("os", "posix_spawn")(interpreter, [interpreter, str(Path(root) / "service.py")],
                              child_env, file_actions=actions, setsid=True, setsigmask=(),
                              setsigdef=(signal.SIGALRM, signal.SIGTERM, signal.SIGINT))


def ensure_watchdog(root, state_dir, interpreter, env):
    registry = _registry()
    key = (str(root), str(state_dir))
    with registry.lock:
        current = registry.jobs.get(key)
        if current and current["alive"]:
            return current
        job = {"alive": True, "stop": False, "children": set()}
        registry.jobs[key] = job
        def run():
            try:
                while not job["stop"] and (Path(root) / "service.py").is_file():
                    for pid in tuple(job["children"]):
                        try:
                            waited, _ = native("os", "waitpid")(pid, os.WNOHANG)
                            if waited:
                                job["children"].discard(pid)
                        except ChildProcessError:
                            job["children"].discard(pid)
                    try:
                        # Hold the registry lock across open/register so an
                        # unrelated host fork cannot inherit an untracked lease.
                        with registry.lock:
                            fd = os.open(lease_path(state_dir), os.O_RDWR | os.O_CREAT, 0o600)
                            registry.leases.add(fd)
                        owns = False
                        try:
                            try:
                                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            except OSError as exc:
                                if exc.errno not in (errno.EAGAIN, errno.EACCES):
                                    raise
                            else:
                                owns = True
                                previous = read_json(status_path(state_dir))
                                if float(previous.get("retry_after", 0)) <= time.time():
                                    atomic_json(status_path(state_dir), {
                                        "phase": "launching", "retry_after": time.time() + 2,
                                        "pid": 0, "listening": False,
                                    })
                                    pid = _spawn(root, state_dir, interpreter, env, fd)
                                    job["children"].add(pid)
                        except Exception as exc:
                            if owns:
                                atomic_json(status_path(state_dir), {
                                    "phase": "error", "pid": 0, "listening": False,
                                    "error": type(exc).__name__, "detail": str(exc)[:200],
                                    "retry_after": time.time() + 5,
                                })
                            raise
                        finally:
                            # Child retains the same open-file-description lease.
                            # LOCK_UN here would also unlock the child's lease!
                            with registry.lock:
                                registry.leases.discard(fd)
                                os.close(fd)
                    except Exception:
                        log.exception("[Arr Stack Connector] Service owner unavailable; watchdog will retry automatically")
                    native("time", "sleep")(1)
            finally:
                job["alive"] = False
        native("_thread", "start_new_thread")(run, ())
        return job


def acquire_owner(state_dir):
    inherited = os.environ.pop("ARR_STACK_CONNECTOR_LEASE_FD", None)
    if inherited is not None:
        fd = int(inherited)
        expected = os.stat(lease_path(state_dir))
        actual = os.fstat(fd)
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise RuntimeError("Invalid inherited service lease")
    else:
        fd = os.open(lease_path(state_dir), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(fd)
        raise
    os.set_inheritable(fd, False)
    # A request's subprocess must not keep this process alive as an owner.
    def close_in_fork_child():
        try:
            os.close(fd)
        except OSError:
            pass
    os.register_at_fork(after_in_child=close_in_fork_child)
    return fd


def retire_legacy(root):
    """Upgrade-only: retire exact old service processes while holding new lease.

    No process is ever signalled based solely on a stale PID file or port owner.
    """
    script = str(Path(root) / "service.py").encode()
    for path in Path("/proc").iterdir():
        if not path.name.isdigit() or int(path.name) == os.getpid():
            continue
        pid = int(path.name)
        try:
            if script not in (path / "cmdline").read_bytes().split(b"\0"):
                continue
            if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
                raise RuntimeError("Safe legacy-service retirement requires pidfd support")
            fd = os.pidfd_open(pid)
            try:
                # Open pidfd BEFORE identity verification, so PID reuse cannot
                # redirect a later signal to an unrelated new process.
                if script not in (path / "cmdline").read_bytes().split(b"\0"):
                    continue
                import select
                poller = select.poll()
                poller.register(fd, select.POLLIN)
                signal.pidfd_send_signal(fd, signal.SIGTERM)
                if not poller.poll(8000):
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                    if not poller.poll(2000):
                        raise RuntimeError("Legacy service did not exit; refusing duplicate owner")
            finally:
                os.close(fd)
        except (FileNotFoundError, ProcessLookupError):
            continue
