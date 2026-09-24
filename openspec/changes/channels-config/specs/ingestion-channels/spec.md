# Capability: ingestion-channels — configurable channel surfaces

## ADDED Requirements

### Requirement: CLI exposes channel list with prerequisite status
`digital-twins channels list` SHALL list every known source (all built-in
sources from `BUILTIN_SOURCES` plus any user-registered custom source found
in `kb.yml` / `kb.local.yml`) with: source name, `enabled` boolean (effective
across all four config layers), `max_items` and `timeout_s` (effective values),
and a `prerequisites` column that names each missing prerequisite by name
(empty when ready). A source whose `prerequisites()` returns a non-empty
list SHALL be marked `BLOCKED: <names>` rather than silently shown as
disabled.

#### Scenario: All sources listed on a fresh install
- **WHEN** a user runs `digital-twins channels list` after a fresh install
  with no config files
- **THEN** all built-in sources appear, each `enabled: false`,
  and any source with unsatisfied prerequisites shows `BLOCKED: <prereq>`.

#### Scenario: Custom source appears after registration
- **WHEN** a user has registered a custom source `mytool` in `kb.local.yml`
  (via `digital-twins channels add mytool --entrypoint m.tool:build_mytool`)
- **THEN** `digital-twins channels list` shows `mytool` alongside the
  built-in sources, with its effective `enabled` state.

### Requirement: CLI enable/disable/configure subcommands
`digital-twins channels enable <name>` SHALL set
`sources.<name>.enabled: true` in `kb.local.yml` (preserving all other
keys). `digital-twins channels disable <name>` SHALL set
`sources.<name>.enabled: false`. Both SHALL accept `--max-items N` and
`--timeout-s N` overrides to write the per-source cap/timeout in the same
call. The commands SHALL write to `kb.local.yml` (machine-local, untracked),
never to `kb.yml` (committed).

#### Scenario: Enable a source with cap override
- **WHEN** a user runs `digital-twins channels enable gmail --max-items 50`
- **THEN** `kb.local.yml` contains `sources.gmail.enabled: true` and
  `sources.gmail.max_items: 50`, and `kb.yml` is untouched.

### Requirement: CLI status subcommand shows effective config
`digital-twins channels status <name>` SHALL print the effective
`enabled`, `max_items`, `timeout_s`, credential-set boolean, and
prerequisite status for the named source, resolved through the full
four-layer config precedence (env > kb.local.yml > kb.yml > defaults).
It SHALL exit non-zero if the named source is not a known source
(built-in or registered custom) — fail-fast naming the unknown source
(BR-11.2.2).

#### Scenario: Status for a source with missing credential
- **WHEN** `KB_SOURCES__GMAIL__ENABLED=true` but `KB_GMAIL_CREDENTIAL`
  env var is not set
- **THEN** `digital-twins channels status gmail` reports
  `credential: NOT SET (env var KB_GMAIL_CREDENTIAL)` and exit code 0
  (status is informational; the failure will surface at run time per
  BR-11.2.2).

### Requirement: CLI add subcommand registers a custom source
`digital-twins channels add <name> --entrypoint module:factory
[--credential ENV_VAR] [--prefix PREFIX]` SHALL write a
`sources.<name>` entry in `kb.local.yml` with `enabled: false`,
`entrypoint`, `credential`, and `prefix` fields, and SHALL validate that
the entrypoint module is importable before writing (fail-fast,
`CustomSourceError` — BR-11.2.2).

#### Scenario: Add a custom source
- **WHEN** a user runs `digital-twins channels add mytool
  --entrypoint m.tool:build_mytool --credential MY_TOOL_TOKEN`
- **THEN** `kb.local.yml` gains `sources.mytool.enabled: false`,
  `sources.mytool.entrypoint: m.tool:build_mytool`,
  `sources.mytool.credential: MY_TOOL_TOKEN`.

### Requirement: Web admin API exposes channel view
`GET /api/config/channels` (admin-only, same gate as
`/api/config/services`) SHALL return a JSON object:
```
{
  "sources": {
    "<name>": {
      "enabled": bool,
      "max_items": int,
      "timeout_s": int,
      "credential_set": bool,
      "prerequisites": ["<missing>", ...]   // empty when ready
    }, ...
  },
  "env_overrides": ["KB_SOURCES__..."]   // env vars shadowing source knobs
}
```
Credential values SHALL be reduced to the `credential_set` boolean;
the values themselves SHALL NOT appear in the response (BR-12.2.2).

#### Scenario: Admin fetches the channel view
- **WHEN** an admin requests `GET /api/config/channels`
- **THEN** the response contains one entry per known source with
  `enabled`, `max_items`, `timeout_s`, `credential_set`, and
  `prerequisites`; no credential value string appears anywhere in
  the response body.

### Requirement: Web admin API accepts channel config writes
`POST /api/config/channels` (admin-only) SHALL accept a JSON body:
```
{"<source_name>": {"enabled": bool, "max_items": int?, "timeout_s": int?}}
```
and persist the write to `kb.local.yml` via `config.local_io.merge_write`
(preserving unrelated keys). The response SHALL be the post-write masked
view (same shape as GET). An unknown source name SHALL yield 404; a
schema-invalid value SHALL yield 422. Credential values SHALL NOT be
logged in any request or response (BR-12.2.2 / FR-004).

#### Scenario: Enable a source via the web API
- **WHEN** an admin POSTs `{"hermes": {"enabled": true, "max_items": 100}}`
  to `/api/config/channels`
- **THEN** `kb.local.yml` gains `sources.hermes.enabled: true` and
  `sources.hermes.max_items: 100`, and the response reflects the new
  effective values.

### Requirement: Per-user channel overrides are stored and respected
The per-user config store SHALL support a `channel_overrides` mapping:
`{<source_name>: {"enabled": bool, "max_items": int?, "timeout_s": int?}}`.
The scheduler SHALL resolve the effective channel config for each run as:
`user_override > env > kb.local.yml > kb.yml > defaults`. A per-user
override of `enabled: false` SHALL suppress that source for that user's
runs without affecting global or other users' runs (BR-11.4.2).

#### Scenario: Per-user source disable does not affect global
- **WHEN** user alice has `channel_overrides.gmail.enabled: false`
  and global `kb.yml` has `sources.gmail.enabled: true`
- **THEN** alice's scheduled runs omit gmail; other users' runs still
  include gmail.

## MODIFIED Requirements
(none — this is a new capability)

## REMOVED Requirements
(none)
