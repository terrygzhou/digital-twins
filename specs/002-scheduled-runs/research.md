# Research: Scheduled & On-Demand Runs

Phase 0 output. All Technical-Context unknowns resolved; no open items.

## R1 — Scheduler loop implementation
- **Decision**: stdlib only. A single process: main loop ticks every few seconds, resolves due schedules against `next_fire_at`, and runs the 001 pipeline in-process. No APScheduler/celery/new dependency.
- **Rationale**: v1 is single-process per host (A2); the queue is in-memory (A4); the whole loop is "fire due schedules + recompute next fire". A scheduler library adds a dependency and a failure surface for logic stdlib covers in ~100 lines. `serve`'s status HTTP server (`http.server.ThreadingHTTPServer`) shares the process.
- **Alternatives considered**: APScheduler (persistence + job stores, wrong weight for single-process v1); separate worker process (adds IPC for no benefit at v1 scale); asyncio event loop (click + stdlib threading is already the 001 pattern; mixing in an event loop complicates the pipeline's synchronous embedding calls).

## R2 — Status transport
- **Decision**: stdlib `http.server` ThreadingHTTPServer, one JSON route `/status` on `scheduler.status_port` (A3). Disabled when port is 0.
- **Rationale**: BR-11.3.6 requires a poll endpoint; the web UI/framework (003) does not exist yet. One handler class + JSON is the minimum that satisfies "endpoint" and is testable with `http.client`.
- **Alternatives considered**: file-based status (poll a JSON file — not an "endpoint", no auth story); FastAPI (pulls in the web framework early; 003 owns the HTTP surface — ponytail: do not build 003's foundation in 002).

## R3 — Catch-up semantics after downtime
- **Decision**: on startup (serve or run --once), schedules with `next_fire_at` in the past fire **once**, then recompute from the actual fire time. Deterministic IDs + high-water marks make the catch-up run idempotent even if host cron also fired in the gap.
- **Rationale**: BR-11.3.1/NFR-1/NFR-14 — the dedup guarantee is content-based, so catch-up can be aggressive without duplication risk; looping on clock skew is prevented by recomputing `next_fire_at` from *now*, never from the stale stored time (edge case: backwards clock).
- **Alternatives considered**: skip overdue fires entirely (data gap until next cycle — worse); replay a window of missed cycles (unbounded catch-up work — unbounded for long outages).

## R4 — Single-instance guard
- **Decision**: pidfile in `state_dir` (`serve.lock`) + SQLite's own locking. `serve` fails fast at startup if a live pidfile exists; `run --once` needs no guard (stateless, short-lived).
- **Rationale**: A2. SQLite already serializes state writes; the pidfile only gives the operator a clear error instead of two processes racing on fire decisions.
- **Alternatives considered**: OS advisory locks (`fcntl`) — platform-conditional behavior; supervisor-level dedup — that's the operator's systemd/Docker job, not the package's.

## R5 — `--as <user>` credential transport
- **Decision**: password via env var `DT_USER_PASSWORD` (or CLI prompt when interactive); never in argv (no process-list leakage), never logged.
- **Rationale**: FR-9 + host-cron usability (BR-11.3.7 snippet). A2 keeps this to 002; token-based auth arrives with 003's personal tokens.
- **Alternatives considered**: `--password` argv flag (visible in `ps`); credential file (extra surface before 003's config-override model exists).
