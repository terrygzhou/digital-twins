"""Serve loop: due-schedule detection, pipeline fire, pidfile guard (002).

std loop, tick ~3 s; in-process queue (R1, A4).

``serve_once_tick`` (T006) is one pass: due_schedules -> run 001 pipeline
per schedule (shared state, trigger='schedule', scheduled_by=schedule owner,
source=schedule.source) -> claim_and_advance. Per-source caps/timeouts are
read fresh from ``config`` on every tick (no caching): the caller (run_serve,
T007) re-loads config each tick, so cap changes are honored without restart
(SC-005).

Error handling (002 ruling R-07, "reported, never silent"):
    A source prerequisite failure (001 ``PrerequisiteError``) or any other
    unexpected pipeline exception for a due schedule writes a ``failed``
    audit row for that run (run_id = fresh uuid4 hex, trigger='schedule',
    scheduled_by=owner, per_source_counts {source: 0}) and STILL advances the
    schedule via claim_and_advance, so it is not re-fired on the next tick.
    The schedule id lands in ``fired``: the fire WAS processed; the run
    failed but was audited.

``run_serve`` (T007) is still red; the import pin in
``tests/unit/test_scheduler_imports.py`` expects both names.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from digital_twins.ingest.pipeline import run_pipeline
from digital_twins.scheduler.schedules import claim_and_advance, due_schedules
from digital_twins.state.models import finish_audit_run, start_audit_run


def serve_once_tick(db, config, qdrant=None, embedder=None,
                    neo4j=None) -> dict:
    """One serve tick: fire the due schedules, audit, and advance.

    Args:
        db: 001 state connection (schedules + audit_runs tables, v2).
        config: the 001 config dict, read fresh on every call (no caching).
        qdrant: client or zero-arg factory, passed through to run_pipeline.
        embedder: list[str] -> list of vectors, passed through.
        neo4j: driver-shaped object (or None), passed through.

    Returns:
        ``{"fired": [schedule id...], "skipped": [schedule id...],
        "queue_depth": int}`` — see module docstring for the semantics.
    """
    now = datetime.now(timezone.utc)
    due = due_schedules(db, now)

    fired: list[int] = []
    skipped: list[int] = []

    for schedule in due:
        source = schedule["source"]
        entry = config["sources"].get(source)
        if entry is None or not entry.get("enabled"):
            # Paused source: do NOT fire-and-advance. No audit row (nothing
            # ran); next_fire_at stays put so the schedule is still due on
            # the next tick and fires when the source is re-enabled.
            skipped.append(schedule["id"])
            continue

        fired_at = now
        rows_before = _audit_count(db)
        try:
            # The pipeline writes its own audit row (trigger + scheduled_by
            # passed through); success or not, advance the schedule.
            run_pipeline(
                config, db, qdrant, embedder,
                source_names=[source],
                trigger="schedule",
                scheduled_by=schedule["owner"],
            )
        except Exception:
            # R-07: a failed fire is audited and advanced — never silent,
            # never re-fired. run_pipeline writes its own `failed` audit row
            # (start_audit_run up front + finish on the exception path); if
            # the run died before any audit row was started, backstop with a
            # fresh `failed` row so the fire is always reported.
            if _audit_count(db) == rows_before:
                _audit_failed_run(db, source, schedule["owner"])
        claim_and_advance(db, schedule["id"], fired_at)
        fired.append(schedule["id"])

    # v1 does not interrupt the tick: every due schedule is either fired
    # or skipped above, so queue_depth is normally 0. It is > 0 only if a
    # non-prerequisite error aborted the whole tick mid-loop.
    queue_depth = len(due) - len(fired) - len(skipped)

    return {"fired": fired, "skipped": skipped, "queue_depth": queue_depth}


def _audit_count(db) -> int:
    """Number of audit_runs rows (to detect whether a run started its row)."""
    return db.execute("SELECT COUNT(*) FROM audit_runs").fetchone()[0]


def _audit_failed_run(db, source: str, owner: str) -> None:
    """Write a ``failed`` audit row for a schedule fire whose run raised.

    run_id is a fresh uuid4 hex (no reuse of the pipeline's run id: the
    pipeline may have died before starting its own audit row, and we must
    not collide with a row it did start).
    """
    run_id = uuid.uuid4().hex
    start_audit_run(db, run_id, trigger="schedule", scheduled_by=owner)
    finish_audit_run(db, run_id, "failed", {source: 0})
