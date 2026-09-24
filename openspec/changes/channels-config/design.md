# Design: Configurable ingestion channels (CLI + web admin UI)

## Decision: Add a `channels` CLI group rather than extending `run`

The existing `digital-twins run --source <name>` takes source names but
does not *configure* sources. A dedicated `channels` command group is
clearer (config surface vs. action surface), and mirrors the existing
`schedule` group (which also manages config, not just runs).

## Decision: Extend `/api/config/services` pattern, not create a new subsystem

The admin web app already has a well-tested admin-gated services panel
pattern: `_require_admin` → masked `_config_services_view` →
`merge_write` to `kb.local.yml`. The channels panel reuses that exact
pattern with a parallel `_CONFIG_CHANNEL_FIELDS` dict. This keeps the
surface consistent and avoids duplicating admin/auth plumbing.

## Decision: Per-user overrides live in `user_config.py` channel_overrides

BR-11.4.2 requires per-user channel config. `user_config.py` already
stores per-user scheduler presets and caps. Adding a `channel_overrides`
mapping follows the existing pattern — no new storage mechanism needed.
The scheduler's existing per-user config merge point (already used for
per-user caps in BR-11.3.5) becomes the resolution point:
`user_override > env > kb.local.yml > kb.yml > defaults`.

## Decision: Prerequisite status is advisory in the UI, fatal at run time

BR-11.2.2 mandates fail-fast at run time. The CLI `channels status`
and web panel show `prerequisites` as a warning (names missing items),
but do not block enabling a source. This matches current behaviour:
a source can be enabled even when its credential env var is not yet
set; the failure surfaces at `read()` time with a clear error. This
avoids the UX problem of not being able to enable a source until
credentials are in place, while still making the gap visible.

## Decision: `channels add` validates the entrypoint before writing

`build_custom` in `digital_twins/sources/custom.py` already validates
that a custom source entrypoint is importable and returns a `Source`
(BR-11.2.2 fail-fast). `channels add` SHALL call this same validation
before persisting to `kb.local.yml`, so a bad entrypoint is rejected
immediately rather than failing at next run.

## Decision: No new source adapters in this change

Web URL fetching and structured notes ingestion are separate changes.
The `channels` surfaces in this change manage *any* source — built-in
or user-defined — so new adapters slot in without further UI/CLI
work.

## Cross-system collision note
Per-user channel overrides are user-scoped config, not data. They do
not affect the NFR-1 dedup invariant (same content → one point), which
is governed by item_id / content_hash, not by which sources are
enabled.

## Spec-013 delta
No Cypher test-contract changes required.

## SDD ledger entry
Create `.superpowers/sdd/015-channels-config/progress.md`.
