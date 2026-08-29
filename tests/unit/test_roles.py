"""Red tests for 003 multi-user: roles (T001)."""


def test_import_role_caps():
    """003 adds ROLE_CAPS matrix to digital_twins.accounts."""
    from digital_twins.accounts import ROLE_CAPS  # noqa: F401
