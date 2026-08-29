"""User-defined source loader (contracts/source.md rule 5).

A custom source is configured with::

    sources.mytool:
      enabled: true
      entrypoint: "mytool_kb:make_source"   # module:factory
      credential: "MYTOOL_TOKEN"            # env-var name (optional)
      prefix: "mytool:"

The ``entrypoint`` is a Python-importable ``module:factory`` path. The factory
is called with the config entry dict and must return a :class:`Source`.

Import failure, a non-callable/missing factory, a non-Source return, or a
contract violation all raise :class:`CustomSourceError` (fail-fast). If the
entry declares a ``credential`` env var, the returned source is wrapped so
``prerequisites()`` also reports a missing var — matching the built-in
sources' behaviour (rule 3).
"""

from __future__ import annotations

import importlib
import os

from .base import Capability, Source

_REQUIRED_METHODS = ("prerequisites", "read", "close")


class CustomSourceError(Exception):
    """A user-defined source could not be loaded or violates the contract."""


def build_custom(name: str, entry: dict) -> Source:
    """Import the entrypoint, call the factory, validate the contract.

    Args:
        name: source name (from the config key).
        entry: config entry dict with ``entrypoint``, optional ``credential``
            and ``prefix``.

    Returns:
        A Source instance (wrapped so a declared credential is checked in
        ``prerequisites()``).

    Raises:
        CustomSourceError: if import fails, the factory is missing or not
            callable, the factory returns a non-Source, or the contract is
            violated.
    """
    entrypoint = entry.get("entrypoint")
    if not entrypoint or not isinstance(entrypoint, str):
        raise CustomSourceError(
            f"sources.{name}: 'entrypoint' must be a 'module:factory' string, "
            f"got {entrypoint!r}"
        )
    if ":" not in entrypoint:
        raise CustomSourceError(
            f"sources.{name}: 'entrypoint' must be 'module:factory', "
            f"got {entrypoint!r}"
        )

    module_path, factory_name = entrypoint.rsplit(":", 1)
    try:
        module = importlib.import_module(module_path)
    except Exception as exc:
        raise CustomSourceError(
            f"sources.{name}: cannot import module '{module_path}' "
            f"(entrypoint {entrypoint!r}): {exc}"
        ) from exc

    if not hasattr(module, factory_name):
        raise CustomSourceError(
            f"sources.{name}: module '{module_path}' has no attribute "
            f"'{factory_name}' (entrypoint {entrypoint!r})"
        )
    factory = getattr(module, factory_name)
    if not callable(factory):
        raise CustomSourceError(
            f"sources.{name}: '{factory_name}' in '{module_path}' is not callable "
            f"(entrypoint {entrypoint!r})"
        )

    try:
        source = factory(dict(entry, name=name))
    except Exception as exc:
        raise CustomSourceError(
            f"sources.{name}: factory '{module_path}:{factory_name}' raised: {exc}"
        ) from exc

    _validate(source, name, entrypoint)

    # Set the name if the factory left it unset.
    if not getattr(source, "name", None):
        source.name = name

    # Config-level prefix overrides the factory's capability prefix.
    prefix = entry.get("prefix")
    if prefix:
        source.capability.prefix = str(prefix)

    credential = entry.get("credential")
    if credential:
        source = _CredentialGuard(source, str(credential))

    return source


def _validate(source: object, name: str, entrypoint: str) -> None:
    """Verify the factory result fulfils the Source contract."""
    if not isinstance(source, Source):
        raise CustomSourceError(
            f"sources.{name}: factory '{entrypoint}' must return a Source, "
            f"got {type(source).__name__}"
        )
    if getattr(source, "capability", None) is None:
        raise CustomSourceError(
            f"sources.{name}: factory '{entrypoint}' returned a Source without "
            f"a 'capability' (must be a Capability)"
        )
    if not isinstance(source.capability, Capability):
        raise CustomSourceError(
            f"sources.{name}: 'capability' must be a Capability, "
            f"got {type(source.capability).__name__}"
        )
    for method in _REQUIRED_METHODS:
        if not callable(getattr(source, method, None)):
            raise CustomSourceError(
                f"sources.{name}: Source returned by '{entrypoint}' is missing "
                f"a callable '{method}'"
            )


class _CredentialGuard(Source):
    """Wrapper adding a credential env-var check to ``prerequisites()``.

    The custom source's own ``prerequisites()`` is still called; a missing
    credential var is reported in addition. ``read`` and ``close`` are
    delegated unchanged.

    Note: the factory should NOT check its own credential env var; the
    config ``credential`` field is the single source of truth.
    """

    def __init__(self, source: Source, credential: str):
        self._source = source
        self._credential = credential

    @property
    def name(self):
        return self._source.name

    @property
    def capability(self):
        return self._source.capability

    def prerequisites(self) -> list:
        missing = list(self._source.prerequisites())
        if not os.environ.get(self._credential):
            missing.append(
                f"credential env var {self._credential} is not set "
                f"(required by sources.{self.name})"
            )
        return missing

    def read(self, since):
        return self._source.read(since)

    def close(self) -> None:
        self._source.close()
