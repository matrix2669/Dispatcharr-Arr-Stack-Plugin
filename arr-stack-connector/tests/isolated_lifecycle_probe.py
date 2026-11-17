"""Opt-in characterization in disposable network-none Dispatcharr copies only.

This demonstrates inherited lifecycle behavior, not a disable/stop-race fix.
"""
import importlib.util
import os
from pathlib import Path
import socket
import sys
import time


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
    from django.db import transaction
    from apps.plugins.models import PluginConfig
    spec = importlib.util.spec_from_file_location(
        "diagnostic_arr_plugin", "/data/plugins/arr_stack_connector/plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plugin = module.Plugin.__new__(module.Plugin)
    assert plugin._health(), "startup service unavailable"
    assert plugin.run("restart")["status"], "Restart failed"
    assert plugin.run("stop")["status"]
    assert not plugin._health(), "Stop did not stop the current service"
    module.Plugin()
    assert plugin._health(), "Subsequent construction failed to start service"
    print("INHERITED: explicit Stop is not persistent across later construction")
    with transaction.atomic():
        cfg = PluginConfig.objects.select_for_update().get(key="arr_stack_connector")
        plugin.stop({"reason": "disable"})
        cfg.enabled = False
        cfg.save(update_fields=["enabled", "updated_at"])
    module.Plugin()
    assert plugin._health(), "Baseline constructor behavior changed"
    print("INHERITED: constructor itself does not enforce enabled state")
    cfg.enabled = True
    cfg.save(update_fields=["enabled", "updated_at"])
    plugin.stop({"reason": "delete"})
    assert not plugin._health(), "Delete stop hook did not stop current service"
    print("CHARACTERIZED: delete stop hook only; host file/database deletion not exercised")
    assert plugin.run("restart")["status"], "Restart after stop hook failed"
    print("LIFECYCLE_CHARACTERIZATION_PASS restart/stop/later-construction/disable-hook/delete-hook")


if __name__ == "__main__":
    main()
