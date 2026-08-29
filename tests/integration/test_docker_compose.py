"""SC-005 automated compose check (T010, feature 005-community-packaging).

Loads `docker-compose.yml` (repo root) and asserts the reference-deployment
invariants from SC-005 / C-6:

  (a) five required services present:
      qdrant, neo4j, digital-twins, llm, embedding-model
  (b) every `image:` is pinned — an explicit version tag, no `:latest`,
      no untagged image
  (c) host-neutral: no `/home/...`, `/Users/...`, `~/...` absolute paths;
      no `python3.N` host-pin shorthand (a `python:3.11-slim` *image
      reference* is allowed — image-ref form ≠ host pin, R17); no literal
      username
  (d) the `digital-twins` service uses named volumes (not host bind paths)

The test FAILS now (RED) because `docker-compose.yml` does not exist yet.
"""

import re
from pathlib import Path

import pytest

import yaml

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "docker-compose.yml"

REQUIRED_SERVICES = [
    "qdrant",
    "neo4j",
    "digital-twins",
    "llm",
    "embedding-model",
]

# --- host-neutrality patterns (mirror test_portability.py) -------------------

HOST_PATTERNS = [
    re.compile(r"/home/[^/\s\"'`]+/"),   # any Linux host home path
    re.compile(r"/Users/[^/\s\"'`]+/"),  # any macOS host home path
    re.compile(r"~/\.\w+"),              # any ~/... absolute host path
]

# A pinned CPython interpreter version (python3.12, python3.11, ...) is a host
# runtime detail. A `python:3.11-slim` *image reference* does NOT match this
# pattern (it has no "3." immediately after "python"), so it is allowed (R17).
INTERPRETER_PIN = re.compile(r"python3\.\d+")

# Literal usernames that would leak a host identity into the compose file.
LITERAL_USERNAMES = re.compile(
    r"\b(terry|alice|bob|johndoe|janedoe|operator)\b",
    re.IGNORECASE,
)

# A host bind mount is a volume entry containing ":", e.g. "/data:/data".
# A named volume is a bare token with no ":", e.g. "digital-twins-state".


@pytest.fixture(scope="module")
def compose() -> dict:
    """Load and parse docker-compose.yml. Fails (RED) if the file is absent."""
    assert COMPOSE.is_file(), (
        f"{COMPOSE.name} is missing (T010: the compose file does not exist yet)"
    )
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{COMPOSE.name} did not parse to a mapping"
    return data


@pytest.fixture(scope="module")
def compose_services(compose: dict) -> dict:
    services = compose.get("services", {})
    assert isinstance(services, dict) and services, (
        f"{COMPOSE.name} has no `services` mapping"
    )
    return services


def test_five_required_services_present(compose_services: dict) -> None:
    missing = [s for s in REQUIRED_SERVICES if s not in compose_services]
    assert not missing, (
        f"missing required service(s): {missing}; "
        f"present: {sorted(compose_services)}"
    )


def test_every_image_is_pinned(compose_services: dict) -> None:
    """Every `image:` has an explicit version tag — no `:latest`, no untagged.

    A service built locally (`build:`) may also carry a tag on its `image:`;
    when it does, that tag must be a real version (not `latest`/untagged).
    """
    unpinned = []
    for name, svc in compose_services.items():
        image = svc.get("image")
        if image is None:
            # A build-only service (no image) is fine; nothing to pin.
            continue
        if not isinstance(image, str):
            unpinned.append(f"{name}: image is not a string ({image!r})")
            continue
        # An explicit tag is everything after the last ":" that follows a
        # registry/host (a ":" before a port is not a tag; compose image refs
        # here carry no host:port, so the last ":" is the tag separator).
        if ":" not in image:
            unpinned.append(f"{name}: untagged image {image!r}")
            continue
        tag = image.rsplit(":", 1)[-1]
        # A port-style tag (digits only, no letter) is not a version tag.
        if tag.isdigit() or tag == "latest":
            unpinned.append(f"{name}: bad tag {tag!r} on {image!r}")
    assert not unpinned, "unpinned image(s):\n" + "\n".join(unpinned)


def test_host_neutral(compose_services: dict) -> None:
    """No host paths, no `python3.N` host-pin, no literal username."""
    raw = COMPOSE.read_text(encoding="utf-8")
    hits = []
    for lineno, line in enumerate(raw.splitlines(), 1):
        for pat in HOST_PATTERNS:
            if pat.search(line):
                hits.append(f"{COMPOSE.name}:{lineno}: {line.strip()}")
        if INTERPRETER_PIN.search(line):
            hits.append(f"{COMPOSE.name}:{lineno}: {line.strip()} (python3.N pin)")
        for m in LITERAL_USERNAMES.finditer(line):
            hits.append(f"{COMPOSE.name}:{lineno}: {line.strip()} (username: {m.group(0)})")
    assert not hits, "host-specific values in compose:\n" + "\n".join(hits)


def test_digital_twins_uses_named_volumes(compose_services: dict) -> None:
    """The `digital-twins` service uses named volumes, not host bind paths."""
    svc = compose_services.get("digital-twins")
    assert isinstance(svc, dict), "digital-twins service missing or not a mapping"
    volumes = svc.get("volumes", [])
    assert isinstance(volumes, list) and volumes, (
        "digital-twins service has no `volumes` (must use named volumes)"
    )
    # A host bind mount has an absolute path as the source ("/data:/data").
    # A named volume mount is "volname:/mountpoint" where volname is a bare
    # token (no leading "/"). In compose, both use "name:mount" syntax; the
    # distinguishing feature is whether the source starts with "/".
    host_binds = [
        v for v in volumes
        if isinstance(v, str) and ":" in v and v.split(":")[0].startswith("/")
    ]
    assert not host_binds, (
        f"digital-twins uses host bind paths (not named volumes): {host_binds}"
    )
