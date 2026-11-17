# Dispatcharr Arr Stack Plugin

A Dispatcharr plugin that exposes **raw Xtream VOD provider variants** to Sonarr and Radarr as a Newznab indexer and emulates the subset of SABnzbd used by Servarr. Selected releases are handed to Mustarrd for the actual download/remux lifecycle.

This project was formerly named **Dispatcharr VOD Newznab Plugin**. The new registry slug and source archive directory are `arr-stack-connector`; Dispatcharr 0.33 installs it as `arr_stack_connector`; fresh 0.34 installs preserve `arr-stack-connector`, and managed updates retain the existing key.

## Architecture

```text
Sonarr / Radarr
  ├─ Newznab Interactive Search ─┐
  └─ SABnzbd Download Client ────┤
                                ▼
                    Dispatcharr Arr Stack Plugin
                    ├─ raw enabled Xtream accounts
                    ├─ TMDB matching + ffprobe
                    ├─ DV/HDR10+/HDR10/HDR/SDR naming
                    ├─ SAB category/job folder layout
                    └─ SAB queue/history translation
                                │
                                ▼
                    Dispatcharr native VOD proxy
                                │
                                ▼
                           Mustarrd API
                                │
                     downloads → completed
                                │
                                ▼
                         Sonarr / Radarr import
```

The plugin deliberately queries the **original Xtream providers configured in Dispatcharr**, not Dispatcharr's deduplicated XC VOD output. This allows multiple 720p/1080p/2160p/HDR/DV variants of the same TMDB title to appear in Interactive Search.

When a selected raw variant was skipped by Dispatcharr's normal importer, the plugin materializes only the missing provider relation needed for that grabbed stream. Mustarrd is then given a native Dispatcharr `/proxy/vod/...` URL pinned to the selected account and stream ID.

The proxy URL contains a unique VOD session ID in the path. This intentionally bypasses Dispatcharr's first-request **Redirect** behavior for Mustarrd jobs, so the media bytes pass through Dispatcharr's VOD connection manager and remain visible in Dispatcharr VOD statistics even when the global default stream profile is Redirect.

## Requirements

- A current Dispatcharr build with plugin support.
- `ffprobe` available at `/usr/bin/ffprobe` by default. The path is configurable.
- Mustarrd with `POST /api/vod/external/download` support. This is currently a required fork dependency tracked on `matrix2669/mustarrd:feature/dispatcharr-arr-connector-integration`; it is not yet available in unmodified upstream Mustarrd.
- A dedicated local Mustarrd user is recommended for the plugin.
- Mustarrd must be able to reach the configured **Dispatcharr URL Seen by Mustarrd**.
- The Mustarrd completed folder must be mounted into Sonarr/Radarr. Configure **Completed Directory Seen by Sonarr/Radarr** to the path those applications see.

See `DEPENDENCIES.md` for the exact Mustarrd contract, known-good fork revision, and upstream-readiness gate.

## Install

Install from the Matrix2669 Dispatcharr plugin registry. The installed directory is host-selected:

```text
/data/plugins/arr_stack_connector/  # 0.33 and existing underscore-key updates
/data/plugins/arr-stack-connector/  # fresh 0.34 installs
```

Then reload plugins in Dispatcharr and enable **Arr Stack Connector**. When enabled, the plugin starts one managed service process (default port `9192`) even when Dispatcharr has multiple uWSGI workers.

An API key is generated automatically on first enable. Use the plugin's **Service Status** action to display the service status and key.

### One-time migration from Dispatcharr VOD Newznab

The new slug is a separate Dispatcharr plugin identity, so an existing legacy VOD Newznab installation does not update in place automatically. The steps below describe the historical underscore-key migration. For a fresh 0.34 installation use its host-selected `arr-stack-connector` directory/record instead, and do not create both spellings. Ordinary managed Arr Stack updates retain the existing key and require no directory rename.

