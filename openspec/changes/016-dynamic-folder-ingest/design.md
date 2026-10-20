# Design: 016 dynamic folder ingestion

## Context
`kb_ingest` triggers configured sources. The `fs` source's directory
is a config value; there is no call-time path. Agents need one.

## Decisions

1. **One-shot source, not a config source.** A new
   `OneShotFolderSource(path, source_name, recursive)` object
   (Source contract: `prerequisites`/`read`/`close`) is built inside
   the tool body from the args. It is never registered in the config
   source table, so `kb_ingest` (config-driven) is untouched.
2. **Mirror `FsSource` read semantics exactly** (recursive `rglob`,
   mtime ts, `errors="replace"`, relative key, `metadata.path`) so
   content + content-hash dedup (NFR-1) behave identically to the
   `fs` source — re-ingesting the same folder writes 0 new points.
3. **Error contract.** Path-level failures return
   `bad_request` with codes `path_not_found` / `not_a_directory` /
   `path_not_readable`; the role gate returns `permission_denied`
   for `reader` (same as `kb_ingest`). File-level read errors are
   recorded per-item (`items[i].error`), the run continues.
   NFR-13: errors echo only the caller-supplied `path`.
4. **Audit.** One `audit_runs` row, `trigger="mcp"`,
   `scheduled_by=caller_email`, `source=source_name` (default
   `"folder"`), `per_source_counts` under `source_name` — the
   `kb_ingest` shape, so `kb_run_history` shows it normally.
5. **Semver MINOR** (new tool; no breaking change; no knob).

## Non-goals
No watch mode, no remote paths, no streaming progress, no owner
attribute on the folder itself (owner scope = caller, as every MCP
trigger).
