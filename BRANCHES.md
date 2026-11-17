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

## Active branch records

### `main`

- Purpose: production-ready source and the line for stable tags and explicitly approved GitHub Releases.
- Current plugin version: `0.2.0`.
- Current distribution: approved for normal GitHub Release `v0.2.0` and focused publication through the registry `main` channel.
- Promotion source: the live-tested `v0.2.0-beta.1` state, with only version and release metadata changed for the stable build.
- Current governance: the project-specific workspace standards update and reconciliation gate is recorded at the applied workspace revision.
- Last verified at: `2026-08-24`.

### `dev`

- Purpose: integrate and validate the next plugin version before promotion to `main`.
- Base: current `main`.
- Current plugin version: `0.2.0`.
- Current state: synchronized to the completed stable source on `main`, including current dependency and workspace-governance documentation.
- Current documentation scope: track the renamed Mustarrd dependency branch, the upstream support required by Arr Stack Connector, and project-specific workspace standards reconciliation.
- Intended target: `main` after versioned testing is complete.
- Validation: 28 unit tests pass; all Python modules compile; `plugin.json` parses; version and identity assertions agree; the beta installed successfully on Dispatcharr; and the clean release ZIP is required to contain only the top-level `arr-stack-connector/` directory.
- Last verified at: `2026-08-24`.

### `fix/dispatcharr-034-service-lifecycle`

- Purpose: correct Arr Stack Connector startup coordination for concurrent Dispatcharr 0.34 discovery while retaining 0.33 compatibility.
- Base: `origin/dev` at `f77ced5`; isolated local worktree, no production edits.
- Scope: automatic committed-state reconciliation, native watchdog recovery, single-process lifetime ownership, durable manual commands, generation-safe replacement and isolated lifecycle validation. Earlier narrow/durable-transition attempts are superseded.
- Authorization: owner approved next beta promotion and development manifest publication on 2026-11-17; no stable Release or live deployment.
- Status: independent review clear; prepared for dev integration and immutable v0.2.1-beta.1. Production remains pinned to 0.33.0.
- Validation: portable and Linux-specific automated tests; isolated 0.33/0.34 all-plugin committed-state, failure-recovery and generation tests. See `docs/dispatcharr-034-startup-validation.md` for exact evidence and remaining release gates. No source CI workflows exist; exact-commit local validation is required. No deployment.
- Last verified: 2026-11-17.

## 2026-11-17 beta promotion

`dev` advances to the reviewed lifecycle repair as `0.2.1-beta.1`; `main` remains unchanged. Registry publication is restricted to `dispatcharr-plugins:dev`. Retain the fix branch; deletion is not authorized. Source has no GitHub Actions workflows. Exact-commit local validation accompanies existing 61-test Linux and copied-state 0.33/0.34 evidence.
