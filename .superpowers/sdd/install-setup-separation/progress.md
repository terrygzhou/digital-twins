# SDD ledger — plan: openspec/changes/install-setup-separation/tasks.md

Worktree: /home/terry/.local/share/wtm/vaults/digital-twins-a189eb43/install-setup-separation (branch wtm/install-setup-separation)
Plan = OpenSpec 4-file set (proposal/design/spec/tasks). Spec = openspec/changes/install-setup-separation/specs/install-setup/spec.md.
MERGE_BASE = 5e85ef7 (turnsnap turn 3). WIP commit = 4973baf (resolve_backends T1.1 + RED CLI tests, imported from main's dirty tree).

## Preflight scan

Checked:
- T1.2 vs T1.3: both modify setup.py; T1.2 wires CLI flags + resolve_backends into run_setup, T1.3 builds up_services from the resolved map inside run_local_stack. T1.3 consumes T1.2's resolved map. No conflict; T1.3 is a consumer.
- T1.2 vs T2: T1.2 is deterministic (flags/env); T2 is the interactive second pass on the default detection path only. Spec scenario "Explicit flags suppress the interactive second pass" pins the boundary. No conflict.
- T3.4 vs T3.1-3.3: T3.4 is the doc-contract test delta (installer tests); T3.1-3.3 are the script changes. Same file (tests) only in T3.4. Clean.
- T4.1 vs T4.2: T4.1 rewrites init (cli.py L482 area), T4.2 is the remediation-string sweep across 4 files + test_auth.py L359. Both touch cli.py but different line ranges. Clean.
- T5.1 vs T5.2: README vs docs/configuration.md, different files. Clean.
- T6.1/T6.2: verification only; 6.2 is manual (fresh venv + pip install) — needs Docker/GPU? No: --backends mixed path with external URL needs no docker. Ruling: 6.2 is runnable in this env if pip install of the sdist works; treat as best-effort, not a gate.
- T3.x scripts + T5.1 README: both touch installer behavior. README rewrite (T5.1) depends on T3.1's new flags (--with-setup/--run-ingest). T5.1 must run AFTER T3.x. Sequence: 3.1 → 3.2 → 3.3 → 3.4 → 5.1.
- WIP defect list (from 4973baf): duplicated _HEALTH_TIMEOUT_S/_POLLEVERY_S block in setup.py L76-79; missing parse_backends; cli.py lacks --backends/--local; test_setup_backends_cli.py L68-70 has a stray Path NameError + dead code. Ruling: T1.2 dispatch owns ALL of these fixes (they are inside T1.2's scope per spec scenario "All-external, non-interactive").
- T4.1 "migrate merge-on-existing into setup's write path": spec says init keeps its own contract, setup gains merge-on-existing in the write path. Ruling: setup's write path = write_kb_local's "leave existing file alone" rule changes to merge-on-existing ONLY when init invokes it; setup standalone keeps "leave an existing file alone" (re-run no-op, spec "Re-run is a no-op" scenario). The two commands agree on merge semantics via a shared helper.
- T4.2 line refs (L1073 etc.): these were captured on the current main tree; implementer must re-locate by string, not line number, since T4.1 shifts lines. Ruling: dispatch says "re-locate by the quoted strings, line numbers are hints only".
- 14 pre-existing S4 red tests (test_mcp_registry_schemas.py / test_web_app_kb_kb.py): unrelated, fail on parent commit. Not a gate for this plan; note them but do not chase them.
- T6.2 "fresh venv → pip install digital-twins-kb[mcp]": the dist name on PyPI is digital-twins-kb (user's external upload). For local verification, pip install . from the worktree is the equivalent. Ruling: 6.2 local verification = pip install . into a fresh venv.

## Bindings (global constraints copied for reviewers)
- tests/integration/test_portability.py (T006) + tests/unit/test_knob_docs.py (T027) must stay green.
- No host paths / usernames / interpreter pins in shipped code (NFR-13 / BR-11).
- docker-compose.yml pinned tags (qdrant/qdrant:1.9.7, neo4j/neo4j:5.18-community) must not change.
- Test-First constitution: new behavior ships with tests written/updated first (RED).
- kb.local.yml write: single trailing newline, byte-exact.

## T1.2 execution log
- 4973baf (WIP import): resolve_backends + _SERVICE_ENV in setup.py; commit message OVERSTATED — the two test files it claims to import were NOT actually committed (git commit -am doesn't add untracked; they were lost when main's dirty tree was cleaned). install.md also lost.
- 699d0de: "T1.2 prep" commit by first subagent — tree is byte-identical to 4973baf (commit is an empty no-op; dedup NOT done).
- Subagent b1c9635f (round 1) BLOCKED: its session sandbox pins workspace-write to the main checkout, the wtm worktree is outside that, and approval prompts are disabled in subagent sessions → cannot escalate to danger-full-access. Built a mirror + patches but that session was interrupted; its artifacts (mirror git refs 0b61c7a/11344a8, patch files) are unrecoverable (mirror checkout was deleted; its git refs never entered any repo I control — its "mirror" commits were actually created inside the main repo's shared object store? No: the T12-mirror checkout is a linked worktree of the MAIN repo; its branches only contained the main branch tip. The subagent's 0b61c7a/11344a8 were made in a DIFFERENT git repo that no longer exists).
- Ruling: RE-IMPORT the lost test files + install.md into the worktree as a new commit (reconstructed from session-turn artifacts — I hold the full text of both test files from this session's reads). Then re-dispatch T1.2 with the patch-handoff model (subagent produces patch + test-evidence in the main checkout; I apply in the worktree).
- Ruling (dedup): 699d0de's claim of a dedup is FALSE (tree identical to 4973baf, dup block still present at L71-79). Dedup remains open work for T1.2.

## T1.2 re-dispatch (corrected) — 2026-08-24

- **Lost-file incident resolved:** the two RED test files were permanently lost
  from git (`git commit -am` in the prior session's WIP commit did not add
  untracked files). Reconstructed from full content read earlier this session
  into `reconstructed-tests/`, copied into the wtm worktree, and committed as
  `5abb940` "Restore lost T1.1/T1.2 test files ... reconstructed".
- **Sandbox constraint (hard, cannot be escalated for subagents):** the wtm
  worktree lives at `/home/terry/.local/share/wtm/vaults/digital-twins-a189eb43/install-setup-separation` — outside the
  session workspace. Subagent sessions have approval prompts disabled, so no
  escalation is possible from within a subagent. The top-level session CAN
  escalate once (danger-full-access, approved once already).
- **Model for T1.2 dispatch:** create an inner git worktree at
  `.superpowers/sdd/install-setup-separation/t12-impl/` (inside the session
  workspace, subagent-writable) on branch `t12-impl`, reset to the wtm tip
  `5abb940`. The subagent implements T1.2+T1.3 + dedup in that inner worktree
  and emits a `git diff` patch. The top-level session applies the patch to the
  wtm worktree (with a one-shot danger-full-access escalation), commits there,
  and runs the pytest gate. The task reviewer then gets a review-package from
  the wtm branch.
- **Contamination fix:** `task-1.2-report.md` had a false "T2 review-response
  fix" tail (mirror commits 0b61c7a/11344a8 that do not exist anywhere).
  Truncated to L56 — now contains only the real WIP state + dispatch binding
  constraints. T2 will be dispatched fresh later; nothing about T2 is
  settled yet.

## T1.2 + T1.3 complete — 2026-08-24

- Commits on `wtm/install-setup-separation`:
  - `a7d8c60` — de-duplicate `_HEALTH_TIMEOUT_S/_POLLEVERY_S` block (redo; 699d0de was a no-op false claim)
  - `3bc1b41` — T1.2+T1.3: `--backends`/`--local` CLI, `parse_backends()`, `run_local_stack(resolved=...)`, `run_setup(local=, backends=)` with documented precedence
  - `8765d57` — mark 1.2/1.3 complete in tasks.md
- Reviewer: PASS (1 nit: unused `key` loop variable in dropped-service remediation loop setup.py L535-541 — non-blocking, will sweep later if needed).
- Gate: 46/46 (35 test_setup + 7 test_resolve_backends + 4 test_setup_backends_cli); portability/knob_docs 25/25; full unit suite 858 passed (1 pre-existing S4-red in test_web_app_kb.py).
- Review file: `.superpowers/sdd/install-setup-separation/task-1.2-review.md`.

## T2 complete — 2026-08-24

- Commit on `wtm/install-setup-separation`: `384ce72` (T2: interactive second pass —
  per-service local/external prompts), + `584ef25` tasks.md mark.
- Reviewer: PASS (1 minor: dead `named` param at sole call site; 1 nit: unused
  `**kw` in a test fake — both non-blocking).
- Gate: 74/74 (46 T1 baseline + 3 new second-pass + portability/knob_docs).
- Review file: `.superpowers/sdd/install-setup-separation/task-2-review.md`.
- Next: T3.1–T3.4 (installer no-op `--no-setup` + `--with-setup` + doc-contract delta).
