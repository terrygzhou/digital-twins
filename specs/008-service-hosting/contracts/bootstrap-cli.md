# Contract: Bootstrap Script — scripts/bootstrap-local.sh (008, FR-005..008)

One invocation on a clean Docker host → healthy local stack + machine-local config (Q11, BR-12.3.2). Host-neutral (FR-008): no host paths, usernames, or interpreter pins; scanned by the portability guard.

## Usage

```bash
bash scripts/bootstrap-local.sh            # full local setup (Docker required)
bash scripts/bootstrap-local.sh --status   # per-service status only, no writes
bash scripts/bootstrap-local.sh --cloud    # no Docker: write cloud endpoints
```

## Environment overrides

| Var | Default | Meaning |
|---|---|---|
| `BOOTSTRAP_TIMEOUT_S` | 600 | max seconds to wait for service health |
| `LLM_SERVICE` | auto | `local` \| `skip` — auto = run bundled LLM only when a suitable GPU is detected (nvidia-smi probe) |
| `KB_QDRANT__URL` / `KB_NEO4J__URL` / `KB_LLM__ENDPOINT` | — | cloud-mode (`--cloud`) required endpoints; unset values are prompted on stdin |
| `KB_EMBEDDING__ENDPOINT` / `KB_NEO4J__USER` / `KB_NEO4J__PASSWORD` | unset | cloud-mode optional keys, written to `kb.local.yml` only when set |
| compose vars | as compose | `NEO4J_PASSWORD`, `QDRANT_HOST`, … (reference compose defaults apply) |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success (full stack healthy, or documented partial stack on no-GPU host) |
| 1 | Docker or compose CLI missing / daemon down (remediation printed) |
| 2 | port conflict on 6333/7474/7687/8000/8080 (service named) |
| 3 | health timeout (services named, `docker compose logs` hint printed) |
| 4 | config write error (file left unwritten or untouched) |
| 5 | cloud mode (`--cloud`): a required endpoint is missing (the missing `KB_*` variable named; no Docker involved) |

## Behaviors

- **Image source**: `docker-compose.yml` is the single source of image pins; the script pulls `qdrant`, `neo4j`, `llm` (public refs) and builds `digital-twins` from the in-repo `Dockerfile` (005 SC-005 guard intact).
- **Idempotency (FR-006)**: on a healthy re-run → 0 pulls, 0 builds (pin hash + image presence check), no state reset, per-service status printed, exit 0.
- **Config write**: `kb.local.yml` (config-dir-resolved, gitignored) is written **only when absent**; it contains the local endpoints (qdrant/neo4j/llm urls, neo4j user/password from `NEO4J_PASSWORD`). If the file exists with disagreeing service endpoints: print diff + warning, do not overwrite (exit 0).
- **No-GPU host (BR-12.3.4)**: bundled `llm` service is not started; qdrant/neo4j/embedding + digital-twins start; the status table marks `llm: SKIPPED (no suitable GPU — set KB_LLM__ENDPOINT to an external LLM via config or the admin UI)`; exit 0. The embedding-model service (tracked 005 defect, A5) may fail its healthcheck: the script reports it explicitly and keeps the rest of the stack (embedding falls back to the in-process default — `kb.local.yml` leaves `embedding.endpoint` unset).
- **Partial failure**: any non-llm service failing health within the timeout → exit 3, nothing half-configured, `kb.local.yml` not written when the stack is not healthy.
- **Cloud mode (014, `--cloud`)**: no Docker, no port probe, no health poll. Writes `kb.local.yml` with cloud endpoints from `KB_QDRANT__URL` / `KB_NEO4J__URL` / `KB_LLM__ENDPOINT` (unset required values prompted on stdin; optional `KB_EMBEDDING__ENDPOINT`, `KB_NEO4J__USER`, `KB_NEO4J__PASSWORD` written only when set). Missing required value after prompt → exit 5, file left unwritten. `--cloud` and `--status` are mutually exclusive (usage error, exit 1). The Docker prerequisites apply to the default local mode only.
