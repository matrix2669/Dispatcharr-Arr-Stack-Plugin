# Dispatcharr startup-contention compatibility correction

## Narrow accepted scope

The unmodified plugin reproduced a startup hang on Dispatcharr 0.34.0 with a
consistent copied database and only Arr Stack installed. Removing Arr Stack
eliminated it. Diagnostic stacks showed concurrent discovery waiting in blocking
`flock(LOCK_EX)` while the same process already owned that lock. This is a
reproducible incompatibility, not a recovered log from the deleted failed container.

The correction uses `LOCK_NB`; a contender returns immediately and logs that no
retry is scheduled. The owner performs the existing startup and health check.
Restart takes startup ownership before stopping a service, and reports busy if
ownership is unavailable. There are no deferred starts, new control files,
transaction callbacks, enable-transition guesses, or host lifecycle migration.

## Inherited limitations, deliberately not fixed

- Explicit Stop is not persistent across later construction.
- The constructor does not itself enforce enabled state. Concurrent discovery can
  conflict with stop/disable/delete hooks. This patch makes no guarantee otherwise.
- A failed winning initializer logs its error; contenders do not retry later.
  A later explicit Restart is the recovery path. Timeout can leave an unhealthy
  child, as before; Restart stops the existing process before retrying.
- Delete characterization covers the plugin stop hook, not Dispatcharr's actual
  file/database deletion. External integrations/downloads are not validated.

Earlier unpublished lifecycle redesign attempts on this branch were rejected and
superseded. They must not be presented as delivered behavior.

## Validation

38 portable tests pass, including the original 28 tests and startup regressions:
same-PID contention, another-process lock ownership, concurrent initializers
launching one child, no future startup after contention, busy Restart leaving the
owner alone, winning failure/timeout visibility and successful subsequent Restart,
lock error propagation, healthy process reuse and unchanged stop-hook dispatch.

The isolated runtime protocol uses all installed plugin sources and copied data on
both Dispatcharr 0.33 and 0.34. HTTP is checked at 20, 50, 170, 320 and 380 seconds,
past the observed five-minute auto-heal cadence. Both versions returned HTTP 200
at every probe through 380 seconds, with a healthy bridge and one service process.
The 0.34 log recorded 10 nonblocking contention skips; 0.33 recorded 6. Both passed
the lifecycle characterization probe. This confirms the contended path was
exercised, not just a contention-free boot. The committed opt-in characterization probe exercises Restart,
Stop, later construction, disabled construction and the delete stop hook. Its
inherited-behavior observations are limitations, not lifecycle improvements.

Python compilation, JSON/version consistency, source archive layout and whitespace
checks run locally. Detailed private evidence stays outside the repository; no
account configuration, database dumps or logs containing credentials are committed.
No remote CI, publication, registry change, production upgrade or deployment is
claimed. Independent review remains separate.

## Isolation and test boundaries

- Network-none containers, no published ports, restart disabled, Docker healthcheck
  disabled so production auto-heal cannot interfere. Manual probes use port 80.
- Disposable data restored from online `pg_dump` with a three-second lock-wait
  bound. No live data, recordings, devices or socket mounts.
- Installed plugin source is copied; external plugin mutable state is omitted.
- The 0.33 copy needs 27 `pg_attver` metadata statements removed from disposable SQL
  for its older PostgreSQL minor version. No live data is changed.
- Other plugins' outbound failures are expected under network isolation.
- Containers, copied data and downloaded test image are removed after validation.
  Production stays pinned to 0.33.0, healthy, restart count zero.

Image identities:

- 0.34.0: `sha256:bcf8dda41864e52b00f30136d882d8b61299a1ca6612d61124850186b2320f60`
- 0.33.0: `sha256:96bc548978c68e684677da7895692eeeefb7f0b2edfa38311dac2b702c61d236`
