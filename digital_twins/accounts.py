"""Account CRUD + role model (003 multi-user, T001 scaffold)."""

# Role capability matrix (R3). Populated by T003+.
ROLE_CAPS: dict[str, set[str]] = {}


def owner_tag_for(email: str) -> str:
    """Return the owner tag for an account (R6). Stub — T007+."""
    raise NotImplementedError("owner_tag_for: 003 T007")
