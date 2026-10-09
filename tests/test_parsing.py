from imap_mcp import parsing
from tests import mails


def test_plain_text_with_umlauts():
    msg = parsing.parse_bytes(mails.plain_umlauts())
    meta = parsing.headers(msg)
    assert meta["subject"] == "Prüfung der Bestellung"
    assert meta["from"] == ["Jürgen Müller <juergen@example.com>"]
    assert meta["date"].startswith("2026-03-02T09:00:00")
    body, source = parsing.body_text(msg)
    assert source == "text/plain"
    assert "Grüße aus Köln" in body
    assert parsing.attachments(msg) == []


def test_html_is_converted_to_text():
    body, source = parsing.body_text(parsing.parse_bytes(mails.html_only()))
    assert source == "text/html"
    assert "Neuigkeiten" in body and "**Welt**" in body
    assert "<p>" not in body


def test_attachments_are_listed_with_sections():
    msg = parsing.parse_bytes(mails.with_attachments())
    listed = parsing.attachments(msg)
    assert [(a["part"], a["filename"], a["content_type"]) for a in listed] == [
        ("2", "positionen.csv", "text/csv"),
        ("3", "rechnung.pdf", "application/pdf"),
        ("4", "logo.png", "image/png"),
    ]
    body, _ = parsing.body_text(msg)
    assert body == "Anbei die Rechnung und die Positionen."


def test_attachment_text_extraction():
    msg = parsing.parse_bytes(mails.with_attachments())
    csv_text, note = parsing.extract_attachment_text(parsing.find_part(msg, "2"), 1000)
    assert "Äpfel;3" in csv_text and note == "complete"
    pdf_text, _ = parsing.extract_attachment_text(parsing.find_part(msg, "3"), 1000)
    assert "Rechnung Nummer 42" in pdf_text
    png_text, png_note = parsing.extract_attachment_text(parsing.find_part(msg, "4"), 1000)
    assert png_text is None and "image/png" in png_note


def test_encoded_subject():
    meta = parsing.headers(parsing.parse_bytes(mails.encoded_subject()))
    assert meta["subject"] == "Grüße vom Team"


def test_thread_ids():
    third = parsing.parse_bytes(mails.thread()[2])
    assert parsing.thread_ids(third) == ["<t1@example.com>", "<t2@example.com>"]


def test_truncate():
    assert parsing.truncate("abc", 5) == ("abc", False)
    text, cut = parsing.truncate("abcdef", 3)
    assert cut and text.startswith("abc") and "truncated" in text


def test_attached_message_is_a_leaf():
    from email.message import EmailMessage

    outer = EmailMessage()
    outer["Subject"] = "Fwd"
    outer.set_content("see attached")
    outer.add_attachment(parsing.parse_bytes(mails.plain_umlauts()))
    msg = parsing.parse_bytes(outer.as_bytes())
    listed = parsing.attachments(msg)
    assert [a["content_type"] for a in listed] == ["message/rfc822"]
    text, _ = parsing.extract_attachment_text(parsing.find_part(msg, listed[0]["part"]), 1000)
    assert "Prüfung der Bestellung" in text and "Grüße aus Köln" in text
