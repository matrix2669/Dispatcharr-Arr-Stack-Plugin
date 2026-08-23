# Arr Stack Connector Dependencies

This file records external behavior required for the plugin to operate. Current repository and API state must be verified before changing compatibility claims or publishing a new version.

## Mustarrd upstream dependency

Status: required changes exist in the maintained fork and are not present in current upstream Mustarrd.

| Item | Current record |
|---|---|
| Upstream repository | `razzamatazm/mustarrd` |
| Reviewed upstream | `main` at `2b7ea998eb5918da9447488d148c526a77091497` |
| Fork production branch | `matrix2669/mustarrd:dev` at `7c56b83879f76faff8f303c139063a1f51a75431` |
| Dependency branch | `feature/dispatcharr-arr-connector-integration` at `55a787f080eaa0e728972ed66d6f6fffde859f83` |
| Required implementation | `0ae5d147e76878e60a897d7cfdbd65abb3068ada` |
| Upstream PR | Not yet created |

The dependency branch replaces the deleted name `feature/dispatcharr-vod-integration`. The implementation is already an ancestor of fork `dev`; the documentation-only branch head is not. Current upstream does not contain the external endpoint.

### Required contract

Arr Stack Connector requires Mustarrd to:

- expose authenticated `POST /api/vod/external/download` support;
- accept the Mustarrd account, media ID, title, absolute HTTP(S) Dispatcharr proxy URL, safe relative output path, and duration;
- restrict arbitrary external URL submission to an authorized local integration user or administrator;
- keep the requested path inside Mustarrd's configured download root and preserve it under the completed root;
- queue the request through Mustarrd's normal VOD download and concurrency lifecycle;
- retry bounded transient HTTP failures and resume safely where supported;
- expose cancellation, failed/cancelled retry, history deletion, queue, and completion behavior compatible with the plugin's SAB translation.

### Upstream readiness gate

The existing dependency branch is 13 commits behind current upstream and contains an unrelated historical `{tmdb}` ancestor. It must not be submitted upstream as-is.

Before declaring compatibility with unmodified upstream Mustarrd:

1. Refresh all upstream refs and contribution guidance.
2. Rebuild only the required implementation from current upstream `main`.
3. Reconcile it with current upstream VOD, download-manager, authentication, retry, and test behavior.
4. Run both complete Mustarrd suites plus Arr Stack Connector automated and live handoff validation.
5. Submit and track a focused upstream PR.
6. Confirm the required behavior exists in an accepted upstream commit; a closed, superseded, or partial PR is not sufficient.

Until this gate is complete, deployments must use the maintained Mustarrd fork revision containing `0ae5d14` or a verified descendant.
