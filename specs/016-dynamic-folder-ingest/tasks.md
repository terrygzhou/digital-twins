# Tasks: 016 — dynamic folder ingestion (kb_ingest_folder)

## 1. One-shot folder source (RED-first, constitution Principle III)

- [x] 1.1 Write unit tests in `tests/unit/test_onshot_folder.py`
      (new file): a tmp dir with nested files →
      `OneShotFolderSource(path, source_name="x").read(since=None)`
      yields the expected `IngestItem`s (relative keys, `metadata.path`,
      mtime ts, `errors="replace"` on a non-UTF8 file, recursive walk,
      `recursive=False` skips subdirs). **Verification:** tests fail on
      current code (module does not exist). No host paths in the test
      (NFR-13) — use `tmp_path`.

- [x] 1.2 Implement `digital_twins/ingest/onshot.py`:
      `OneShotFolderSource(path: Path, source_name: str = "folder",
      recursive: bool = True)` conforming to the `Source` contract
      (`prerequisites()` → path-exists checks; `read(since)` → the
      walk; `close()` → no-op), mirroring `FsSource` read semantics
      exactly so content/dedup behaviour matches the `fs` source.
      **Verification:** §1.1 tests green.

## 2. MCP tool surface

- [x] 2.1 In `digital_twins/mcp/registry.py`: add
      `_kb_ingest_folder_schema()` + the `kb_ingest_folder` entry in
      `build_tool_registry()` (schema: `path` required string,
      `source_name` optional string, `recursive` optional bool
      default true, `agent_kind` shared prop). Description documents
      the 4 error codes + dedup guarantee. Update the module
      docstring's tool list (ten → eleven tools).
      **Verification:** a registry test asserts the 11th tool is
      present with the exact schema.

- [x] 2.2 Unit tests (RED) in `tests/unit/test_mcp_ingest_folder.py`:
      monkeypatched `dispatch.run_pipeline` — happy path (assert
      pipeline got the one-shot source, trigger `"mcp"`,
      `scheduled_by` = caller, `source` = chosen name),
      `reader` role → `permission_denied`, missing path →
      `path_not_found`, file path (not a dir) → `not_a_directory`,
      config None → `config_not_loaded`. **Verification:** tests fail
      before the body exists.

- [x] 2.3 Implement `_kb_ingest_folder_body(ctx, args)` in
      `digital_twins/mcp/dispatch.py` + register it in the tool
      dispatch table. Role gate mirrors `kb_ingest` (reader
      refused). Path validation echoes only the caller-supplied
      `path` (NFR-13). Audit row exactly like `kb_ingest`.
      **Verification:** §2.2 tests green.

## 3. Integration + guards

- [x] 3.1 One integration test (in `tests/integration/`, mirror the
      `kb_ingest` test shape): real pipeline run over a tmp folder
      into the integration target — second run writes 0 new points
      (NFR-1 dedup), `kb_search` finds the content under
      `source_name`. **Verification:** test green.

- [x] 3.2 Guards: `python3 -m pytest tests/integration/test_portability.py
      tests/unit/test_knob_docs.py -q` → both green (no new knob, no
      host paths introduced).

- [x] 3.3 Full suite: `python3 -m pytest tests/ -q` → all green.

## 4. Semver + release note

- [x] 4.1 Bump `__version__` to 0.12.0 (MINOR — new tool surface).

- [x] 4.2 `CHANGELOG.md` 0.12.0 entry: "Added: `kb_ingest_folder`
      MCP tool — ingest a caller-supplied folder path at call time
      (016)."

## 5. Bookkeeping

- [x] 5.1 `openspec/changes/016-dynamic-folder-ingest/` (proposal /
      spec / design / tasks) — validate with
      `openspec validate 016-dynamic-folder-ingest` if the CLI is
      available.

- [x] 5.2 `.superpowers/sdd/016-dynamic-folder-ingest/progress.md`
      ledger created and kept in sync with commits.

## Verification summary

- `pytest tests/unit/test_onshot_folder.py tests/unit/test_mcp_ingest_folder.py
  tests/unit/test_mcp_registry.py -q` → all passed (tool present,
  body correct).
- `pytest tests/ -q` → all green.
- `pytest tests/integration/test_portability.py tests/unit/test_knob_docs.py -q`
  → both guards green.
