# Plan: 016 dynamic folder ingestion (kb_ingest_folder)

## Summary

New MCP tool `kb_ingest_folder` lets an agent (or operator) ingest a
folder whose **path is chosen at call time**, without touching config.
Today the only folder-ingestion path (`fs` source) has its directory
fixed in config/env; changing it means editing config. This feature
adds a one-shot source driven by tool arguments and reuses the
existing dedup-safe pipeline.

## Approach

1. **One-shot folder source** (`digital_twins/ingest/onshot.py`, new):
   a small `Source`-conforming object `OneShotFolderSource(path,
   source_name="folder", recursive=True)` mirroring
   `FsSource.read` semantics (`rglob`, mtime ts, `errors="replace"`
   read, relative key, `metadata.path`). It is **not** a registered
   config source — it is constructed per-call from the tool args.
2. **Tool registry** (`digital_twins/mcp/registry.py`): add
   `kb_ingest_folder` with inputSchema
   `{path: string (required), source_name?: string, recursive?:
   boolean (default true), agent_kind?: string}`. Description
   documents the 4 error codes + the dedup guarantee.
3. **Tool body** (`digital_twins/mcp/dispatch.py`):
   `_kb_ingest_folder_body(ctx, args)`:
   - Role gate: `reader` → `permission_denied` (same gate as
     `kb_ingest` — reader cannot trigger runs).
   - Path validation on the **server process's** filesystem: missing /
     not a dir / not readable → `bad_request` with code
     `path_not_found` / `not_a_directory` / `path_not_readable`.
     Echo back only the caller-supplied `path`, never more (NFR-13).
   - Build `OneShotFolderSource`, run `run_pipeline` with
     `trigger="mcp"`, `scheduled_by=ctx.caller_email`,
     `source=source_name` — audited exactly like `kb_ingest`
     (one `audit_runs` row, `per_source_counts` under the
     `source_name`).
   - Return the FR-5 summary shape.
4. **Dedup (NFR-1)**: `since=None` on the one-shot source (mtime
   filter is meaningless for a one-shot), but the pipeline's
   content-hash dedup still applies — re-calling the same path
   writes 0 new points. This keeps the top acceptance check intact.
5. **Tests (Test-First, constitution Principle III)**:
   - Unit: tool body with a tmp dir — happy path, re-run dedup,
     4 error codes, `reader` role refusal. Monkeypatch
     `run_pipeline` for unit scope; one integration test runs the
     real pipeline into a temp Qdrant-shaped target (mirror the
     `kb_ingest` integration test shape in
     `tests/integration/test_mcp_kb_tools.py`).
   - Registry test: `kb_ingest_folder` appears in
     `build_tool_registry()` with the FR-1 schema.
   - Guards: `test_portability.py` (T006) + `test_knob_docs.py`
     (T027) stay green; no new knob exists so knob docs are
     unchanged.

## Files touched

- `digital_twins/ingest/onshot.py` (new)
- `digital_twins/mcp/registry.py` (tool entry + schema)
- `digital_twins/mcp/dispatch.py` (tool body + registry wiring)
- `tests/unit/test_mcp_ingest_folder.py` (new)
- `tests/integration/test_mcp_kb_tools.py` (one new test, or a new
  file if it's cleaner)
- `CHANGELOG.md` (0.12.0 entry — new tool surface = MINOR)
- `digital_twins/__init__.py` (version bump)
- `specs/016-.../tasks.md`, `.superpowers/sdd/016-.../progress.md`,
  `openspec/changes/016-.../` (proposal/spec/design/tasks)

## Semver ruling

MINOR (0.11.x → 0.12.0): new MCP tool surface, no breaking change, no
new config knob. Existing `kb_ingest` + `fs` source contract
unchanged.

## Non-goals

- No watch-mode / continuous ingestion.
- No remote paths (S3/HTTP) — filesystem paths only, read by the
  server process.
- No per-file progress streaming; the tool is request/response JSON
  (004 transport contract).
- No ACL change: the folder's owner scope is the **caller**'s
  (same as every other MCP trigger); the folder itself has no
  owner attribute in v1.
