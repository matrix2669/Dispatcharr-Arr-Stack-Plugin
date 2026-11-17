# Dispatcharr concurrent-startup compatibility validation

Scope: local, unpublished Arr Stack Connector compatibility fix. No LineupARR,
provider matching, API route, credential, download, or live configuration changes.

## Reproduction and fix

The unmodified plugin reproduced a startup hang on Dispatcharr 0.34.0 with a
consistent copied database and only Arr Stack installed. The same copy without
Arr Stack started. Diagnostic stacks showed the new leadership discovery path
waiting in `fcntl.flock(LOCK_EX)` while the same process already owned that lock.
This is evidence of a reproducible incompatibility, not a recovered traceback
from the user's deleted failed container.

The fix retains constructor startup, makes lock acquisition nonblocking, retries
contention outside discovery, and fences lifecycle requests. It is not a full
conversion to the new host lifecycle API. Retry timeout/failure is logged; an
unavailable service still requires diagnostic attention rather than infinite
background retries.

## Test isolation and limitations

- Containers use `--network none`, no published ports, restart disabled, and no
  Docker healthcheck so host auto-heal cannot interfere. HTTP health is probed
  manually at the configured port 80.
- All data is disposable. PostgreSQL is restored from an online `pg_dump` with a
  three-second lock-wait bound. No live data, recordings, device or socket mounts.
- Installed plugin source is copied; external plugin mutable state is omitted.
- The 0.33 control needs 27 `pg_attver` metadata statements removed from the copied
  SQL dump for its older PostgreSQL minor version. Live data is never altered.
- Outbound integration failures in other plugins are expected under network
  isolation. This does not validate real downloads or external provider traffic.
- Containers and temporary copied data are removed after every run. Production
  remains pinned to 0.33.0, healthy, restart count zero.

Image identities:

- 0.34.0: `sha256:bcf8dda41864e52b00f30136d882d8b61299a1ca6612d61124850186b2320f60`
- 0.33.0: `sha256:96bc548978c68e684677da7895692eeeefb7f0b2edfa38311dac2b702c61d236`

## Acceptance evidence

Portable automated regressions cover same-process lock contention, deferred retry,
request cancellation, restart serialization, persistent Stop, disabled discovery,
reload/shutdown fencing, superseded commands, malformed control, lock errors and
termination failure. The complete suite has 50 passing tests, including the
existing 28 bridge/helper tests. Python compilation, manifest parsing, version
agreement, whitespace and source archive layout are checked locally.

Isolated all-plugin copies start on both versions with HTTP 200, exactly one
Arr Stack service process, and a healthy service endpoint. The committed opt-in `tests/isolated_lifecycle_probe.py` additionally checks
persistent Stop across reload/shutdown and uses two real PostgreSQL connections:
one pauses a row-locked disable transaction after stop and before the enabled
update; the other confirms a stale enabled read, requests Restart, and verifies
no service resurrection before or after commit. Re-enable and running-service
reload must then succeed. The probe also rolls back Disable, writes unrelated metadata and settings, and
verifies Restart remains inhibited until an actual committed disable/enable.
The probe refuses non-isolated environments. Detailed private diagnostic logs are retained
outside the repository; no database dumps or account configuration are committed.

No remote CI, publication, registry update, production upgrade, or live deployment
is claimed. Independent review and release gates remain separate.
