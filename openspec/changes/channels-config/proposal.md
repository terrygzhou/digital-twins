# Change: Configurable ingestion channels (CLI + web admin UI)

## Why
BR-11.2.1–7 and BR-12.2.1 specify that every ingestion source (file path,
agent chat history, email with auth, web link, user notes, custom channels)
shall be configurable via the config layer, the CLI, and the web admin UI.
The current implementation ships source adapters and per-source knobs
(`sources.<name>.enabled/max_items/timeout_s`), but exposes no dedicated
CLI surface for channel configuration and no web admin UI panel for it —
the `/api/config/services` admin endpoint only covers service endpoints
(Qdrant / Neo4j / LLM / embedding), not source/channel state.

This change closes that gap: first-class CLI and web-UI surfaces for listing,
enabling, disabling, and configuring ingestion channels, so that external
or partial hosting (BR-12.2) and multi-user source overrides (BR-11.4.2) are
completable without editing config files by hand.

## What changes
- **CLI**: add a `digital-twins channels` command group:
  - `channels list` — list all known sources (built-in + user-defined custom),
    their `enabled` state, capability summary, and missing prerequisites
    (fail-fast names the missing item per BR-11.2.2).
  - `channels enable <name> [--max-items N] [--timeout-s N]`
  - `channels disable <name>`
  - `channels status <name>` — show current effective config + prerequisites
    (reads all four config layers, same precedence as the rest of the package).
  - `channels add <name> --entrypoint module:factory [--credential ENV_VAR]`
    — register a user-defined custom source without a package update
    (BR-11.2.7).
- **Web admin UI**: extend `/api/config/services` (or add
  `/api/config/channels`) to support:
  - `GET /api/config/channels` — masked view of all sources: `enabled`,
    `max_items`, `timeout_s`, credential-set boolean, prerequisite status
    (admin-gated, same as `/api/config/services`).
  - `POST /api/config/channels` — body: `{"<name>": {"enabled": bool,
    "max_items": int, "timeout_s": int}}`; persists to `kb.local.yml` via
    `config.local_io.merge_write`; returns post-write masked view.
    Credential values are never returned or logged (BR-12.2.2 / FR-004).
- **Web admin UI**: add a "Channels" panel to the admin HTML page,
  listing each source with a toggle, cap/timeout inputs, and credential
  status indicator (mirrors the existing Services panel pattern).
- **Docs**: document the new CLI subcommands in `docs/` and the web panel
  in `docs/configuration.md` (BR-11.2.4 — every knob documented).
- **Per-user channel overrides (BR-11.4.2)**: extend the per-user config
  store to carry `{source_name: {enabled, max_items, timeout_s}}`; the
  scheduler already reads per-user schedule presets — this extends the same
  mechanism to per-user source overrides so the effective channel config is
  `user_override > machine-local > global > defaults`.

## Non-goals
- No new source adapter implementations (web URL fetcher, notes parser) —
  those are separate changes.
- No hard per-user data isolation (Q2 is deferred per requirement.md §5).
- No change to ingestion pipeline internals (dedup, point-ID, payload shape).

## Impact
- Affected specs: none (this is a new capability; no existing spec delta).
- Affected code: `digital_twins/cli.py`, `digital_twins/web/app.py`,
  `digital_twins/config/` (new channel view helper), `digital_twins/user_config.py`.
- NFR-1 dedup invariant unaffected (channel config does not touch item IDs).
- BR-11.2.7 "user can add new sources without a package update" — the
  `channels add` CLI + custom source factory already exists; this change
  makes it discoverable and testable through the admin surfaces.
