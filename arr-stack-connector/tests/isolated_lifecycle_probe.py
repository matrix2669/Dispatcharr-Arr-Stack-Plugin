"""Explicit integration probe for disposable, network-none Dispatcharr copies only.

Never run against production. Caller must opt in and provide restored disposable
PostgreSQL/plugin data. This is not part of portable unittest discovery.
"""
import importlib.util
import os
from pathlib import Path
import sys
import socket
import threading


def main():
    if os.environ.get("ARR_STACK_ISOLATED_TEST") != "1":
        raise RuntimeError("Requires explicit disposable-test opt in")
    if set(name for _, name in socket.if_nameindex()) != {"lo"}:
        raise RuntimeError("Requires network-none container")
    if b"sleep\x00infinity" not in Path("/proc/1/cmdline").read_bytes():
        raise RuntimeError("Requires disposable diagnostic container entrypoint")
    os.environ["DJANGO_SETTINGS_MODULE"] = "dispatcharr.settings"
    os.environ["DISPATCHARR_SKIP_PLUGIN_AUTODISCOVERY"] = "1"
    sys.path.insert(0, "/app")
    import django
    django.setup()
    from django.db import connections, transaction
    from apps.plugins.models import PluginConfig

    spec = importlib.util.spec_from_file_location(
        "diagnostic_arr_plugin", "/data/plugins/arr_stack_connector/plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plugin = module.Plugin.__new__(module.Plugin)
    assert plugin._health(), "startup service unavailable"
    plugin.run("stop")
    for reason in ("reload", "shutdown"):
        plugin.stop({"reason": reason})
        module.Plugin()
        assert not plugin._health(), "persistent Stop lost on " + reason
    assert plugin.run("restart")["status"], "explicit Restart failed"

    class RollbackProbe(Exception):
        pass

    try:
        with transaction.atomic():
            cfg = PluginConfig.objects.select_for_update().get(key="arr_stack_connector")
            plugin.stop({"reason": "disable"})
            cfg.enabled = False
            cfg.save(update_fields=["enabled", "updated_at"])
            raise RollbackProbe()
    except RollbackProbe:
        pass
    cfg = PluginConfig.objects.get(key="arr_stack_connector")
    assert cfg.enabled, "Disable rollback did not preserve enabled state"
    cfg.name += " [isolated metadata probe]"
    cfg.save(update_fields=["name", "updated_at"])
    assert not plugin.run("restart")["status"], "Metadata update bypassed rolled-back Disable"
    cfg.settings = dict(cfg.settings or {}, _isolated_lifecycle_probe=True)
    cfg.save(update_fields=["settings", "updated_at"])
    module.Plugin()
    assert not plugin.run("restart")["status"], "Settings update bypassed rolled-back Disable"
    assert not plugin._health()
    # Recovery requires an actual committed disabled state followed by enable.
    with transaction.atomic():
        cfg = PluginConfig.objects.select_for_update().get(key="arr_stack_connector")
        plugin.stop({"reason": "disable"})
        cfg.enabled = False
        cfg.save(update_fields=["enabled", "updated_at"])
    cfg.enabled = True
    cfg.save(update_fields=["enabled", "updated_at"])
    module.Plugin()
    assert plugin._health(), "Committed disable/enable did not recover"

    pending = threading.Event()
    commit = threading.Event()
    errors = []

    def disable_transaction():
        try:
            with transaction.atomic():
                cfg = PluginConfig.objects.select_for_update().get(key="arr_stack_connector")
                assert cfg.enabled
                # Exact Dispatcharr 0.34 ordering: stop under row lock before
                # updating enabled, then delay commit while another connection
                # reads the still-enabled committed row and requests Restart.
                plugin.stop({"reason": "disable"})
                pending.set()
                if not commit.wait(30):
                    raise RuntimeError("Concurrent restart probe timed out")
                cfg.enabled = False
                cfg.save(update_fields=["enabled", "updated_at"])
        except BaseException as exc:
            errors.append(exc)
            pending.set()
        finally:
            connections.close_all()

    worker = threading.Thread(target=disable_transaction, daemon=True)
    worker.start()
    try:
        assert pending.wait(30), "Disable did not reach transaction barrier"
        if errors:
            raise errors[0]
        assert PluginConfig.objects.get(key="arr_stack_connector").enabled, "Expected stale enabled read"
        assert not plugin.run("restart")["status"], "Restart bypassed uncommitted Disable"
        module.Plugin()
        assert not plugin._health(), "Service resurrected before Disable commit"
    finally:
        commit.set()
        worker.join(30)
    assert not worker.is_alive(), "Disable thread did not finish"
    if errors:
        raise errors[0]
    cfg = PluginConfig.objects.get(key="arr_stack_connector")
    assert not cfg.enabled
    module.Plugin()
    assert not plugin._health(), "Service survived committed Disable"
    cfg.enabled = True
    cfg.save(update_fields=["enabled", "updated_at"])
    module.Plugin()
    assert plugin._health(), "Re-enable failed"
    plugin.stop({"reason": "reload"})
    module.Plugin()
    assert plugin._health(), "Running service reload failed"
    print("LIFECYCLE_PASS persistent-stop/rollback-unrelated-updates/concurrent-transaction-disable/reenable/reload")


if __name__ == "__main__":
    main()
