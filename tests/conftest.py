"""Fixtures for the integration tests against GreenMail (IMAPS on 127.0.0.1:3993)."""

from __future__ import annotations

import logging
import os
import socket
import ssl
import threading
import time
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from email import message_from_bytes
from email.utils import parsedate_to_datetime
from pathlib import Path

import pytest
from imapclient import IMAPClient

from tests import mails

IMAP_HOST = os.environ.get("TEST_IMAP_HOST", "127.0.0.1")
IMAP_PORT = int(os.environ.get("TEST_IMAP_PORT", "3993"))
GREENMAIL_API = os.environ.get("TEST_GREENMAIL_API", "http://127.0.0.1:3980")
PASSWORDS = {"support": "support-secret", "info": "info-secret", "writer": "writer-secret"}
ADMIN_KEY = "admin-key-0123456789"
SUPPORT_KEY = "support-key-0123456789"


def _tls() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def imap_login(user: str) -> IMAPClient:
    client = IMAPClient(IMAP_HOST, port=IMAP_PORT, ssl=True, ssl_context=_tls(), timeout=10)
    client.login(user, PASSWORDS[user])
    return client


def _greenmail_up() -> bool:
    try:
        with socket.create_connection((IMAP_HOST, IMAP_PORT), timeout=1):
            return True
    except OSError:
        return False


def pytest_collection_modifyitems(config, items):
    if _greenmail_up():
        return
    skip = pytest.mark.skip(reason="GreenMail not reachable (docker compose --profile test up -d greenmail)")
    for item in items:
        if "imap" in item.keywords:
            item.add_marker(skip)


def _reset_greenmail() -> None:
    """Drop all mailboxes and messages; the configured users stay."""
    request = urllib.request.Request(f"{GREENMAIL_API}/api/service/reset", method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 200
    deadline = time.monotonic() + 10
    while True:
        try:
            imap_login("support").logout()
            return
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)


@dataclass
class Seeded:
    uids: dict[str, int]


@pytest.fixture(scope="session")
def seeded() -> Seeded:
    """Fill the GreenMail mailboxes with a fixed set of messages and return their UIDs."""
    uids: dict[str, int] = {}

    def append(client: IMAPClient, folder: str, key: str, raw: bytes, flags=()) -> None:
        # Use the Date header as INTERNALDATE so SINCE/BEFORE searches are deterministic.
        sent = parsedate_to_datetime(message_from_bytes(raw)["Date"])
        client.append(folder, raw, flags=flags, msg_time=sent)
        client.select_folder(folder, readonly=True)
        uids[key] = max(client.search(["ALL"]))

    _reset_greenmail()
    support = imap_login("support")
    for folder in ("Archive", "Archive.2025", "Secret"):
        support.create_folder(folder)
    append(support, "INBOX", "plain", mails.plain_umlauts())
    append(support, "INBOX", "html", mails.html_only(), flags=[b"\\Seen"])
    append(support, "INBOX", "attach", mails.with_attachments(), flags=[b"\\Flagged"])
    append(support, "INBOX", "encoded", mails.encoded_subject(), flags=[b"\\Seen"])
    for index, raw in enumerate(mails.thread(), start=1):
        append(support, "INBOX", f"thread{index}", raw)
    append(support, "Archive.2025", "archived", mails.plain_simple("Alte Mail", "<old-1@example.com>"))
    append(support, "Secret", "secret", mails.plain_simple("Streng geheim", "<secret-1@example.com>"))
    support.logout()

    info = imap_login("info")
    append(info, "INBOX", "info1", mails.plain_simple("Info-Anfrage", "<info-1@example.org>", to="info@example.org"))
    info.logout()

    writer = imap_login("writer")
    for folder in ("Drafts", "Done"):
        writer.create_folder(folder)
    append(writer, "INBOX", "w1", mails.plain_simple("Zu erledigen 1", "<w1@example.net>", to="writer@example.net"))
    append(writer, "INBOX", "w2", mails.plain_simple("Zu erledigen 2", "<w2@example.net>", to="writer@example.net"))
    writer.logout()
    return Seeded(uids)


ACCOUNTS_YAML = f"""
accounts:
  - id: support
    label: Support mailbox
    host: {IMAP_HOST}
    port: {IMAP_PORT}
    username: support
    password_env: TEST_SUPPORT_PASSWORD
    tls_verify: false
    folders_allow: ["INBOX", "Archive", "Archive.*", "Secret"]
    folders_deny: ["Secret"]
  - id: info
    label: Info mailbox
    host: {IMAP_HOST}
    port: {IMAP_PORT}
    username: info
    password_env: TEST_INFO_PASSWORD
    tls_verify: false
"""

WRITER_YAML = f"""
  - id: writer
    label: Writer mailbox
    host: {IMAP_HOST}
    port: {IMAP_PORT}
    username: writer
    password_env: TEST_WRITER_PASSWORD
    tls_verify: false
    mode: readwrite
"""

ENV = {
    "TEST_SUPPORT_PASSWORD": PASSWORDS["support"],
    "TEST_INFO_PASSWORD": PASSWORDS["info"],
    "TEST_WRITER_PASSWORD": PASSWORDS["writer"],
}


class LogCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


@dataclass
class RunningServer:
    url: str
    logs: LogCollector


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_server(tmp: Path, yaml_text: str, api_keys: str) -> Iterator[RunningServer]:
    import uvicorn

    from imap_mcp.config import Settings, load_accounts
    from imap_mcp.server import configure_logging, create_app

    path = tmp / "accounts.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    port = _free_port()
    settings = Settings(host="127.0.0.1", port=port, api_keys=api_keys, accounts_file=str(path), log_level="INFO")
    configure_logging(settings.log_level)
    collector = LogCollector()
    logging.getLogger().addHandler(collector)
    app, state = create_app(settings, load_accounts(path, ENV))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("test server did not start")
        time.sleep(0.05)
    try:
        yield RunningServer(url=f"http://127.0.0.1:{port}", logs=collector)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        state.pools.stop()
        logging.getLogger().removeHandler(collector)


@pytest.fixture(scope="session")
def server(seeded, tmp_path_factory) -> Iterator[RunningServer]:
    """Read-only server with two accounts and two API keys (one scoped to 'support')."""
    yield from start_server(tmp_path_factory.mktemp("ro"), ACCOUNTS_YAML, f"{ADMIN_KEY},{SUPPORT_KEY}:support")


@pytest.fixture(scope="session")
def rw_server(seeded, tmp_path_factory) -> Iterator[RunningServer]:
    """Server with an additional readwrite account and no API keys."""
    yield from start_server(tmp_path_factory.mktemp("rw"), ACCOUNTS_YAML + WRITER_YAML, "")
