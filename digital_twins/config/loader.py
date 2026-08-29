"""Four-layer configuration loader.

Precedence (highest wins):

    1. environment variables — real process env first, then `.env` files
       (`KB_` prefix, `__` = nesting, e.g. `KB_QDRANT__URL`)
    2. `kb.local.yml` — machine-local, lives in the config dir (untracked)
    3. `kb.yml`       — committed defaults; read from the config dir first,
                        then the current working directory
    4. built-in defaults — `config.schema.DEFAULTS` (+ per-source defaults)

The config dir is bootstrapped from real env / `.env` / the built-in default
only, so a user `kb.yml` cannot relocate itself out of discovery.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv as _load_dotenv

from . import schema as _schema
from .schema import SchemaError, coerce, env_path_for


class ConfigError(Exception):
    """Base error for configuration problems (fail-fast, SC-001)."""


def _yaml(path: Path):
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    return data or {}


def _set_path(target: dict, dotted: str, value):
    parts = dotted.split(".")
    node = target
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _env_layer(env: dict, var_filter=None) -> dict:
    """Project `KB_*` environment variables onto a nested config dict."""
    layer = {}
    for name, value in env.items():
        if name.startswith("KB_") and (
                var_filter is None or var_filter(name)):
            dotted = env_path_for(name)
            if dotted is not None:
                _set_path(layer, dotted, value)
    return layer


def _nested(d: dict) -> dict:
    """Expand dotted registry keys into a nested dict."""
    out: dict = {}
    for key, value in d.items():
        _set_path(out, key, value)
    return out


def _merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _build_layers(cwd, env, config_dir):
    """Build the four config layers in ascending precedence.

    Returns a list of four (layer_name, nested_dict) tuples:
        1. "defaults"     — built-in defaults (schema.DEFAULTS)
        2. "kb.yml"       — merged cwd/kb.yml + config_dir/kb.yml
        3. "kb.local.yml" — machine-local overrides
        4. "env"          — projected KB_* environment variables

    When ``env`` is None, the ``.env`` files are read into the process
    environment (CWD first, then the user config dir) exactly as
    :func:`load` does; when an explicit mapping is given, it is used
    verbatim and no ``.env`` files are touched.
    """
    if env is None:
        # .env layer: CWD first, then the user config dir as derived from
        # real env / the built-in default. override=False: real environment
        # variables always win, CWD .env wins over the user one.
        _load_dotenv(cwd / ".env", override=False)
        pre_dir = Path(
            dict(os.environ).get("KB_CONFIG_DIR")
            or _schema.DEFAULTS["config_dir"]
        ).expanduser()
        _load_dotenv(pre_dir / ".env", override=False)
        env = dict(os.environ)
    if config_dir is None:
        config_dir = env.get("KB_CONFIG_DIR") or _schema.DEFAULTS["config_dir"]
    config_dir = Path(config_dir).expanduser()

    layers = [
        ("defaults", _nested(_schema.DEFAULTS)),
        ("kb.yml", _merge(_yaml(cwd / "kb.yml"), _yaml(config_dir / "kb.yml"))),
        ("kb.local.yml", _yaml(config_dir / "kb.local.yml")),
        ("env", _env_layer(env)),
    ]
    return layers, config_dir


def load(cwd=None, env=None, config_dir=None):
    """Load and validate the effective configuration.

    Args:
        cwd: current working directory (defaults to os.getcwd()).
        env: explicit environment mapping; when given, `.env` files are
            ignored (the caller controls the environment). Defaults to
            os.environ.
        config_dir: override the config directory directly.

    Returns a validated, normalized config dict (see schema.validate).
    """
    cwd = Path(cwd) if cwd is not None else Path.cwd()
    layers, config_dir = _build_layers(cwd, env, config_dir)

    merged = layers[0][1]
    for _, layer in layers[1:]:
        merged = _merge(merged, layer)

    cfg = _schema.validate(merged)
    cfg["state_dir"] = str(Path(cfg["state_dir"]).expanduser())
    cfg["config_dir"] = str(config_dir)
    return cfg


def load_debug(cwd=None, env=None, config_dir=None) -> dict[str, str]:
    """Return a mapping of knob dotted-path -> winning layer name.

    Layer names (exactly): "env", "kb.local.yml", "kb.yml", "defaults".
    Only knobs that are actually set in at least one layer are included;
    the highest-precedence layer that set a value wins. The same layer
    inputs as :func:`load` are used — identical arguments yield identical
    winners — but the values are not validated or normalized.
    """
    cwd = Path(cwd) if cwd is not None else Path.cwd()
    layers, _config_dir = _build_layers(cwd, env, config_dir)

    winner: dict[str, str] = {}
    for name, layer in layers:
        for dotted, _value in _flatten(layer):
            winner[dotted] = name
    return winner


def _flatten(nested: dict, prefix: str = "") -> list[tuple[str, object]]:
    """Flatten a nested dict to (dotted.path, value) pairs."""
    out: list[tuple[str, object]] = []
    for key, value in nested.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            out.extend(_flatten(value, path))
        else:
            out.append((path, value))
    return out
