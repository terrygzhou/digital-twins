"""Red tests for 003 multi-user: owner tag (T001)."""


def test_import_accounts_owner_tag():
    """003 adds owner-tag helpers to digital_twins.accounts."""
    from digital_twins.accounts import owner_tag_for  # noqa: F401
