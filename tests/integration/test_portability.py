"""Portability invariant (SC-003, NFR-13).

The shipped surface must contain zero host-specific paths, usernames, or
install locations — all such values resolve through the config layer at
runtime. This test is a standing guard: it must stay green in every phase.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# Host home paths (any user) and the baseline host runtime directories.
# Our own documented defaults (~/.digital-twins, ~/.config/digital-twins)
# are host-neutral and intentionally NOT matched.
HOST_PATTERNS = [
    re.compile(r"/home/[^/\s\"'`]+/"),      # any Linux host home path
    re.compile(r"/Users/[^/\s\"'`]+/"),      # any macOS host home path
    re.compile(r"~/\.\w*hermes\b|~/\.dsh\b"),  # baseline host runtimes
]

# Everything the package ships or publishes with.
SHIPPED = [
    "digital_twins",          # package code (.py)
    "pyproject.toml",
    "README.md",
    "CHANGELOG.md",
    "config.example.yml",
    ".env.example",
    "LICENSE",
]


def _shipped_files():
    for entry in SHIPPED:
        p = REPO / entry
        if p.is_dir():
            yield from (f for f in p.rglob("*.py") if f.is_file())
        elif p.is_file():
            yield p


def test_no_host_specific_values_in_shipped_surface():
    hits = []
    for f in _shipped_files():
        for lineno, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            for pat in HOST_PATTERNS:
                if pat.search(line):
                    hits.append(f"{f.relative_to(REPO)}:{lineno}: {line.strip()}")
    assert not hits, (
        "host-specific values found in the shipped surface:\n" + "\n".join(hits)
    )
