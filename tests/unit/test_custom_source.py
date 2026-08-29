"""Custom (user-defined) source loader tests (T035, US5).

The loader (T036) imports a config `entrypoint` (`module:factory`), calls
the factory, validates the Source contract, and wraps the result so a
declared `credential` env var is checked in `prerequisites()`.

Fake modules are injected via `sys.modules` so no real files are needed.
"""

import sys
import types

import pytest

from digital_twins.sources import build, CustomSourceError
from digital_twins.sources.base import Capability, IngestItem, Source
from digital_twins.sources import custom as custom_mod


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

class FakeSource(Source):
    """A minimal contract-compliant source used by the fake factory."""

    def __init__(self, name: str, entry: dict):
        self.name = name
        self.capability = Capability(runtime=None, credential=None, prefix=f"{name}:")

    def prerequisites(self):
        return []

    def read(self, since):
        yield IngestItem(key="k1", content="fake", ts="2025-01-01T00:00:00+00:00")

    def close(self):
        pass


def _make_fake_module(name, factory):
    """Build a fake module object and register it in sys.modules."""
    mod = types.ModuleType(name)
    mod.factory = factory
    sys.modules[name] = mod
    return mod


@pytest.fixture(autouse=True)
def _cleanup_fake_modules():
    """Remove any fake module injected by a test so it can't leak."""
    yield
    for m in ("fake_module", "nonexistent_module"):
        sys.modules.pop(m, None)


# --------------------------------------------------------------------------- #
# 1. valid entrypoint imports and returns a Source
# --------------------------------------------------------------------------- #

def test_valid_entrypoint_returns_source():
    _make_fake_module("fake_module", lambda entry: FakeSource(entry.get("name", "x"), entry))
    entry = {"enabled": True, "entrypoint": "fake_module:factory", "prefix": "fake:"}
    source = build("mytool", entry)
    assert isinstance(source, Source)
    assert source.name == "mytool"


# --------------------------------------------------------------------------- #
# 2. contract adherence
# --------------------------------------------------------------------------- #

def test_contract_adherence():
    _make_fake_module("fake_module", lambda entry: FakeSource(entry.get("name", "x"), entry))
    entry = {"enabled": True, "entrypoint": "fake_module:factory", "prefix": "fake:"}
    source = build("mytool", entry)
    assert hasattr(source, "name")
    assert hasattr(source, "capability")
    assert callable(source.prerequisites)
    assert callable(source.read)
    assert callable(source.close)
    # capability is a Capability with the configured prefix
    assert isinstance(source.capability, Capability)
    assert source.capability.prefix == "fake:"


# --------------------------------------------------------------------------- #
# 3. import failure fails fast, naming the module path
# --------------------------------------------------------------------------- #

def test_import_failure_fails_fast_names_module():
    entry = {"enabled": True, "entrypoint": "nonexistent_module:factory", "prefix": "x:"}
    with pytest.raises(CustomSourceError) as exc:
        build("mytool", entry)
    assert "nonexistent_module" in str(exc.value)


def test_factory_not_callable_fails_fast():
    _make_fake_module("fake_module", "not-a-function")  # factory attr is a string
    entry = {"enabled": True, "entrypoint": "fake_module:factory", "prefix": "fake:"}
    with pytest.raises(CustomSourceError):
        build("mytool", entry)


def test_missing_factory_attr_fails_fast():
    mod = types.ModuleType("fake_module")
    sys.modules["fake_module"] = mod  # module has no `factory` attribute
    entry = {"enabled": True, "entrypoint": "fake_module:factory", "prefix": "fake:"}
    with pytest.raises(CustomSourceError):
        build("mytool", entry)


# --------------------------------------------------------------------------- #
# 4. factory returns non-Source -> error
# --------------------------------------------------------------------------- #

def test_factory_returns_non_source_raises():
    _make_fake_module("fake_module", lambda entry: {"name": "bogus"})
    entry = {"enabled": True, "entrypoint": "fake_module:factory", "prefix": "fake:"}
    with pytest.raises(CustomSourceError):
        build("mytool", entry)


def test_factory_returns_source_missing_methods_raises():
    class Incomplete:
        name = "x"
        capability = Capability(runtime=None, credential=None, prefix="x:")
    _make_fake_module("fake_module", lambda entry: Incomplete())
    entry = {"enabled": True, "entrypoint": "fake_module:factory", "prefix": "fake:"}
    with pytest.raises(CustomSourceError):
        build("mytool", entry)


# --------------------------------------------------------------------------- #
# 5 + 6. credential env-var check (via the loader's wrap)
# --------------------------------------------------------------------------- #

def test_credential_env_missing_reported_in_prerequisites(monkeypatch):
    monkeypatch.delenv("MYTOOL_TOKEN", raising=False)

    def factory(entry):
        src = FakeSource(entry.get("name", "x"), entry)
        src.capability = Capability(runtime=None, credential="MYTOOL_TOKEN", prefix="mytool:")
        return src

    _make_fake_module("fake_module", factory)
    entry = {"enabled": True, "entrypoint": "fake_module:factory",
             "credential": "MYTOOL_TOKEN", "prefix": "mytool:"}
    source = build("mytool", entry)
    missing = source.prerequisites()
    assert len(missing) == 1
    assert "MYTOOL_TOKEN" in missing[0]


def test_credential_env_present_prerequisites_empty(monkeypatch):
    monkeypatch.setenv("MYTOOL_TOKEN", "secret-value")

    def factory(entry):
        src = FakeSource(entry.get("name", "x"), entry)
        src.capability = Capability(runtime=None, credential="MYTOOL_TOKEN", prefix="mytool:")
        return src

    _make_fake_module("fake_module", factory)
    entry = {"enabled": True, "entrypoint": "fake_module:factory",
             "credential": "MYTOOL_TOKEN", "prefix": "mytool:"}
    source = build("mytool", entry)
    assert source.prerequisites() == []


def test_no_credential_field_does_not_wrap():
    """Without a `credential` field, the loader does NOT wrap in _CredentialGuard."""
    instance = None

    def factory(entry):
        nonlocal instance
        src = FakeSource(entry.get("name", "x"), entry)
        instance = src
        return src

    _make_fake_module("fake_module", factory)
    entry = {"enabled": True, "entrypoint": "fake_module:factory", "prefix": "fake:"}
    source = build("mytool", entry)
    result = source.prerequisites()
    assert result == []
    # No `credential` field -> the returned object is the factory's own instance,
    # not wrapped in _CredentialGuard.
    assert source is instance, "expected the factory's own source, not a wrapper"
    assert not isinstance(source, custom_mod._CredentialGuard)


# --------------------------------------------------------------------------- #
# built-in names are unaffected
# --------------------------------------------------------------------------- #

def test_builtin_name_still_resolves():
    """A built-in name goes through the registry, not the custom path."""
    source = build("fs", {"extra": {"dir": "/tmp"}})
    assert source.name == "fs"
