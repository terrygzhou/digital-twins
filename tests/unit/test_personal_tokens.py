"""Red tests for 003 multi-user: personal_tokens (T001)."""


def test_import_auth_personal_token_helpers():
    """003 adds personal-token helpers to digital_twins.auth."""
    from digital_twins.auth import verify_personal_token  # noqa: F401
