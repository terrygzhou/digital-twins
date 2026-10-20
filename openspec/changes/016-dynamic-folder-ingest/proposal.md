# Change: kb_ingest_folder — ingest a caller-supplied folder path at call time

## Why
Today the only folder ingestion path is the `fs` source, whose
directory is fixed in config/env (`sources.fs.extra.dir` /
`KB_SOURCES__FS__EXTRA__DIR`). An agent that wants to ingest a
different folder per call must have an operator edit config and
restart the server. Agents need a call-time path.

## What changes
- New MCP tool `kb_ingest_folder` (`digital_twins/mcp/registry.py` +
  `digital_twins/mcp/dispatch.py`): args
  `{path: string, source_name?: string, recursive?: bool}`.
- New one-shot source `digital_twins/ingest/onshot.py`
  (`OneShotFolderSource`): walks the given path, yields `IngestItem`s
  with the exact `FsSource` semantics (relative keys, mtime ts,
  `errors="replace"` read) so dedup behaviour is identical.
- The pipeline runs with `trigger="mcp"`, caller identity +
  `source_name` in the audit row (same as `kb_ingest`).
- **No new config knob** (the path is a tool argument). No
  **BREAKING** changes: `kb_ingest` + the `fs` source contract are
  untouched.
- Tests: RED-first unit tests for the source + tool body (4 error
  codes, reader-role refusal, dedup on re-run), one integration
  test (real pipeline, NFR-1 0-new-points on re-ingest).

## Capabilities
- **Modified Capabilities**: `mcp-transport` (a new tool on the
  transport). No main spec exists yet under `openspec/specs/`, so
  the delta is an ADDED Requirement: `kb_ingest_folder` returns a
  JSON summary for any readable folder passed as an argument, is
  audited like `kb_ingest`, and dedups by content hash.

## Impact
- Affected code: `digital_twins/mcp/registry.py`,
  `digital_twins/mcp/dispatch.py`, new
  `digital_twins/ingest/onshot.py`, tests.
- Unaffected: `state/db.py`, `sources/fs.py`, `kb_ingest`, web/stdio
  transports, scheduler.
- Semver: MINOR (new tool surface).
