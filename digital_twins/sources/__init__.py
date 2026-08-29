"""Built-in source registry (contracts/source.md)."""

from __future__ import annotations

import importlib

# source name -> module holding `factory(config_entry) -> Source`
BUILTIN_MODULES = {
    "fs": "digital_twins.sources.fs",
    "hermes": "digital_twins.sources.hermes",
    "pi": "digital_twins.sources.pi",
    "dsh": "digital_twins.sources.dsh",
    "paperclip": "digital_twins.sources.paperclip",
    "yahoo": "digital_twins.sources.imap_mail",
    "gmail": "digital_twins.sources.imap_mail",
}


class UnknownSourceError(KeyError):
    """An enabled source name has no registered module."""


# Re-export the custom-source error so callers can import it from the package
from .custom import CustomSourceError  # noqa: F401


def build(name: str, entry: dict):
    """Resolve a source name + config entry to a Source instance.

    Built-in names resolve through the registry; any other name with an
    ``entrypoint`` field resolves as a user-defined source (custom.py).
    """
    module_path = BUILTIN_MODULES.get(name)
    if module_path is not None:
        module = importlib.import_module(module_path)
        return module.factory(dict(entry, name=name))
    if entry.get("entrypoint"):
        from .custom import build_custom
        return build_custom(name, entry)
    raise UnknownSourceError(name)