1. Disable the old plugin so its service releases port `9192`.
2. Copy or move `/data/plugins/dispatcharr_vod_newznab` to `/data/plugins/arr_stack_connector`.
3. Copy or move `/data/dispatcharr_vod_newznab` to `/data/arr_stack_connector` to preserve bridge job state and logs.
4. Install or update **Arr Stack Connector** from the renamed registry entry so the copied plugin files are replaced by the new version.
5. Copy the old plugin settings into the new plugin record. Reuse the existing API key to avoid changing the Sonarr and Radarr indexer/download-client credentials.
6. Enable **Arr Stack Connector**, verify Service Status and Sonarr/Radarr validation, then remove the old plugin entry and directories.

Do not enable both identities at the same time because both use port `9192` by default.

## Dispatcharr plugin settings

Important settings:

- **Listen Address / Port**: defaults to `0.0.0.0:9192`.
- **Newznab / SAB API Key**: generated automatically.
- **Dispatcharr URL Seen by Mustarrd**: required. Set the exact Dispatcharr base URL that Mustarrd can reach, including scheme and port when needed. There is no guessed hostname/default.
- **Mustarrd URL / Username / Password**: credentials for a local Mustarrd user.
- **Mustarrd Account ID**: existing Mustarrd account used for job ownership/concurrency.
- **Completed Directory Seen by Sonarr/Radarr**: e.g. `/completed`.
- **Sonarr SAB Category** and **Radarr SAB Category**: default to `sonarr` and `radarr`.
- **ffprobe Path**: defaults to `/usr/bin/ffprobe`.
- **Respect Enabled Dispatcharr VOD Groups**: limits raw provider results to VOD categories enabled for each account in Dispatcharr.

## Dispatcharr compatibility

This release supports Dispatcharr 0.33 and 0.34. Future changes target Dispatcharr 0.34 and later; that direction does not retroactively remove 0.33 support from this release. The manifest minimum remains 0.29.0 and is not evidence of validation on every older version. See the lifecycle validation notes for test scope and outstanding real-traffic validation.

## SAB-compatible completed layout

The final relative output path is determined at SAB `addfile` time because that is when Servarr supplies the actual category. The plugin mirrors normal SAB category + job-folder behavior:

```text
mustarrd/<SAB category>/<release>/<release>.<ext>
```

For example, with Radarr category `radarr`:

```text
mustarrd/radarr/Zootopia.2.2025.2160p.WEB-DL.DV.HEVC.DDP5.1-MUSTARRD/
└── Zootopia.2.2025.2160p.WEB-DL.DV.HEVC.DDP5.1-MUSTARRD.mkv
```

With **Completed Directory Seen by Sonarr/Radarr** set to `/completed`, the emulated SAB API reports:

```text
complete_dir: /completed
category dir: mustarrd/radarr
job storage: /completed/mustarrd/radarr/<release>
```

The plugin always rebuilds this path when `mode=addfile` is received, so an older cached NZB cannot restore the pre-0.1.5 Movies/TV Shows directory layout.

## Service logging and diagnostics

The plugin uses Dispatcharr's `apps.plugins.loader` logger for plugin lifecycle messages, prefixed with `[Arr Stack Connector]`.

The detached Newznab/SAB service writes its rotating log in the persistent plugin state directory:

```text
/data/arr_stack_connector/servarr_service.log
```

The log rotates at 5 MB and keeps three backups. Very early child-process stdout/stderr is captured separately in:

```text
/data/arr_stack_connector/servarr_service_bootstrap.log
```

Routine `/health` probes are logged only at DEBUG.

Bootstrap output is also rotated before a new owner launch when it exceeds 5 MB,
with three backups. Service Status includes the current controller phase and any
startup error; no saved credentials are included in that record.

## Service lifecycle (development)

The service now reconciles current **committed** Dispatcharr configuration rather
than starting/stopping inside plugin discovery. A single lease-owning process
hosts the HTTP listener. Lightweight native watchdogs recover that process if it
exits, and failed listener startup is retried automatically.

- Enabling starts the listener automatically; committed disable or a missing
  configuration row stops it. Rollback and rapid disable/re-enable do not require
  any remembered intermediate transition.
- **Stop Service** is a persistent manual inhibit. Reload, shutdown and toggling
  enabled do not clear it. **Restart Service** clears it, but never overrides a
  disabled plugin configuration.
