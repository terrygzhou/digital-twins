# 014 — bootstrap cloud mode (spec-lite)

Owner feedback 2026-09-08: containers are not necessary to bootstrap the
digital-twins server — users may run the services in the cloud and point
the package at cloud endpoints instead of local containers. The bootstrap
script, however, had exactly one path: the local Docker stack.

## Scope

`scripts/bootstrap-local.sh` gains a `--cloud` mode:

- Skips every container step (no `docker_ok`, no port probe, no compose,
  no GPU probe, no health poll) — pinned: the exec log records zero
  `docker`/`ss`/`netstat`/`curl`/`nvidia-smi` calls.
- Writes `kb.local.yml` with cloud endpoints:
  required `KB_QDRANT__URL`, `KB_NEO4J__URL`, `KB_LLM__ENDPOINT`
  (unset → prompt on stdin), optional `KB_EMBEDDING__ENDPOINT`,
  `KB_NEO4J__USER`, `KB_NEO4J__PASSWORD` (written only when set).
- New exit 5: a required cloud endpoint is still missing after the
  prompt (variable named, file left unwritten).
- `--cloud` + `--status` are mutually exclusive (usage error, exit 1).
- `--help` advertises `--cloud`, the env vars, exit 5, and states that
  the Docker prerequisites are local-mode only.

Contract: `specs/008-service-hosting/contracts/bootstrap-cli.md` (updated
in the same commit). Host-neutral (NFR-13): example URLs are `*.example`.
bash-3.2 safe (no `declare -A`/`mapfile`/`&>`), pinned by the existing
guard.

## Tests (RED first)

`tests/unit/test_bootstrap_script.py` — six new cases:
cloud happy-path (exact `kb.local.yml` content + zero tooling calls),
optional-key omission, exit-5 missing endpoints, stdin prompts,
`--cloud`/`--status` conflict, `--help` cloud documentation.

## Status

Complete — commit T11 below; guards (`test_portability.py`,
`test_knob_docs.py`) green; full suite 985+ passed.
