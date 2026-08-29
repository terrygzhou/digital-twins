"""One-run deprecation warning mechanism (T017).

A deprecated config knob, when read, fires a one-run ``DeprecationWarning``
that names the replacement knob. Each deprecated name warns at most once per
process: a module-level ``_ALREADY_WARNED`` set records which names have
already fired, so subsequent reads of the same name do NOT re-warn.

The warning is a mechanism, not a new knob — no schema / knob-registry entry
is added. It is host-neutral: no host path, username, or install location
appears in the message.

Intended use from the config loader: when a deprecated alias is encountered
during a read, call :func:`deprecation_warn(old_name, new_name)` before
resolving to the new value.
"""

from __future__ import annotations

import warnings

# Names that have already fired their one-run warning, for this process.
# A module-level set so the "once per process" guarantee holds across
# repeated reads of the same deprecated knob.
_ALREADY_WARNED: set[str] = set()


def deprecation_warn(old_name: str, new_name: str) -> None:
    """Fire a one-run ``DeprecationWarning`` for ``old_name`` -> ``new_name``.

    The warning message names both the deprecated knob and its replacement::

        DeprecationWarning: <old_name> is deprecated; use <new_name> instead.

    Each ``old_name`` fires at most once per process: on the first call the
    warning is emitted and the name is recorded in ``_ALREADY_WARNED``; on
    every subsequent call with the same ``old_name`` the function returns
    without emitting anything (no re-warn).

    Args:
        old_name: the deprecated knob / alias (dotted path).
        new_name: the replacement knob / command it maps to.
    """
    if old_name in _ALREADY_WARNED:
        return
    _ALREADY_WARNED.add(old_name)
    warnings.warn(
        f"{old_name} is deprecated; use {new_name} instead.",
        DeprecationWarning,
        stacklevel=2,
    )