- Source updates replace the owner automatically. The lifetime lock is retained
  until process teardown, so a replacement cannot overlap the previous owner.
- Reconciliation is asynchronous, not instantaneous at the commit boundary.
  Already accepted requests may finish; remote Mustarrd downloads are not canceled.
- Database errors fail closed. Startup, reconciliation and shutdown attempts have
  deadlines; watchdogs retry after a failed owner exits. Permanent configuration,
  filesystem or database failures still need their underlying cause corrected.

This development implementation is validated on the Linux/Python 3.13 Dispatcharr
0.33 and 0.34 runtimes. It requires safe `posix_spawn` CLOSEFROM and Linux pidfd
support; unsupported runtime capabilities are reported rather than bypassed.

**Upgrade requirement:** the first upgrade from the older constructor-based
service requires a clean Dispatcharr restart after installation. Old loaded plugin
instances cannot acquire the new manual-Stop semantics merely by updating files.
Do not promise safe mixed-generation rolling behavior. Settings, API key, plugin
identity and `/data/arr_stack_connector` remain unchanged. Do not remove the
private ownership lock while Dispatcharr is running. Publication and deployment
of this development code remain separate release gates.


## Radarr / Sonarr indexer

Add a generic Newznab indexer using:

```text
http://DISPATCHARR_HOST:9192/api
```

Use the generated plugin API key.

Set the indexer to **Interactive Search only**:

- RSS: disabled
- Automatic Search: disabled
- Interactive Search: enabled

The plugin advertises:

```text
movie-search: q,tmdbid
tv-search:    q,tmdbid,season,ep
```

## SABnzbd download client

Add SABnzbd to Sonarr/Radarr with:

```text
Host: DISPATCHARR_HOST
Port: 9192
API Key: <same plugin API key>
```

Recommended categories:

```text
Sonarr: sonarr
Radarr: radarr
```

The plugin implements the Servarr-used SAB modes:

- `version`
- `get_config`
- `fullstatus`
- `addfile`
- `queue`
- `history`
- queue/history delete
- `retry`

## Search behavior

### Movies

Radarr's TMDB ID is matched against each enabled original Xtream provider. Every exact raw stream match is probed (up to the configured maximum) and returned as a separate release.

### TV

Sonarr's TMDB ID selects raw provider series. Exact episode searches probe the actual episode stream. Season searches use provider episode metadata to avoid probing an entire season.

Dynamic range classification order is:

```text
DV → HDR10+ → HDR10 → HDR → SDR
```

Dolby Vision detection precedes HDR10 because DV streams may expose PQ/BT.2020 fallback metadata.

## Synthetic NZBs and download routing

Newznab results contain a small signed synthetic NZB. It contains no provider username/password or provider source URL.

On `addfile`, the plugin verifies the descriptor, reads the SAB category supplied by Servarr, constructs the SAB-compatible output path, and resolves the exact account/stream again. If Dispatcharr's importer did not retain that raw variant, the plugin creates the missing `M3UMovieRelation` or `M3UEpisodeRelation` for that real provider stream. The source passed to Mustarrd is then a Dispatcharr-native proxy URL of the form:

```text
<configured-dispatcharr-url>/proxy/vod/movie/<uuid>/mustarrd_<session>?m3u_account_id=<id>&stream_id=<provider-stream-id>
```

Episodes use `/proxy/vod/episode/...` in the same way.

Because the session ID is already present, Dispatcharr skips the initial Redirect branch and proxies the transfer through its VOD connection manager. The selected provider variant therefore appears in Dispatcharr's active VOD statistics while Mustarrd downloads it.

## State

`servarr_jobs.json` is written beside the plugin and stores only the mapping needed to translate Mustarrd jobs back to SAB category/title/output path. Provider passwords are not written to this file.

## Development tests

Pure helper tests can be run without a Dispatcharr instance:

```bash
python -m unittest discover -s tests -v
```

End-to-end provider, Django model, Newznab, SAB, native VOD proxy, and Mustarrd tests require a running Dispatcharr/Mustarrd environment.
