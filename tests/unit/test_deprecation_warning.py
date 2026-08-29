"""T016: One-run deprecation warning mechanism.

A deprecated config knob, when read, fires a one-run DeprecationWarning that
names the replacement. The warning fires exactly once per process per
deprecated name — subsequent reads do NOT re-warn.

The test uses a synthetic deprecated key (``synthetic.old_knob``) mapped to a
live knob (``chunking.max_chars``) so it does not deprecate any real shipped
knob.

Each test resets the module-level ``_ALREADY_WARNED`` set so the one-run
semantics can be verified independently per test (the set is process-global,
so without a reset a test running after an earlier test would see zero
warnings).
"""

from __future__ import annotations

import warnings

import pytest

from digital_twins.config import deprecation as dep_mod
from digital_twins.config.deprecation import deprecation_warn

# Synthetic deprecated name (does not collide with any shipped knob).
_OLD = "synthetic.old_knob"
_NEW = "chunking.max_chars"


# --- fixture ----------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_warned_state():
    """Reset the module-level already-warned set before each test.

    The one-run guarantee is per-process, but pytest runs every test in a
    single process, so a test that runs after another test using the same
    name would see zero warnings. Clearing the set before each test makes
    every test start from a fresh-process state.
    """
    dep_mod._ALREADY_WARNED.clear()
    yield
    dep_mod._ALREADY_WARNED.clear()


@pytest.fixture
def synthetic_deprecated_config(tmp_path):
    """A config dict that carries a synthetic deprecated key.

    The deprecated key maps to a real knob so the loader can resolve it, but
    the key itself is synthetic — no shipped knob is deprecated.
    """
    return {
        "synthetic": {"old_knob": 1234},
        "chunking": {"max_chars": 5678},
    }


# --- tests ------------------------------------------------------------------

class TestDeprecationWarn:
    """Unit tests for the deprecation_warn() function."""

    def test_warning_fires_on_first_read(self, synthetic_deprecated_config):
        """Reading a deprecated knob fires a DeprecationWarning."""
        cfg = synthetic_deprecated_config
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            deprecation_warn(_OLD, _NEW)
        assert len(caught) == 1, (
            f"expected 1 warning, got {len(caught)}: {caught}"
        )
        assert issubclass(caught[0].category, DeprecationWarning)

    def test_warning_names_replacement(self, synthetic_deprecated_config):
        """The warning message names the replacement knob."""
        cfg = synthetic_deprecated_config
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            deprecation_warn(_OLD, _NEW)
        assert len(caught) == 1
        msg = str(caught[0].message)
        assert _OLD in msg, f"warning should name the old knob: {msg!r}"
        assert _NEW in msg, f"warning should name the new knob: {msg!r}"

    def test_warning_fires_exactly_once(self, synthetic_deprecated_config):
        """The warning fires at most once per process per deprecated name.

        The first read warns; the second read does NOT re-warn.
        """
        cfg = synthetic_deprecated_config
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            deprecation_warn(_OLD, _NEW)
            deprecation_warn(_OLD, _NEW)  # second read — should NOT warn
        assert len(caught) == 1, (
            f"expected exactly 1 warning (one-run), got {len(caught)}: {caught}"
        )

    def test_distinct_names_each_warn_once(self):
        """Two different deprecated names each fire their own one-run warning."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            deprecation_warn("synthetic.name_a", "chunking.max_chars")
            deprecation_warn("synthetic.name_b", "chunking.max_chars")
            deprecation_warn("synthetic.name_a", "chunking.max_chars")  # no re-warn
            deprecation_warn("synthetic.name_b", "chunking.max_chars")  # no re-warn
        assert len(caught) == 2, (
            f"expected 2 warnings (one per distinct name), got {len(caught)}: {caught}"
        )


class TestLoaderIntegration:
    """Integration: the loader's resolve() fires the warning when it
    encounters a deprecated alias."""

    def test_resolve_fires_warning_for_alias(self, synthetic_deprecated_config):
        """resolve() on a deprecated alias fires the one-run warning.

        Temporarily register a synthetic alias so the loader's resolve path
        is exercised without deprecating any real shipped knob.
        """
        from digital_twins.config import loader

        # Register the synthetic alias (cleanup below restores the empty set).
        original = dict(loader._DEPRECATED_ALIASES)
        loader._DEPRECATED_ALIASES[_OLD] = _NEW
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                value = loader.resolve(_OLD, 1234)
            assert value == 1234, "resolve() must return the value unchanged"
            assert len(caught) == 1, (
                f"expected 1 warning from resolve(), got {len(caught)}: {caught}"
            )
            assert issubclass(caught[0].category, DeprecationWarning)
            msg = str(caught[0].message)
            assert _OLD in msg and _NEW in msg, f"warning must name both: {msg!r}"

            # Second resolve of the same alias — no re-warn.
            with warnings.catch_warnings(record=True) as caught2:
                warnings.simplefilter("always")
                loader.resolve(_OLD, 1234)
            assert len(caught2) == 0, (
                f"expected 0 warnings on second resolve (one-run), "
                f"got {len(caught2)}: {caught2}"
            )
        finally:
            loader._DEPRECATED_ALIASES.clear()
            loader._DEPRECATED_ALIASES.update(original)

    def test_resolve_no_warning_for_non_alias(self):
        """resolve() on a non-aliased path fires no warning and returns the
        value unchanged."""
        from digital_twins.config import loader

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            value = loader.resolve("chunking.max_chars", 800)
        assert value == 800
        assert len(caught) == 0, (
            f"expected 0 warnings for a non-alias, got {len(caught)}: {caught}"
        )
