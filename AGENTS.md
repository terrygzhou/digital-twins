# AGENTS.md — digital-twins

## Source of truth
- `requirement.md` (BR-11, NFR-12..17) governs scope. All owner decisions (Q1–Q10) are locked in §5 — do not re-litigate them in review.
- BR-1..BR-10 / NFR-1..11 live in `docs/business-requirements.md` **outside this repo**. They apply unchanged; do not assume the file is local.

## Workflow
- Work flows through the installed Spec-Kit workflow: `specify → clarify → plan → tasks → implement` (prompts in `.pi/prompts/speckit.*`, scripts in `.specify/`). No ad-hoc coding before the artifacts exist.
- `.specify/memory/constitution.md` is filled and ratified (v1.0.0, six principles incl. Test-First) — the constitution gate applies. T028–T040 are complete; `specs/001-package-foundation/tasks.md` checkboxes should be kept in sync with commits (the SDD ledger in `.superpowers/sdd/tasks-md/progress.md` is the detailed record).

## Baseline pipeline (context, not a dependency)
- The job being promoted lives outside this repo: `~/.hermes/skills/hermes/personal-kb/scripts/` + Hermes cron `e4735cf2a2f2` (`0 3 * * *`). Read it for context when implementing.
- BR-11.1.4 absorbs those scripts into the package — shipped code must never reference host paths.

## Host pins that must NOT leak into the package
- `python3.12` pin, `CUDA_VISIBLE_DEVICES=""`, absolute `~/.` source paths: all host-specific. BR-11.2 / NFR-13 — no host path, username, or install location in shipped code, config defaults, or docs. Everything resolves through the config layer at runtime.

## Package & build/test (0.1.0)
- Python package `digital_twins/`: `config/` (schema, loader, knobs), `sources/` (base + fs/hermes/pi/dsh/paperclip/imap_mail/custom), `ingest/` (ids, chunking, embedding, pipeline), `state/` (db, models, migrations), `health.py`, `cli.py`, `__main__.py`.
- Install/build: `pip install .` (hatchling); console script `digital-twins`; also `python -m digital_twins`.
- Test: `pytest` (unit in `tests/unit/`, integration in `tests/integration/`; 163 tests at v0.1.0, 165 at v0.1.1). Standing guards: `tests/integration/test_portability.py` (T006) and `tests/unit/test_knob_docs.py` (T027) must stay green.
- Config precedence (deterministic): `env (incl. .env) → kb.local.yml → kb.yml → built-in defaults` (env wins; `KB_` prefix, `__` = nesting).
- Version: `__version__` single-sourced in `digital_twins/__init__.py`; `digital-twins --version` / `--version-json`.

## Top acceptance check
- The same content ingested via schedule, `run --once`, MCP, or web UI yields **one** point, not four (NFR-1, NFR-14).

<!-- SPECKIT START -->
For additional context about technologies to be used, project structure,
shell commands, and other important information, read the current plan
at specs/001-package-foundation/tasks.md
<!-- SPECKIT END -->
