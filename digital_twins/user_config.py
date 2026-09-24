"""Per-user config KV + merge (003 multi-user, R5/C-3/SC-003).

``user_config`` is a flat key-value store (data-model C-3): one row per
(account_email, source, key) override.  The overridable keys are the
per-source knobs a user would plausibly tune for "my KB" (R5):
``enabled``, ``max_items``, ``timeout_s``.  Source-identity knobs
(``prefix``/``entrypoint``/``credential``/``email``) are NOT user-
overridable in v1 — ``set_override`` fails fast on them with a
"not a user-overridable knob" error.

Values are stored as TEXT and type-coerced at *merge* time via 001's
``schema.coerce`` (the single type validator, R5).  ``set_override``
pre-validates the value at write time so a malformed value fails fast
rather than poisoning the store.

``merge_user_config`` implements the C-3 merge (pure, at the caller —
serve tick or ``run --as``): start from the resolved global config,
apply the owner's per-source, per-key overrides, and return a **new**
dict.  The input config is never mutated, so two owners' merges are
independent (SC-003 isolation).
"""

from __future__ import annotations

from datetime import datetime, timezone

from .config.schema import coerce, SchemaError

# R5: the only per-source knobs a user may override.
OVERRIDABLE_KEYS: frozenset = frozenset({"enabled", "max_items", "timeout_s"})


class NotUserOverridableError(SchemaError):
    """Raised by ``set_override``/``unset_override`` when ``key`` is not one
    of R5's user-overridable knobs.  A named subclass of ``SchemaError`` so
    callers that catch ``SchemaError`` keep working, while the CLI (T018)
    can surface the terse "not a user-overridable knob" reason.
    """
    pass


def _now() -> str:
    """UTC ISO-8601 timestamp, seconds precision (matches state.models._now)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def set_override(db, account_email: str, source: str, key: str, value) -> None:
    """Insert or update a ``user_config`` row for (account_email, source, key).

    ``key`` must be one of R5's overridable knobs (``enabled``/``max_items``/
    ``timeout_s``); any other key fails fast with
    :class:`NotUserOverridableError` ("not a user-overridable knob").  The
    ``value`` is type-coerced via 001 ``schema.coerce`` at write time
    (fail-fast on a malformed value) and stored as its ``str()`` form.

    The key-restriction check and the coercion happen *before* any write, so
    a rejected call leaves no partial row behind.
    """
    if key not in OVERRIDABLE_KEYS:
        raise NotUserOverridableError(
            f"{key}: not a user-overridable knob "
            f"(overridable: {', '.join(sorted(OVERRIDABLE_KEYS))})"
        )
    # Pre-validate the value at write time (fail-fast, R5): coerce into the
    # knob's declared type; a malformed value raises SchemaError here.
    coerced = coerce(f"sources.{source}.{key}", value)
    # Store in the wire-friendly TEXT form (lowercase for booleans) so the
    # merge-time re-coerce (``coerce`` on the stored TEXT) round-trips.
    # ``str(True)`` is ``"True"``, which ``coerce`` does not accept, so we
    # normalise booleans explicitly.
    stored = "true" if coerced is True else ("false" if coerced is False else str(coerced))
    db.execute(
        "INSERT INTO user_config (account_email, source, key, value, updated_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (account_email, source, key) DO UPDATE SET "
        "value=excluded.value, updated_at=excluded.updated_at",
        (account_email, source, key, stored, _now()),
    )
    db.commit()


def get_overrides(db, account_email: str) -> dict:
    """Return a user's overrides as ``{(source, key): value_text}``.

    The ``value`` is the stored TEXT form (type-coerced at merge time).
    An empty dict is returned when the user has no rows.
    """
    rows = db.execute(
        "SELECT source, key, value FROM user_config WHERE account_email=?",
        (account_email,),
    ).fetchall()
    return {(r[0], r[1]): r[2] for r in rows}


def get_user_channel_overrides(db, user_id: str) -> dict:
    """Return a user's ``channel_overrides`` as ``{source: {key: value}}``.

    This is the per-user channel-override mapping (channels-config 5.1):
    ``{<source_name>: {"enabled": bool, "max_items": int?, "timeout_s": int?}}``.

    The underlying storage is the same ``user_config`` table used by
    :func:`get_overrides` (one row per (account_email, source, key)); this
    function reshapes the flat ``{(source, key): value_text}`` mapping into a
    nested ``{source: {key: value}}`` mapping.  Values are the stored TEXT
    form (type-coerced at merge time, exactly as
    :func:`merge_user_config` does) — one type boundary, no divergence.

    Fail-fast: each stored key is validated against :data:`OVERRIDABLE_KEYS`;
    a key that is not one of the overridable knobs raises
    :class:`NotUserOverridableError` naming the bad key. A user with no
    override rows returns an empty dict.

    Args:
        db: 001 state connection (user_config table, v3).
        user_id: the account_email / owner of the overrides.
    """
    flat = get_overrides(db, user_id)
    nested: dict = {}
    for (source, key), value_text in flat.items():
        if key not in OVERRIDABLE_KEYS:
            # Rows are only ever written with overridable keys (set_override
            # enforces this), so this is a defensive fail-fast: name the bad
            # key rather than silently dropping it.
            raise NotUserOverridableError(
                f"{source}.{key}: not a user-overridable knob "
                f"(overridable: {', '.join(sorted(OVERRIDABLE_KEYS))})"
            )
        nested.setdefault(source, {})[key] = value_text
    return nested




def unset_override(db, account_email: str, source: str, key: str) -> None:
    """Remove the ``user_config`` row for (account_email, source, key).

    A no-op when the row does not exist (defensive: stale key from a
    previously-removed override).
    """
    db.execute(
        "DELETE FROM user_config WHERE account_email=? AND source=? AND key=?",
        (account_email, source, key),
    )
    db.commit()


def merge_user_config(config: dict, db, owner: str, source: str | None = None) -> dict:
    """C-3 merge: return a **new** config with the owner's overrides applied.

    Start from the resolved global ``config`` (001's four-layer ``load()``
    output).  For the owner ``U`` and each source ``S`` (filtered to ``source``
    when given) that has a ``user_config`` row, parse ``value`` into the knob's
    declared type via 001 ``schema.coerce`` and set it on
    ``result["sources"][S][key]``.

    The returned dict is a fresh copy: the input ``config`` is **never
    mutated** (SC-003 isolation), so two owners' merges are independent.

    :raises SchemaError: if a stored value fails to coerce to the knob's
        declared type (should not happen — values are coerced at write time —
        but the merge is the type boundary and must not silently drop a
        malformed value).
    """
    import copy

    result = copy.deepcopy(config)
    result.setdefault("sources", {})

    overrides = get_overrides(db, owner)
    for (src, key), value_text in overrides.items():
        if source is not None and src != source:
            continue
        if key not in OVERRIDABLE_KEYS:
            # Defensive: rows are only ever written with overridable keys.
            continue
        coerced = coerce(f"sources.{src}.{key}", value_text)
        result.setdefault("sources", {}).setdefault(src, {})
        result["sources"][src][key] = coerced
    return result
