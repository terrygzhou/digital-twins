"""IMAP mail source (yahoo/gmail): fail-fast prerequisites, stubbed read."""

from digital_twins.sources import build
from digital_twins.sources import imap_mail as imap_mod


def test_gmail_capability_declared():
    source = build("gmail", {"extra": {"imap_host": "imap.gmail.com"}})
    cap = source.capability
    assert cap.credential == "GMAIL_APP_PASSWORD"
    assert cap.prefix == "gmail:"


def test_yahoo_capability_declared():
    source = build("yahoo", {"extra": {"imap_host": "imap.mail.yahoo.com"}})
    cap = source.capability
    assert cap.credential == "YMAIL_APP_PASSWORD"
    assert cap.prefix == "yahoo:"


def test_gmail_missing_credential_is_prerequisite(monkeypatch):
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    source = build("gmail", {"extra": {"imap_host": "imap.gmail.com"}})
    missing = source.prerequisites()
    assert len(missing) == 1
    assert "GMAIL_APP_PASSWORD" in missing[0]


def test_yahoo_missing_credential_is_prerequisite(monkeypatch):
    monkeypatch.delenv("YMAIL_APP_PASSWORD", raising=False)
    source = build("yahoo", {"extra": {"imap_host": "imap.mail.yahoo.com"}})
    missing = source.prerequisites()
    assert len(missing) == 1
    assert "YMAIL_APP_PASSWORD" in missing[0]


def test_gmail_missing_imap_host_is_prerequisite(monkeypatch):
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "test-cred")
    source = build("gmail", {})
    missing = source.prerequisites()
    assert len(missing) == 1
    assert "sources.gmail.extra.imap_host" in missing[0]
    # read() without connection must yield nothing
    assert list(source.read(None)) == []


def test_gmail_all_prereqs_satisfied(monkeypatch):
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "test-cred")
    source = build("gmail", {"extra": {"imap_host": "imap.gmail.com"}})
    assert source.prerequisites() == []


def test_gmail_read_with_stubbed_imap(monkeypatch):
    """read() yields IngestItems from a stubbed IMAP connection."""
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "test-cred")
    source = build("gmail", {"extra": {"imap_host": "imap.gmail.com"}})
    assert source.prerequisites() == []

    raw_email = (
        b"From: sender@example.com\r\n"
        b"To: me@example.com\r\n"
        b"Subject: Test Email\r\n"
        b"Date: Fri, 16 Jan 2025 10:00:00 +0000\r\n"
        b"Message-ID: <test-123@example.com>\r\n"
        b"\r\n"
        b"Hello, this is the email body.\r\n"
    )

    class StubIMAP:
        def select(self, mailbox, mode="R"):
            return ("OK", [b"1"])

        def search(self, charset, criteria):
            return ("OK", [b"1"])

        def fetch(self, mid, partspec):
            return ("OK", [(b"1 (UID 1)", raw_email)])

        def close(self):
            pass

        def logout(self):
            pass

    stub = StubIMAP()
    monkeypatch.setattr(imap_mod, "_imap_connect", lambda *a, **kw: stub)

    items = list(source.read(None))
    assert len(items) == 1
    assert items[0].key == "<test-123@example.com>"
    assert "Hello, this is the email body." in items[0].content
    assert items[0].metadata["from"] == "sender@example.com"
    assert items[0].metadata["subject"] == "Test Email"


def test_yahoo_read_respects_max_unseen(monkeypatch):
    """max_unseen cap limits how many messages are fetched."""
    monkeypatch.setenv("YMAIL_APP_PASSWORD", "test-cred")
    source = build("yahoo", {
        "extra": {"imap_host": "imap.mail.yahoo.com", "max_unseen": 2},
    })

    raw1 = (b"From: a@x.com\r\nSubject: one\r\n"
            b"Message-ID: <a@x>\r\n\r\nbody one\r\n")
    raw2 = (b"From: b@x.com\r\nSubject: two\r\n"
            b"Message-ID: <b@x>\r\n\r\nbody two\r\n")

    class StubIMAP:
        def select(self, mailbox, mode="R"):
            return ("OK", [b"3"])

        def search(self, charset, criteria):
            return ("OK", [b"1 2 3"])

        def fetch(self, mid, partspec):
            if mid == b"1":
                return ("OK", [(b"1", raw1)])
            if mid == b"2":
                return ("OK", [(b"2", raw2)])
            return ("OK", [None])

        def close(self):
            pass

        def logout(self):
            pass

    stub = StubIMAP()
    monkeypatch.setattr(imap_mod, "_imap_connect", lambda *a, **kw: stub)

    items = list(source.read(None))
    assert len(items) == 2  # capped at max_unseen=2
