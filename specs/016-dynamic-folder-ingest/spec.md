# Feature 016: Dynamic folder ingestion (kb_ingest_folder)

## User Scenarios & Testing

An agent (or human operator) wants to ingest a folder **whose path is
chosen at call time**, not fixed in config. Today the only folder
ingestion path is the `fs` source, whose directory is locked in
config/env (`sources.fs.extra.dir` / `KB_SOURCES__FS__EXTRA__DIR`) —
changing the folder means editing config and restarting.

**Primary scenario**

1. An agent decides to ingest `/reports/q3` right now, on this call.
2. It invokes MCP tool `kb_ingest_folder` with
   `{"path": "/reports/q3"}` (plus optional `source_name`).
3. The server validates the path, walks it (recursive), ingests every
   regular file through the normal dedup/chunk/embed pipeline, and
   returns a JSON summary (files found, files ingested, points
   written, skipped/unchanged count, per-file errors).

**Secondary scenario — per-user named sources**

4. A user whose agent manages several project folders calls
   `kb_ingest_folder` with `{"path": "/data/alice/notes",
   "source_name": "alice_notes"}`. The files are tagged with
   `source=alice_notes`, prefix `alice_notes:`, so owner-scoped
   `kb_search` keeps them isolated from the default `fs` source.

**Edge cases**

- Path does not exist / not a directory → clean JSON error
  (`bad_request` with `code: path_not_found`), no crash, no partial run.
- Empty directory → `ok` with zero files, an empty `items`.
- Permission denied on a file → file-level error recorded in
  `items[i].error`; the run continues with the other files.
- Non-UTF-8 bytes → read with `errors="replace"` (same as the `fs`
   source today), no crash.
- Caller role `reader` → `permission_denied` (same gate as
  `kb_ingest`).

## Functional Requirements

- **FR-1**: Add MCP tool `kb_ingest_folder` to
  `digital_twins/mcp/registry.py` with inputSchema
  `{path: string (required), source_name: string (optional, default
  "folder"), recursive: bool (optional, default true), agent_kind:
  string (optional)}`.
- **FR-2**: Add tool body `_kb_ingest_folder_body(ctx, args)` in
  `digital_twins/mcp/dispatch.py`: role gate (`reader` →
  `permission_denied`), path validation (`path_not_found` /
  `not_a_directory` / `path_not_readable`), then a one-shot
  `run_pipeline` over the given folder (trigger `"mcp"`, scheduled_by
  caller email, audited like `kb_ingest`).
- **FR-3**: Implement a one-shot folder source
  `digital_twins/ingest/onshot.py` (or `sources/`-level helper): given
  a `Path` + optional `source_name`/prefix, walk the tree, yield
  `IngestItem`s exactly like `FsSource.read` (recursive `rglob`,
  mtime ts, `errors="replace"` read, relative key, `metadata.path`).
  No config dependency — the path arrives in the tool args.
- **FR-4**: Pipeline runs with `since=None` for the one-shot source
  but content-hash dedup (NFR-1) still applies, so re-ingesting the
  same folder does not create duplicate points.
- **FR-5**: Response contract:
  `{"ok": true, "tool": "kb_ingest_folder", "items": [{"key", "status"
  in {written, skipped, error}, "point_id"?, "error"?}], "points_written": n,
  "points_skipped": n, "errors": n}`; on path-level failure:
  `{"ok": false, "error": {"code": "path_not_found" | "not_a_directory"
  | "path_not_readable" | "permission_denied" | "config_not_loaded",
  "message": ...}}`.
- **FR-6**: No new config knob. The folder path is a **call-time
  argument**, not a config value — this is the point of the feature.
  NFR-13 holds: no host paths/usernames in shipped code; `path`
  validation errors never echo absolute host paths beyond the
  caller-supplied value.
- **FR-7**: The existing `fs` source and `kb_ingest` tool are
  unchanged (config-fixed folder path still works as before).

## Success Criteria

- An agent can ingest **any readable folder** without editing config:
  one MCP call, JSON result, sub-minute on a few thousand small files.
- Re-calling the same path yields 0 new points (dedup holds; NFR-1
  top acceptance check intact).
- `kb_search` finds the folder's content under the chosen
  `source_name`'s owner scope.
- The 4 error codes above are all exercised by unit tests.

## Key Entities

- `IngestItem` (key, content, ts, metadata) — unchanged.
- One-shot folder source (ephemeral `Source` instance, not a
  registered config source).
- `audit_runs` row: trigger `"mcp"`, tool `kb_ingest_folder`,
  `source` = chosen source_name.

## Assumptions

- Folder size is modest (files already chunked by the pipeline);
  no streaming needed for v1.
- The caller's filesystem user owns read permission on the folder
  (the server process reads it).
- `source_name` becomes the ingest source/prefix; default `"folder"`.

## Open questions (max 3)

- [NEEDS CLARIFICATION: should `kb_ingest_folder` also accept a list
  of paths, or is one path per call sufficient for v1?] — default:
  one path per call (a list is a trivial v1.1 add).
