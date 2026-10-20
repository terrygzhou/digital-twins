## Purpose
Behavior contract for `kb_ingest_folder` on the MCP transports: a
caller can ingest a folder whose **path is supplied per call**, with
a JSON summary response, `kb_ingest`-equivalent auditing, and
content-hash dedup — without any config change.

## ADDED Requirements

### Requirement: kb_ingest_folder ingests a caller-supplied folder path
When a caller with a run-capable role (admin / scheduler) invokes
`kb_ingest_folder` with a readable directory `path`, the server
SHALL recursively walk `path` (unless `recursive=false`), ingest
every regular file through the standard pipeline, and return a JSON
summary of per-file outcomes and point counts. No config value SHALL
be read or modified for the path.

#### Scenario: Happy path returns the summary
- **WHEN** a scheduler-role caller POSTs
  `{"tool": "kb_ingest_folder", "args": {"path": "/tmp/reports"}}`
  and `/tmp/reports` is a readable directory with regular files
- **THEN** the response is `ok: true` with `items` (one entry per
  file, status `written` or `skipped`), `points_written`,
  `points_skipped`, and `errors: 0`

#### Scenario: Re-ingesting the same path writes no new points
- **WHEN** `kb_ingest_folder` is called twice with the same
  unchanged `path`
- **THEN** the second run's `points_written` is 0 (content-hash
  dedup, NFR-1)

#### Scenario: Reader role is refused
- **WHEN** a `reader`-role caller invokes `kb_ingest_folder`
- **THEN** the response is `ok: false` with error code
  `permission_denied` and no pipeline run occurs

#### Scenario: Missing path is a clean JSON error
- **WHEN** `path` does not exist (or is not a directory / not
  readable)
- **THEN** the response is `ok: false` with error code
  `path_not_found` / `not_a_directory` / `path_not_readable`
  respectively, and no pipeline run occurs

### Requirement: kb_ingest_folder is audited like kb_ingest
Every `kb_ingest_folder` run SHALL write one `audit_runs` row with
`trigger="mcp"`, the caller's email as `scheduled_by`, and
`per_source_counts` under the chosen `source_name` (default
`"folder"`), so `kb_run_history` reports it with the existing
scoping rules.
