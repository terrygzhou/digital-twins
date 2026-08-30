"""T032 (008 US3, RED): bootstrap script pins — no image refs + compose guard intact.

Two assertions:

1. ``scripts/bootstrap-local.sh`` contains **no image reference of its own**
   (005 SC-005 — ``docker-compose.yml`` stays the single source of image pins).
   The script must reference services by compose service name (``qdrant``,
   ``neo4j``, ``llm``, ``embedding-model``, ``digital-twins``), not by
   registry path (``qdrant/qdrant``) or ``image:`` line.

2. ``docker-compose.yml`` well-formedness guard (``test_docker_compose.py``)
   is unaffected — the module still exists and its tests pass.

Both are **guard** assertions, not feature tests: they are expected to be
GREEN on first run (the stub has no image refs and the compose file is
already well-formed).  The RED value is that they will keep the T033
implementer honest: if the full script starts embedding image tags, this
test fails.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "bootstrap-local.sh"
COMPOSE_TESTS = REPO / "tests" / "integration" / "test_docker_compose.py"


# ---------------------------------------------------------------------------
# 1. No image reference in the bootstrap script (SC-005)
# ---------------------------------------------------------------------------

# Image reference patterns:
#   - `image:` key in YAML (compose-style)
#   - registry/namespace/path:tag  (e.g. qdrant/qdrant:1.9.7, lmsys/sglang:v0.4.2)
#   - docker pull <registry/path:tag>
#   - FROM <registry/path:tag>  (Dockerfile-style, not expected in a .sh)
IMAGE_REF_PATTERNS = [
    # `image:` key (YAML/compose)
    re.compile(r"\bimage:\s*\S+"),
    # registry/namespace:tag  (at least one slash, at least one colon with a tag)
    re.compile(r"\b[a-z][a-z0-9-]*(?:[/.][a-z0-9.-]+)+:[A-Za-z0-9][A-Za-z0-9._-]*"),
    # docker pull with a tagged ref
    re.compile(r"\bdocker\s+pull\s+\S+:\S+"),
    # FROM <image> (Dockerfile)
    re.compile(r"^\s*FROM\s+\S+", re.MULTILINE),
]


def test_script_has_no_image_references():
    """SC-005: docker-compose.yml is the single source of image pins.

    The bootstrap script must not contain its own image references.
    It may reference compose *service names* (qdrant, neo4j, llm, …) —
    those are not image refs.
    """
    assert SCRIPT.is_file(), f"{SCRIPT} is missing (T030: stub not created)"
    raw = SCRIPT.read_text(encoding="utf-8")

    hits = []
    for lineno, line in enumerate(raw.splitlines(), 1):
        for pat in IMAGE_REF_PATTERNS:
            m = pat.search(line)
            if m:
                hits.append(f"{SCRIPT.name}:{lineno}: {line.strip()}  (pattern: {pat.pattern})")

    assert not hits, (
        "image references found in bootstrap script (SC-005: compose is the single pin source):\n"
        + "\n".join(hits)
    )


# ---------------------------------------------------------------------------
# 2. Compose well-formedness guard unaffected (test_docker_compose.py)
# ---------------------------------------------------------------------------

def test_compose_guard_module_exists():
    """The existing compose well-formedness guard (T010, 005) must still exist.

    T032 does not re-implement the compose checks; it asserts the guard
    module is present so the standing test suite keeps running.
    """
    assert COMPOSE_TESTS.is_file(), (
        f"{COMPOSE_TESTS} is missing — the 005 compose well-formedness guard "
        "(test_docker_compose.py) must remain in the suite"
    )


def test_compose_guard_tests_pass():
    """Re-run the 005 compose guard tests to confirm they still pass.

    This is a belt-and-braces check: if the compose file or the guard
    module has regressed, T032 fails here even though T032's own
    image-ref assertion is independent.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(COMPOSE_TESTS), "-q", "--tb=short"],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        timeout=60,
    )
    assert result.returncode == 0, (
        f"compose guard tests failed:\n{result.stdout}\n{result.stderr}"
    )
