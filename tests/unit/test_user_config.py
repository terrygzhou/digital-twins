"""Red tests for 003 multi-user: user_config KV + merge (T015, R5/SC-003).

Covers the four functions in ``digital_twins/user_config.py``:
- ``set_override``  — insert-or-update a row; key restricted to R5's
  overridable knobs; value type-coerced via 001 ``schema.coerce`` at
  write time (fail-fast on a malformed value or a non-overridable key).
- ``get_overrides`` — read back a user's overrides as a dict.
- ``unset_override`` — remove one row.
- ``merge_user_config`` — the C-3 merge: start from the resolved global
  config, apply the owner's per-source, per-key overrides, and return a
  **new** dict (never mutate the input).

SC-003 isolation: alice's ``hermes.max_items=50`` must differ from
bob's merged config *only* on that key; in **both orderings**
(alice-then-bob, bob-then-alice) one user's cap change never alters the
other's; the input config dict is not mutated.
"""

import copy

import pytest

from digital_twins import user_config
from digital_twins.config import load, get
from digital_twins.config.schema import SchemaError
from digital_twins.state.db import connect
from digital_twins.state.migrations import migrate


@pytest.fixture()
def db(tmp_path):
    """A migrated (v3) state DB with the user_config table."""
    conn = connect(tmp_path)
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture()
def global_cfg(tmp_path):
    """A resolved global config (001's four-layer ``load()`` output)."""
    return load(cwd=tmp_path, config_dir=tmp_path)


# --- set_override -----------------------------------------------------------

def test_set_override_inserts(db, global_cfg):
    user_config.set_override(db, "alice@example.com", "hermes", "max_items", 50)
    row = db.execute(
        "SELECT source, key, value FROM user_config "
        "WHERE account_email=?",
        ("alice@example.com",),
    ).fetchone()
    assert row is not None
    assert row[0] == "hermes"
    assert row[1] == "max_items"
    # value is stored as TEXT and type-coerced at write time
    assert row[2] == "50"


def test_set_override_type_coerces_str_to_int(db):
    user_config.set_override(db, "alice@example.com", "hermes", "max_items", "42")
    row = db.execute(
        "SELECT value FROM user_config WHERE account_email=? AND key='max_items'",
        ("alice@example.com",),
    ).fetchone()
    assert row[0] == "42"


def test_set_override_bool_coerced(db):
    user_config.set_override(db, "alice@example.com", "hermes", "enabled", "true")
    row = db.execute(
        "SELECT value FROM user_config WHERE account_email=? AND key='enabled'",
        ("alice@example.com",),
    ).fetchone()
    assert row[0] == "true"


