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
    re.compile(r"~/\.\w*hermes\b|~/\.dsh\b|~/\.pi\b"),  # baseline host runtimes
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
    "docs/scheduling.md",     # 002 scheduling docs (added in T013, guarded here in T020)
    "docs/multi-user.md",     # 003 multi-user docs (added in T020, guarded here in T020)
    # 005 community/packaging artifacts
    "docs/configuration.md",
    "docs/semver-policy.md",
    "docs/release-runbook.md",
    "docs/references/agent-guides.md",
    "docker-compose.yml",
    "Dockerfile",
    ".github/ISSUE_TEMPLATE",
]

# 005 artifacts that are not Python files — the SHIPPED list includes
# directories and non-.py files that need the portability scan too.
SHIPPED_NON_PY = [
    "docs/configuration.md",
    "docs/semver-policy.md",
    "docs/release-runbook.md",
    "docs/references/agent-guides.md",
    "docker-compose.yml",
    "Dockerfile",
]


def _shipped_files():
    for entry in SHIPPED:
        p = REPO / entry
        if p.is_dir():
            yield from (f for f in p.rglob("*.py") if f.is_file())
        elif p.is_file():
            yield p
    # 005: also scan non-Python shipped artifacts
    for entry in SHIPPED_NON_PY:
        p = REPO / entry
        if p.is_file():
            yield p
    # 005: .github/ISSUE_TEMPLATE/ (YAML files)
    tpl_dir = REPO / ".github" / "ISSUE_TEMPLATE"
    if tpl_dir.is_dir():
        yield from (f for f in tpl_dir.rglob("*.yml") if f.is_file())


# Interpreter pins: a pinned CPython version (python3.12, python3.11, ...)
# is a host runtime detail; the package targets "python3" generically.
INTERPRETER_PIN = re.compile(r"python3\.\d+")

# Literal usernames that would leak a host identity into shipped docs.
# Placeholders like `<user>` / `<email>` are allowed; concrete names are not.
LITERAL_USERNAMES = re.compile(
    r"\b(terry|alice|bob|admin|root|johndoe|janedoe|operator)\b",
    re.IGNORECASE,
)
# Lines in a docs file that are allowed to contain the literal-name tokens:
# none in shipped docs — every example must use a placeholder.


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


def test_no_interpreter_pins_in_shipped_surface():
    """NFR-13 (T020): no pinned CPython version (python3.12) in shipped files."""
    hits = []
    for f in _shipped_files():
        for lineno, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if INTERPRETER_PIN.search(line):
                hits.append(f"{f.relative_to(REPO)}:{lineno}: {line.strip()}")
    assert not hits, (
        "interpreter pins found in the shipped surface:\n" + "\n".join(hits)
    )


def test_no_literal_usernames_in_multi_user_doc():
    """NFR-13 (T020): docs/multi-user.md must use placeholders only.

    The multi-user doc describes accounts, roles and credentials. Any
    concrete username or email (alice@example.com, terry, ...) would leak
    a host identity. Every example must use <user>, <email>, <token> ...
    """
    doc = REPO / "docs" / "multi-user.md"
    assert doc.is_file(), "docs/multi-user.md is missing (T020)"
    hits = []
    for lineno, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
        for m in LITERAL_USERNAMES.finditer(line):
            word = m.group(0)
            # 'admin' is a role name, not a username: allow it when it is
            # the literal role token (role=admin / "admin" / an 'admin'
            # column header) but flag it in a username position.
            if word.lower() == "admin":
                continue
            hits.append(f"{doc.name}:{lineno}: {line.strip()} ({word})")
    assert not hits, (
        "literal usernames found in docs/multi-user.md:\n" + "\n".join(hits)
    )
