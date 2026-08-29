"""Shared IMAP mail source for yahoo and gmail.

Credential env vars are declared in the capability; all other locations
(host, caps) come from config ``extra``.
"""

from __future__ import annotations

import email
import email.header
import email.policy
import os

from .base import Capability, IngestItem, Source

_CREDENTIALS = {
    "yahoo": "YMAIL_APP_PASSWORD",
    "gmail": "GMAIL_APP_PASSWORD",
}

_EMAIL_ENV = {
    "yahoo": "YMAIL_EMAIL",
    "gmail": "GMAIL_EMAIL",
}

_IMAP_PORT = 993


class ImapMailSource(Source):
    capability: Capability  # set by factory based on provider name

    def __init__(self, name: str, entry: dict):
        self.name = name
        extra = entry.get("extra") or {}
        self.imap_host: str = str(extra.get("imap_host") or "")
        self.max_unseen: int = int(extra.get("max_unseen", 200))
        self.credential: str = _CREDENTIALS.get(name, "")

    def prerequisites(self) -> list:
        missing = []
        if self.credential and not os.environ.get(self.credential):
            missing.append(
                f"credential env var {self.credential} is not set "
                f"(required by sources.{self.name})"
            )
        if not self.imap_host:
            missing.append(
                f"sources.{self.name}.extra.imap_host is not set "
                f"(IMAP server host for {self.name})"
            )
        return missing

    def read(self, since: str | None):
        if not self.imap_host:
            return
        conn = _imap_connect(
            self.imap_host,
            os.environ.get(_EMAIL_ENV.get(self.name, ""), ""),
            os.environ.get(self.credential, ""),
        )
        try:
            conn.select("INBOX")
            status, data = conn.search(None, "UNSEEN")
            if status != "OK" or not data or not data[0]:
                return
            mids = data[0].split()[: self.max_unseen]
            for mid in mids:
                status, msg_data = conn.fetch(
                    mid,
                    "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)] BODY.PEEK[TEXT])",
                )
                if status != "OK" or not msg_data:
                    continue
                raw = b""
                for item in msg_data:
                    if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                        raw = item[1]
                        break
                if not raw:
                    continue
                msg = email.message_from_bytes(raw, policy=email.policy.default)
                key = msg.get("Message-ID") or f"{self.name}-{mid.decode()}"
                subject = _decode_header(msg.get("Subject"))
                from_addr = _decode_header(msg.get("From"))
                body = _extract_body(msg)
                if not body.strip():
                    continue
                content = f"FROM: {from_addr}\nSUBJECT: {subject}\n\n{body}"
                ts = _parse_date(msg.get("Date"))
                yield IngestItem(
                    key=key,
                    content=content,
                    ts=ts,
                    metadata={
                        "from": from_addr,
                        "subject": subject,
                        "imap_mid": mid.decode() if isinstance(mid, bytes) else str(mid),
                    },
                )
        finally:
            conn.logout()

    def close(self) -> None:
        pass


def _decode_header(value) -> str:
    if not value:
        return ""
    try:
        return str(email.header.make_header(email.header.decode_header(str(value))))
    except Exception:
        return str(value)


def _extract_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(
                        part.get_content_charset() or "utf-8", errors="replace")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    html = payload.decode(
                        part.get_content_charset() or "utf-8", errors="replace")
                    import re
                    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()
        return ""
    payload = msg.get_payload(decode=True)
    if payload:
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return ""


def _parse_date(value) -> str:
    if not value:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(str(value))
        if dt is None:
            raise ValueError("unparseable")
        return dt.isoformat(timespec="microseconds")
    except Exception:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _imap_connect(host: str, email_addr: str, password: str):
    """Connect to the IMAP server; overridable in tests.

    In production: create an ``imaplib.IMAP4_SSL`` connection, login, return.
    """
    import imaplib

    conn = imaplib.IMAP4_SSL(host, _IMAP_PORT)
    conn.login(email_addr, password)
    return conn


def factory(entry: dict) -> ImapMailSource:
    name = entry.get("name", "")
    if name not in _CREDENTIALS:
        raise ValueError(f"unknown IMAP provider: {name!r}")
    credential = _CREDENTIALS[name]
    cap = Capability(
        runtime=None,
        credential=credential,
        prefix=f"{name}:",
    )
    source = ImapMailSource(name, entry)
    source.capability = cap
    return source
