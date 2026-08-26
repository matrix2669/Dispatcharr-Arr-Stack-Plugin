# Branches

This ledger records why every current branch exists. GitHub remains authoritative for live refs, commits, pull requests, and checks.

## Maintenance rules

- Add or update a record before substantive work begins on a branch.
- Refresh observed heads and validation before review or promotion.
- Use a branch record as source material for a pull request, but verify it against the current diff first.
- Before deleting a branch, transfer user-visible results to `CHANGELOG.md` and durable rationale to `DECISIONS.md`, then remove its entry here.

## Branch index

| Branch | Type | Status | Base | Target | Purpose |
|---|---|---|---|---|---|
| `main` | long-lived | active | initial project history | stable source | Production-ready plugin source and explicitly approved GitHub Releases. |
| `dev` | long-lived | active | `main` | `main` | Integrate and validate the next plugin version; synchronized to current `main` at stable Arr Stack Connector `0.2.0`. |
| `feature/session-completion-remote-checkpoint` | governance | active | `dev` at `fae497e` | `dev` | Reconcile the mandatory session-end GitHub checkpoint rule without changing plugin behavior or distribution. |

## Active branch records

### `main`

- Purpose: production-ready source and the line for stable tags and explicitly approved GitHub Releases.
- Current plugin version: `0.2.0`.
- Current distribution: approved for normal GitHub Release `v0.2.0` and focused publication through the registry `main` channel.
- Promotion source: the live-tested `v0.2.0-beta.1` state, with only version and release metadata changed for the stable build.
- Current governance: the mandatory workspace standards drift and reconciliation gate is recorded at the applied workspace revision.
- Last verified at: `2026-08-24`.

### `dev`

- Purpose: integrate and validate the next plugin version before promotion to `main`.
- Base: current `main`.
- Current plugin version: `0.2.0`.
- Current state: synchronized to the completed stable source on `main`, including current dependency and workspace-governance documentation.
- Current documentation scope: track the renamed Mustarrd dependency branch, the upstream support required by Arr Stack Connector, and mandatory workspace standards reconciliation.
- Intended target: `main` after versioned testing is complete.
- Validation: 28 unit tests pass; all Python modules compile; `plugin.json` parses; version and identity assertions agree; the beta installed successfully on Dispatcharr; and the clean release ZIP is required to contain only the top-level `arr-stack-connector/` directory.
- Last verified at: `2026-08-24`.

### `feature/session-completion-remote-checkpoint`

- Purpose: inherit the workspace rule that every session checkpoints all in-scope work on its owning GitHub branch while keeping integration and publication separate.
- Base: `dev` at `fae497e3b23f5c7a36b456fb46721bffca0699ac`.
- Intended target: `dev` after review.
- Scope: `AGENT.md`, `WORKSPACE-STANDARDS.yaml`, and this branch record.
- Exclusions: plugin runtime, rename or dependency contracts, version, tag, registry, Release, ZIP, or deployment changes.
- Validation: workspace standards validation and `git diff --check` pass; no plugin, rename, dependency, version, registry, release, or deployment behavior changed.
- Started: `2026-08-26`.
