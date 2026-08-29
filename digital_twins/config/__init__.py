"""Layered configuration: typed knob registry (schema) + four-layer loader."""

from .deprecation import deprecation_warn
from .loader import ConfigError, SchemaError, load, resolve
from .schema import SCHEMA, env_var_for, get, validate

__all__ = [
    "SCHEMA",
    "ConfigError",
    "SchemaError",
    "deprecation_warn",
    "env_var_for",
    "get",
    "load",
    "resolve",
    "validate",
]
