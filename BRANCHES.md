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
| `dev` | long-lived | active | `main` | `main` | Integrate and validate the next plugin version; synchronized to stable Arr Stack Connector `0.2.0`. |
| `chore/version-tags-release-notes` | maintenance | active | `main` at `2c7441b` | `main` | Replace historical version branches with exact-commit tags, remove superseded branches, and repair the `v0.2.0` release-note formatting. |

## Active branch records

### `main`

- Purpose: production-ready source and the line for stable tags and explicitly approved GitHub Releases.
- Current plugin version: `0.2.0`.
- Current distribution: approved for normal GitHub Release `v0.2.0` and focused publication through the registry `main` channel.
- Promotion source: the live-tested `v0.2.0-beta.1` state, with only version and release metadata changed for the stable build.
- Last verified at: `2026-08-22`.

### `dev`

- Purpose: integrate and validate the next plugin version before promotion to `main`.
- Base: `main` at `13c5d114fce94ae4c802128bae224d8623163141`.
- Current plugin version: `0.2.0`.
- Current state: synchronized to the completed stable source promoted to `main` and approved for release.
- Intended target: `main` after versioned testing is complete.
- Validation: 28 unit tests pass; all Python modules compile; `plugin.json` parses; version and identity assertions agree; the beta installed successfully on Dispatcharr; and the clean release ZIP is required to contain only the top-level `arr-stack-connector/` directory.
- Last verified at: `2026-08-22`.

### `chore/version-tags-release-notes`

- Purpose: complete the documented branch-to-tag migration and correct the published `v0.2.0` release body.
- Base: `main` at `2c7441b`.
- Intended target: `main`.
- Scope: repository refs, branch ledger, release metadata, and validation evidence only; no plugin runtime or release artifact changes.
- Validation: all 17 historical registry commit SHAs match their same-name annotated tag targets; remote branches are deleted; the corrected release body uses Markdown line breaks and retains both original assets.
- Last verified at: `2026-08-22`.
