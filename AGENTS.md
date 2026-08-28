# AGENTS.md — digital-twins

## Source of truth
- `requirement.md` (BR-11, NFR-12..17) governs scope. All owner decisions (Q1–Q10) are locked in §5 — do not re-litigate them in review.
- BR-1..BR-10 / NFR-1..11 live in `docs/business-requirements.md` **outside this repo**. They apply unchanged; do not assume the file is local.

## Workflow
- Work flows through the installed Spec-Kit workflow: `specify → clarify → plan → tasks → implement` (prompts in `.pi/prompts/speckit.*`, scripts in `.specify/`). No ad-hoc coding before the artifacts exist.
- `.specify/memory/constitution.md` is still the unfilled template — fill it (or declare it deliberately open) before the constitution gate starts applying.

## Baseline pipeline (context, not a dependency)
- The job being promoted lives outside this repo: `~/.hermes/skills/hermes/personal-kb/scripts/` + Hermes cron `e4735cf2a2f2` (`0 3 * * *`). Read it for context when implementing.
- BR-11.1.4 absorbs those scripts into the package — shipped code must never reference host paths.

## Host pins that must NOT leak into the package
- `python3.12` pin, `CUDA_VISIBLE_DEVICES=""`, absolute `~/.` source paths: all host-specific. BR-11.2 / NFR-13 — no host path, username, or install location in shipped code, config defaults, or docs. Everything resolves through the config layer at runtime.

## When code exists (add as it lands)
- Build/test commands, package layout, config precedence (`env → kb.local.yml → kb.yml → built-in defaults`).
- Top acceptance check: the same content ingested via schedule, `run --once`, MCP, or web UI yields **one** point, not four (NFR-1, NFR-14).

<!-- SPECKIT START -->
For additional context about technologies to be used, project structure,
shell commands, and other important information, read the current plan
at specs/001-package-foundation/plan.md
<!-- SPECKIT END -->
