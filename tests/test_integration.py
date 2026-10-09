"""End-to-end tests: MCP over HTTP against a real IMAP server (GreenMail)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

import anyio
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from tests.conftest import ADMIN_KEY, PASSWORDS, SUPPORT_KEY, imap_login

pytestmark = pytest.mark.imap

WRITE_TOOLS = {"set_flags", "move_messages", "create_draft"}


async def _session(url: str, key: str | None, fn):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with (
        httpx2.AsyncClient(headers=headers, timeout=30) as http,
        Client(streamable_http_client(f"{url}/mcp", http_client=http)) as client,
    ):
        return await fn(client)


def call(url: str, tool: str, args: dict[str, Any] | None = None, key: str | None = ADMIN_KEY) -> Any:
    async def run(client):
        return await client.call_tool(tool, args or {})

    return anyio.run(_session, url, key, run)


def ok(result) -> dict:
    assert not result.is_error, result.content
    return result.structured_content


def error_text(result) -> str:
    assert result.is_error
    return result.content[0].text


def tool_names(url: str, key: str | None = ADMIN_KEY) -> set[str]:
    async def run(client):
        return {t.name for t in (await client.list_tools()).tools}

    return anyio.run(_session, url, key, run)


def http_status(url: str, path: str, headers: dict[str, str] | None = None, body: bytes | None = None) -> int:
    request = urllib.request.Request(url + path, data=body, headers=headers or {}, method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


# ---- HTTP layer -------------------------------------------------------------------------


def test_health_without_key(server):
    with urllib.request.urlopen(server.url + "/health", timeout=10) as response:
        payload = json.loads(response.read())
    assert payload["status"] == "ok"
    assert set(payload["accounts"]) == {"support", "info"}
    assert set(payload["accounts"].values()) <= {"ok", "error", "unknown"}


def test_mcp_requires_valid_key(server):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    assert http_status(server.url, "/mcp", headers, body) == 401
    assert http_status(server.url, "/mcp", {**headers, "Authorization": "Bearer wrong"}, body) == 401
    assert http_status(server.url, "/mcp", {**headers, "X-API-Key": "wrong"}, body) == 401
    assert http_status(server.url, "/mcp", {**headers, "X-API-Key": ADMIN_KEY}, body) != 401


# ---- accounts and scopes ----------------------------------------------------------------


def test_list_accounts_hides_connection_data(server):
    result = ok(call(server.url, "list_accounts"))
    assert result["accounts"] == [
        {"id": "support", "label": "Support mailbox", "mode": "readonly"},
        {"id": "info", "label": "Info mailbox", "mode": "readonly"},
    ]
    dumped = json.dumps(result)
    for secret in ("127.0.0.1", "3993", *PASSWORDS.values(), "username"):
        assert secret not in dumped


def test_scoped_key_sees_only_its_accounts(server):
    result = ok(call(server.url, "list_accounts", key=SUPPORT_KEY))
    assert [a["id"] for a in result["accounts"]] == ["support"]
    text = error_text(call(server.url, "list_folders", {"account": "info"}, key=SUPPORT_KEY))
    assert "Unknown account 'info'" in text and "Valid accounts: support" in text


def test_unknown_account_lists_valid_ids(server):
    text = error_text(call(server.url, "list_folders", {"account": "nope"}))
    assert "Valid accounts: support, info" in text


def test_readonly_server_has_no_write_tools(server):
    names = tool_names(server.url)
    assert names == {"list_accounts", "list_folders", "search_messages", "get_message", "get_thread", "get_attachment"}


# ---- folders --------------------------------------------------------------------------


def test_list_folders_hides_denied(server, seeded):
    result = ok(call(server.url, "list_folders", {"account": "support"}))
    names = [f["name"] for f in result["folders"]]
    assert names[0] == "INBOX"
    assert "Archive.2025" in names
    assert "Secret" not in names
    inbox = result["folders"][0]
    assert inbox["messages"] == 7 and inbox["unseen"] == 5


def test_denied_folder_is_not_readable(server, seeded):
    for tool, args in (
        ("search_messages", {"account": "support", "folder": "Secret"}),
        ("get_message", {"account": "support", "folder": "Secret", "uid": seeded.uids["secret"]}),
        ("get_thread", {"account": "support", "folder": "Secret", "uid": seeded.uids["secret"]}),
        ("get_attachment", {"account": "support", "folder": "Secret", "uid": seeded.uids["secret"], "part": "1"}),
    ):
        assert "not accessible" in error_text(call(server.url, tool, args))


# ---- search ---------------------------------------------------------------------------


def test_search_newest_first_with_summary(server, seeded):
    result = ok(call(server.url, "search_messages", {"account": "support"}))
    assert result["total_matches"] == 7 and result["uidvalidity"] > 0
    dates = [m["date"] for m in result["messages"]]
    assert dates == sorted(dates, reverse=True)
    by_uid = {m["uid"]: m for m in result["messages"]}
    attach = by_uid[seeded.uids["attach"]]
    assert attach["has_attachments"] and "\\Flagged" in attach["flags"]
    assert attach["subject"] == "Rechnung März"
    assert not by_uid[seeded.uids["plain"]]["has_attachments"]
    assert by_uid[seeded.uids["encoded"]]["subject"] == "Grüße vom Team"
    assert by_uid[seeded.uids["plain"]]["from"] == ["Jürgen Müller <juergen@example.com>"]


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({"from": "billing@example.net"}, {"attach"}),
        ({"subject": "Kickoff"}, {"thread1", "thread2", "thread3"}),
        ({"unseen": True}, {"plain", "attach", "thread1", "thread2", "thread3"}),
        ({"flagged": True}, {"attach"}),
        ({"since": "2026-03-07"}, {"thread2", "thread3"}),
        ({"before": "2026-03-03"}, {"plain"}),
        ({"text": "Dienstag"}, {"thread2", "thread3"}),
        ({"subject": "Prüfung"}, {"plain"}),
    ],
)
def test_search_filters(server, seeded, args, expected):
    result = ok(call(server.url, "search_messages", {"account": "support", **args}))
    assert {m["uid"] for m in result["messages"]} == {seeded.uids[k] for k in expected}


def test_search_limit(server):
    result = ok(call(server.url, "search_messages", {"account": "support", "limit": 2}))
    assert result["returned"] == 2 and result["total_matches"] == 7
    assert "less than or equal" in error_text(call(server.url, "search_messages", {"account": "support", "limit": 51}))


# ---- reading --------------------------------------------------------------------------


def _is_unseen(uid: int) -> bool:
    client = imap_login("support")
    try:
        client.select_folder("INBOX", readonly=True)
        return b"\\Seen" not in client.get_flags([uid])[uid]
    finally:
        client.logout()


def test_get_message_keeps_unseen(server, seeded):
    uid = seeded.uids["plain"]
    assert _is_unseen(uid)
    result = ok(call(server.url, "get_message", {"account": "support", "folder": "INBOX", "uid": uid}))
    assert result["uid"] == uid and result["uidvalidity"] > 0
    assert result["content"]["subject"] == "Prüfung der Bestellung"
    assert "Grüße aus Köln" in result["content"]["body"]
    assert "untrusted" in result["content_note"].lower()
    assert _is_unseen(uid)


def test_get_message_html_and_truncation(server, seeded):
    args = {"account": "support", "folder": "INBOX", "uid": seeded.uids["html"], "max_chars": 100}
    result = ok(call(server.url, "get_message", args))
    assert result["content"]["body_format"] == "text/html"
    assert "Neuigkeiten" in result["content"]["body"]
    args["max_chars"] = 120
    long = ok(call(server.url, "get_message", {**args, "uid": seeded.uids["attach"]}))
    assert not long["content"]["truncated"]


def test_get_message_lists_attachments(server, seeded):
    args = {"account": "support", "folder": "INBOX", "uid": seeded.uids["attach"]}
    result = ok(call(server.url, "get_message", args))
    assert [a["filename"] for a in result["attachments"]] == ["positionen.csv", "rechnung.pdf", "logo.png"]


def test_get_message_unknown_uid(server):
    text = error_text(call(server.url, "get_message", {"account": "support", "folder": "INBOX", "uid": 99999}))
    assert "not found" in text and "uidvalidity" in text


def test_get_thread(server, seeded):
    for start in ("thread1", "thread2", "thread3"):
        args = {"account": "support", "folder": "INBOX", "uid": seeded.uids[start]}
        result = ok(call(server.url, "get_thread", args))
        assert [m["uid"] for m in result["messages"]] == [seeded.uids[f"thread{i}"] for i in (1, 2, 3)]
    assert _is_unseen(seeded.uids["thread1"])


def test_get_attachment_text_pdf_and_binary(server, seeded):
    base = {"account": "support", "folder": "INBOX", "uid": seeded.uids["attach"]}
    csv = ok(call(server.url, "get_attachment", {**base, "part": "2"}))
    assert csv["filename"] == "positionen.csv" and "Äpfel;3" in csv["content"]
    pdf = ok(call(server.url, "get_attachment", {**base, "part": "3"}))
    assert "Rechnung Nummer 42" in pdf["content"]
    png = ok(call(server.url, "get_attachment", {**base, "part": "4"}))
    assert png["content"] is None and png["content_type"] == "image/png" and png["size"] > 0
    text = error_text(call(server.url, "get_attachment", {**base, "part": "1"}))
    assert "Valid parts: 2, 3, 4" in text


# ---- logging --------------------------------------------------------------------------


def test_logs_contain_no_mail_data_or_secrets(server, seeded):
    call(server.url, "get_message", {"account": "support", "folder": "INBOX", "uid": seeded.uids["plain"]})
    call(server.url, "search_messages", {"account": "support", "subject": "Prüfung"})
    call(server.url, "get_message", {"account": "support", "folder": "Secret", "uid": 1})
    joined = "\n".join(server.logs.lines)
    assert "tool=get_message account=support" in joined
    for needle in ("Prüfung", "Grüße", "juergen@example.com", "Rechnung", "Kickoff", *PASSWORDS.values(), ADMIN_KEY):
        assert needle not in joined


# ---- phase 2 --------------------------------------------------------------------------


def test_write_tools_registered_only_with_readwrite_account(rw_server):
    assert tool_names(rw_server.url, key=None) >= WRITE_TOOLS


def test_write_tools_refuse_readonly_account(rw_server, seeded):
    args = {"account": "support", "folder": "INBOX", "uids": [seeded.uids["plain"]], "seen": True}
    assert "read-only" in error_text(call(rw_server.url, "set_flags", args, key=None))
    assert _is_unseen(seeded.uids["plain"])


def test_set_flags_move_and_draft(rw_server, seeded):
    uid = seeded.uids["w1"]
    flags = ok(
        call(
            rw_server.url,
            "set_flags",
            {"account": "writer", "folder": "INBOX", "uids": [uid], "seen": True, "flagged": True},
            key=None,
        )
    )
    assert set(flags["messages"][0]["flags"]) >= {"\\Seen", "\\Flagged"}

    moved = ok(
        call(
            rw_server.url,
            "move_messages",
            {"account": "writer", "folder": "INBOX", "uids": [uid], "target_folder": "Done"},
            key=None,
        )
    )
    new_uid = moved["moved"][0]["new_uid"]
    assert moved["moved"][0]["old_uid"] == uid and new_uid
    done = ok(call(rw_server.url, "get_message", {"account": "writer", "folder": "Done", "uid": new_uid}, key=None))
    assert done["content"]["subject"] == "Zu erledigen 1"

    draft = ok(
        call(
            rw_server.url,
            "create_draft",
            {
                "account": "writer",
                "to": ["kunde@example.com"],
                "subject": "Antwort",
                "body": "Hallo,\ndanke.",
                "in_reply_to": "<w2@example.net>",
            },
            key=None,
        )
    )
    assert draft["folder"] == "Drafts"
    found = ok(call(rw_server.url, "search_messages", {"account": "writer", "folder": "Drafts"}, key=None))
    assert [m["subject"] for m in found["messages"]] == ["Antwort"]
    assert "\\Draft" in found["messages"][0]["flags"]
