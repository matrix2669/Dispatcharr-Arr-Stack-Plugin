# Automatic service reconciliation validation

## Scope and guarantees

The unmodified plugin reproduced a startup hang on Dispatcharr 0.34: concurrent
constructor discovery blocked in flock while the same process already owned it.
The final design does not merely skip a contender. Host callbacks perform no
service effects; one detached owner reconciles committed database state and
persistent manual intent. Independent native watchdogs recover a failed owner.
HTTP lives inside the owner process, so death cannot leave an orphan listener.

Normal polling is 0.5 seconds. Initialization has a 30-second kernel deadline,
reconciliation steps 15 seconds, listener shutdown is bounded, and process exit
has a final five-second deadline. Failed bind/reconciliation retries back off up
to 15 seconds; watchdog availability checks run each second. These are bounded
attempts and eventual convergence, not a guarantee of availability during a
permanent external fault or instantaneous effect at commit. In-flight requests
may finish; already delegated remote downloads are not canceled.

Earlier narrow contention-only and inferred-disable-history revisions in local
history were rejected and are superseded. They are not the delivered design.

## Automated coverage

The full suite contains 61 tests. Four process tests require the actual Linux
runtime and are skipped on macOS; the complete suite is run in both supported
container images using a canonical source directory. Tests cover:

- Same-process ownership, inherited lease lifetime, fork-reset registries, native
  watchdog deduplication and serialized commands.
- Owner death and initialization timeout recovery without a new constructor.
- CLOSEFROM isolation of unrelated inheritable descriptors and pidfd legacy
  retirement without signalling an unrelated process.
- Missing/disabled/re-enabled rows, rollback/latest-state semantics, manual Stop,
  command generations, slow-start rechecks, failed bind recovery, database errors,
  listener death/stall, source snapshots and source removal.
- Existing Newznab/SAB/provider/helper contracts, compilation, JSON/version
  consistency, whitespace and archive layout.

## Isolated integration coverage

Both 0.33 and 0.34 copied-data all-plugin runs execute the committed opt-in
`tests/isolated_lifecycle_probe.py`. The probe refuses a non-isolated environment.
It verifies real PostgreSQL and host behavior:

1. Kill the owner **before this test process imports Django or constructs a
   plugin**; actual host watchdogs must replace it automatically, with one owner.
2. Authenticate Newznab caps and SAB version using a disposable test key; reject
   a wrong key. The copied production key is never sent in test HTTP requests.
3. Hold a row-locked disable uncommitted, then commit; continue the committed
   state before commit and converge off afterward. Re-enable without intervention.
4. Roll back disable, edit unrelated metadata/settings, and remain available.
5. Exercise autocommit capability-denial ordering and rapid false/true commits.
6. Preserve manual Stop through reload/shutdown, enabled toggles and owner death;
   clear it only by explicit Restart.
7. Occupy the port, force startup failure, release it, and require automatic
   recovery without another command or constructor.
8. Change and restore source generation, remove/recreate the row, physically
   remove/reinstall source and use ordinary discovery, always returning to one
   listener owner.
9. Under actual gevent monkey-patching, perform real spawn in a genuine native
   watchdog while the main thread does not drive a gevent hub.

Extended startup probes occur at 20, 50, 170, 320 and 380 seconds, beyond the
observed five-minute auto-heal cadence. Service health, process count and complete
unit results are collected with the runtime evidence.

## Isolation and evidence boundaries

- Network-none containers, no published ports, restart disabled and Docker
  healthcheck disabled to exclude production auto-heal. Manual HTTP probes use 80.
- Disposable data from online pg_dump with a three-second lock-wait bound; no
  live data, recordings, devices or socket mounts.
- Installed plugin source copied; mutable external plugin state omitted. Current
  package code replaces only Arr Stack in the copy.
- The 0.33 copy requires removal of 27 pg_attver metadata statements from disposable
  SQL. Its copied database can also retain 0.34-only physical columns; the row
  deletion test restores the full SQL row rather than assuming the older ORM can
  recreate those columns. No live data is altered.
- Other plugins' outbound errors are expected with network disabled. Real Sonarr,
  Radarr, provider traffic, Mustarrd downloads and hardware transcoding are not
  exercised. Business handlers remain unchanged apart from health ownership data
  and listener construction/control.
- Detailed private evidence stays outside the repository. No account settings,
  database dumps or private runtime logs are committed.

Images:

- 0.34.0: `sha256:bcf8dda41864e52b00f30136d882d8b61299a1ca6612d61124850186b2320f60`
- 0.33.0: `sha256:96bc548978c68e684677da7895692eeeefb7f0b2edfa38311dac2b702c61d236`

## Remaining release boundary

No remote CI, publication, registry change or live deployment is claimed. The
cold-start copied-state tests and pidfd migration test do not certify a rolling
mixed-old/new host update. First deployment requires a clean Dispatcharr restart,
a new version, independent review and normal release/archive/upgrade gates.
Production remains pinned to 0.33; disposable containers/data/test image are removed
after validation. Chrome and browser automation are not used.

## Final review and beta candidate, 2026-11-17

Independent review cleared both lifecycle race repairs and API-key initialization. All 61 Linux tests, lifecycle/authentication probes and 380-second health soaks passed on both versions with synthetic and owner-approved host-local copied data. Test containers and snapshots were removed. No production service was changed. Source runtime digest before version-only beta metadata: `4bfd80c71e854eea71e23deb5e9b0e0d421cd994c5da806b27bfcb60043ba5e6`.

Official compatibility refresh: `Dispatcharr/Dispatcharr` v0.34.0 commit `3e322c537f26207bf219aa5b4beaa69d3f5f19a4` and v0.33.0 commit `2fa13a6707bdf0dae87e0c494bcd3ca282cdeb3e`. Refreshed official v0.34 source confirms legacy manifest support, fresh-install hyphen preservation (`arr-stack-connector`), while 0.33 uses `arr_stack_connector`, nearest plugin.py archive discovery, atomic directory replacement, existing managed-key/settings retention, minimum-version checking and differing-beta update detection. Enable/disable hooks can execute before commit; settings are transactionally persisted. VOD account/stream selection and unique account/stream relations remain present. The beta keeps legacy manifest fields and minimum v0.29.0 unchanged. The plugin derives its database key from its installed directory, so both identities are supported. No claim is made that unmodified upstream Mustarrd supplies the fork-only dependency.
