# Changelog

All notable changes to this project are documented here.

This file contains release history only. Architectural decisions belong in `DECISIONS.md`.

The format follows Keep a Changelog principles where practical.

## [0.2.1-beta.1] - 2026-11-17

### Fixed

- Prevent concurrent Dispatcharr 0.34 discovery from deadlocking service startup while retaining 0.33 compatibility.
- Reconcile the listener against committed plugin state, recover owner failures automatically, and preserve manual Stop until explicit Restart.
- Handle bind-setting changes, source replacement, deletion/reinstall, and watchdog retirement safely; preserve startup API-key initialization.

### Upgrade note

- Perform a clean Dispatcharr restart after installing this first reconciler beta. Mixed old/new loaded plugin instances are not supported.
- Isolated lifecycle/authentication tests passed on 0.33 and 0.34; real provider and Sonarr/Radarr/Mustarrd download traffic remains a deployment validation step.

### Changed

- Renamed the Mustarrd dependency branch to `feature/dispatcharr-arr-connector-integration` and documented that its external VOD endpoint and lifecycle changes are required upstream support for Arr Stack Connector, not functionality currently present in unmodified upstream Mustarrd.
- Converted the historical `v0.1.0` through `v0.1.16` version branches to annotated tags at their exact original commits, verified them against the legacy registry metadata, and removed the obsolete branches.
- Removed the superseded `docs/bootstrap-release-sync` branch after confirming its durable guidance is present or superseded on `main`.
- Corrected the published `v0.2.0` GitHub Release notes from literal escaped line breaks to properly rendered Markdown without changing the release artifacts.

## [0.2.0] - 2026-08-22

### Added

- Added project AI documentation structure with `AGENT.md` and `DECISIONS.md`.
- Documented architecture decisions for Dispatcharr proxy handling, Mustarrd integration, and Servarr compatibility.
- Added standalone branch tracking, canonical version, and release procedures.

### Changed

- Improved documentation separation between user guidance, agent instructions, architectural decisions, and release history.
- Renamed the public project to **Dispatcharr Arr Stack Plugin** and the in-Dispatcharr display name to **Arr Stack Connector**.
- Adopted the standalone `main`/`dev` workflow with immutable beta and stable tags instead of permanent version branches.
- Changed the registry/archive slug to `arr-stack-connector`, installed key to `arr_stack_connector`, state root to `/data/arr_stack_connector`, and plugin-owned environment variables to `ARR_STACK_CONNECTOR_*`.
- Added a one-time migration path from the legacy plugin and state directories while preserving settings IDs, API behavior, and job-state formats.

## [0.1.16]

### Added

- Added lightweight Servarr recent-feed handling.
- Added improved Servarr release naming behavior.

### Changed

- Improved Sonarr/Radarr validation performance.
- Reduced unnecessary metadata probing during validation.
- Improved ffprobe discovery across Dispatcharr runtime layouts.

### Fixed

- Fixed ffprobe path assumptions across different Dispatcharr container layouts.
- Fixed validation workflows performing expensive operations unnecessarily.

## [0.1.15]

### Added

- Added lightweight recent-feed support for Servarr workflows.

### Changed

- Optimized indexer validation paths.
- Improved compatibility with different ffprobe installation layouts.
- Improved Servarr feed performance.

## [0.1.14]

### Added

- Improved plugin service startup and health handling.
- Added API key generation and display improvements.
- Improved Sonarr/Radarr indexer validation performance.
- Reduced unnecessary ffprobe work during validation requests.

### Fixed

- Fixed plugin loading failures caused by detached service startup handling.
- Fixed cases where indexer validation took several minutes to complete.

## [0.1.13]

### Added

- Initial public plugin release.
- Newznab-compatible indexer support.
- SABnzbd-compatible download client support.
- Mustarrd-backed VOD download lifecycle.
- Dispatcharr native VOD proxy integration.
