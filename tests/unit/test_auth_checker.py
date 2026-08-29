"""Red tests for 003 multi-user: auth_checker (T001)."""


def test_import_web_server():
    """003 adds digital_twins.web.server with auth_checker support."""
    import digital_twins.web.server  # noqa: F401