def test_set_override_upserts_same_row(db):
    user_config.set_override(db, "u@example.com", "pi", "timeout_s", 100)
    user_config.set_override(db, "u@example.com", "pi", "timeout_s", 200)
    rows = db.execute(
        "SELECT value FROM user_config WHERE account_email=? AND key='timeout_s'",
        ("u@example.com",),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "200"


def test_set_override_rejects_non_overridable_key(db):
    with pytest.raises(SchemaError) as exc:
        user_config.set_override(db, "alice@example.com", "hermes", "prefix", "x:")
    assert "not a user-overridable knob" in str(exc.value)


def test_set_override_rejects_malformed_value(db):
    with pytest.raises(SchemaError):
        user_config.set_override(db, "alice@example.com", "hermes", "max_items", "abc")


def test_set_override_does_not_mutate_on_reject(db):
    # A rejected key must leave no row behind.
    with pytest.raises(SchemaError):
        user_config.set_override(db, "alice@example.com", "hermes", "prefix", "x:")
    rows = db.execute(
        "SELECT COUNT(*) FROM user_config WHERE account_email=?",
        ("alice@example.com",),
    ).fetchone()[0]
    assert rows == 0


# --- get_overrides ----------------------------------------------------------

def test_get_overrides_returns_dict(db):
    user_config.set_override(db, "alice@example.com", "hermes", "max_items", 50)
    user_config.set_override(db, "alice@example.com", "pi", "enabled", "false")
    ov = user_config.get_overrides(db, "alice@example.com")
    assert ov == {
        ("hermes", "max_items"): "50",
        ("pi", "enabled"): "false",
    }


def test_get_overrides_empty_when_no_rows(db):
    assert user_config.get_overrides(db, "nobody@example.com") == {}


# --- unset_override ---------------------------------------------------------

def test_unset_override_removes_row(db):
    user_config.set_override(db, "alice@example.com", "hermes", "max_items", 50)
    user_config.unset_override(db, "alice@example.com", "hermes", "max_items")
    assert user_config.get_overrides(db, "alice@example.com") == {}


def test_unset_override_is_idempotent(db):
    # No row present: must not raise.
    user_config.unset_override(db, "alice@example.com", "hermes", "max_items")
    assert user_config.get_overrides(db, "alice@example.com") == {}


# --- merge_user_config: C-3 precedence + SC-003 isolation -------------------

def test_merge_applies_alice_override_not_bob(db, global_cfg):
    user_config.set_override(db, "alice@example.com", "hermes", "max_items", 50)
    alice_cfg = user_config.merge_user_config(global_cfg, db, "alice@example.com")
    bob_cfg = user_config.merge_user_config(global_cfg, db, "bob@example.com")
    # alice: hermes cap is 50
    assert get(alice_cfg, "sources.hermes.max_items") == 50
    # bob: global cap (default 200), unaffected by alice's row
    assert get(bob_cfg, "sources.hermes.max_items") == 200


def test_merge_differs_only_on_overridden_key(db, global_cfg):
    """alice's merge differs from bob's merge *only* on hermes.max_items."""
    user_config.set_override(db, "alice@example.com", "hermes", "max_items", 50)
    alice_cfg = user_config.merge_user_config(global_cfg, db, "alice@example.com")
    bob_cfg = user_config.merge_user_config(global_cfg, db, "bob@example.com")
    # Every non-overridden key is identical between the two merged configs.
    for source in global_cfg.get("sources", {}):
        for key in ("enabled", "max_items", "timeout_s"):
            if source == "hermes" and key == "max_items":
                continue
            a = get(alice_cfg, f"sources.{source}.{key}")
            b = get(bob_cfg, f"sources.{source}.{key}")
            assert a == b, f"unexpected diff on {source}.{key}: {a} != {b}"


def test_merge_bob_equals_global(db, global_cfg):
    """bob's merged config is the global config (he has no overrides)."""
    user_config.set_override(db, "alice@example.com", "hermes", "max_items", 50)
    bob_cfg = user_config.merge_user_config(global_cfg, db, "bob@example.com")
    for source in global_cfg.get("sources", {}):
        for key in ("enabled", "max_items", "timeout_s"):
            assert get(bob_cfg, f"sources.{source}.{key}") == \
                get(global_cfg, f"sources.{source}.{key}")


def test_merge_does_not_mutate_input(db, global_cfg):
    """The input config dict is not mutated (SC-003 isolation)."""
    snapshot = copy.deepcopy(global_cfg)
    user_config.set_override(db, "alice@example.com", "hermes", "max_items", 50)
    user_config.merge_user_config(global_cfg, db, "alice@example.com")
    assert global_cfg == snapshot


def test_merge_returns_new_dict(db, global_cfg):
    user_config.set_override(db, "alice@example.com", "hermes", "max_items", 50)
    out = user_config.merge_user_config(global_cfg, db, "alice@example.com")
    assert out is not global_cfg


# --- get_user_channel_overrides (channels-config 5.1) -----------------------

def test_channel_overrides_round_trip(db):
    """set_override -> get_user_channel_overrides round-trips the mapping.

    Values come back as the stored TEXT form (the single type boundary is
    the merge-time ``coerce`` in ``merge_user_config``), nested by source.
    """
    user_config.set_override(db, "alice@example.com", "hermes", "max_items", 50)
    user_config.set_override(db, "alice@example.com", "hermes", "enabled", "false")
    user_config.set_override(db, "alice@example.com", "pi", "timeout_s", 30)
    ov = user_config.get_user_channel_overrides(db, "alice@example.com")
    assert ov == {
        "hermes": {"max_items": "50", "enabled": "false"},
        "pi": {"timeout_s": "30"},
    }


def test_channel_overrides_values_are_stored_text(db):
    """Values are the stored TEXT form, not coerced types (single type
    boundary at merge time, mirroring ``get_overrides``)."""
    user_config.set_override(db, "a@example.com", "fs", "max_items", 42)
    user_config.set_override(db, "a@example.com", "fs", "enabled", "true")
    ov = user_config.get_user_channel_overrides(db, "a@example.com")
    assert ov["fs"]["max_items"] == "42"
    assert ov["fs"]["enabled"] == "true"
    assert isinstance(ov["fs"]["max_items"], str)


def test_channel_overrides_round_trip_then_merge_typed(db, global_cfg):
    """The read mapping feeds ``merge_user_config``: re-coercing the stored
    TEXT yields the typed values the pipeline sees."""
    user_config.set_override(db, "alice@example.com", "hermes", "max_items", 50)
    user_config.set_override(db, "alice@example.com", "hermes", "enabled", "false")
    ov = user_config.get_user_channel_overrides(db, "alice@example.com")
    merged = user_config.merge_user_config(global_cfg, db, "alice@example.com")
    assert get(merged, "sources.hermes.max_items") == 50
    assert get(merged, "sources.hermes.enabled") is False


def test_channel_overrides_empty_when_no_rows(db):
    """A user with no override rows -> empty dict (not None)."""
    assert user_config.get_user_channel_overrides(db, "nobody@example.com") == {}


def test_channel_overrides_mixed_keys_round_trip(db):
    """Multiple sources with a mix of key subsets round-trip exactly."""
    user_config.set_override(db, "u@example.com", "fs", "enabled", "true")
    user_config.set_override(db, "u@example.com", "fs", "max_items", 7)
    user_config.set_override(db, "u@example.com", "fs", "timeout_s", 1)
    user_config.set_override(db, "u@example.com", "gmail", "enabled", "false")
    ov = user_config.get_user_channel_overrides(db, "u@example.com")
    assert ov == {
        "fs": {"enabled": "true", "max_items": "7", "timeout_s": "1"},
        "gmail": {"enabled": "false"},
    }


def test_channel_overrides_fail_fast_on_unknown_key(db):
    """A row carrying a key outside OVERRIDABLE_KEYS fails fast, naming it.

    set_override already refuses such keys at write time (the normal
    path); this pins the READ-side guard: a foreign row (e.g. written by an
    older version with a different key set) must not be silently dropped.
    """
    db.execute(
        "INSERT INTO user_config (account_email, source, key, value, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("w@x.com", "hermes", "prefix", "x:", "2026-01-01T00:00:00+00:00"),
    )
    db.commit()
    with pytest.raises(SchemaError) as exc:
        user_config.get_user_channel_overrides(db, "w@x.com")
    assert "prefix" in str(exc.value)
    assert "not a user-overridable knob" in str(exc.value)



def test_channel_overrides_returns_fresh_dict(db):
    """Each call returns a fresh nested dict: mutating it does not affect
    subsequent reads (the stored rows are untouched)."""
    user_config.set_override(db, "a@example.com", "fs", "max_items", 9)
    ov = user_config.get_user_channel_overrides(db, "a@example.com")
    ov["fs"]["max_items"] = "999"
    # Re-reading yields the original stored value ("9"), not the mutated one.
    again = user_config.get_user_channel_overrides(db, "a@example.com")
    assert again == {"fs": {"max_items": "9"}}


# --- SC-003 isolation: both orderings ---------------------------------------

def _diff_keys(a: dict, b: dict) -> set:
    """Return the set of (source, key) tuples where a and b differ on R5 knobs."""
    diffs = set()
    sources = set(a.get("sources", {})) | set(b.get("sources", {}))
    for source in sources:
        for key in ("enabled", "max_items", "timeout_s"):
            pa = f"sources.{source}.{key}"
            if get(a, pa) != get(b, pa):
                diffs.add((source, key))
    return diffs


def test_ordering_alice_then_bob(db, global_cfg):
    """alice sets her cap first, then bob merges: bob is the global."""
    user_config.set_override(db, "alice@example.com", "hermes", "max_items", 50)
    alice_cfg = user_config.merge_user_config(global_cfg, db, "alice@example.com")
    bob_cfg = user_config.merge_user_config(global_cfg, db, "bob@example.com")
    assert get(alice_cfg, "sources.hermes.max_items") == 50
    assert get(bob_cfg, "sources.hermes.max_items") == 200
    # The two configs differ *only* on hermes.max_items.
    assert _diff_keys(alice_cfg, bob_cfg) == {("hermes", "max_items")}


def test_ordering_bob_then_alice(db, global_cfg):
    """bob sets his cap first, then alice merges: alice is the global."""
    user_config.set_override(db, "bob@example.com", "hermes", "max_items", 30)
    bob_cfg = user_config.merge_user_config(global_cfg, db, "bob@example.com")
    alice_cfg = user_config.merge_user_config(global_cfg, db, "alice@example.com")
    assert get(bob_cfg, "sources.hermes.max_items") == 30
    assert get(alice_cfg, "sources.hermes.max_items") == 200
    assert _diff_keys(alice_cfg, bob_cfg) == {("hermes", "max_items")}


def test_one_users_cap_never_alters_the_others_both_orderings(db, global_cfg):
    """Setting one user's cap must never alter the other user's merged cap.

    Covers both orderings in one test: alice's cap change, then bob's, then
    re-merge both and confirm each user still sees only their own cap.
    """
    user_config.set_override(db, "alice@example.com", "hermes", "max_items", 50)
    user_config.set_override(db, "bob@example.com", "hermes", "max_items", 30)
    alice_cfg = user_config.merge_user_config(global_cfg, db, "alice@example.com")
    bob_cfg = user_config.merge_user_config(global_cfg, db, "bob@example.com")
    # Each user sees exactly their own cap, the other's row is ignored.
    assert get(alice_cfg, "sources.hermes.max_items") == 50
    assert get(bob_cfg, "sources.hermes.max_items") == 30


def test_merge_source_filter(db, global_cfg):
    """source= filters the merge to that one source."""
    user_config.set_override(db, "alice@example.com", "hermes", "max_items", 50)
    user_config.set_override(db, "alice@example.com", "pi", "timeout_s", 1)
    out = user_config.merge_user_config(
        global_cfg, db, "alice@example.com", source="hermes"
    )
    assert get(out, "sources.hermes.max_items") == 50
    # The pi override is NOT applied when source=hermes.
    assert get(out, "sources.pi.timeout_s") == get(
        global_cfg, "sources.pi.timeout_s")
