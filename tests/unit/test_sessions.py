"""Red tests for 003 multi-user: sessions (T001)."""


def test_import_auth_session_helpers():
    """003 adds session helpers to digital_twins.auth."""
    from digital_twins.auth import verify_session  # noqa: F401
