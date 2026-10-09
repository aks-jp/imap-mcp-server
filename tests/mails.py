"""Builders for the test messages: plain, HTML, umlauts, attachments, threads."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime

BASE_DATE = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)


def _base(subject: str, sender: str, day: int, message_id: str, to: str = "support@example.com") -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = format_datetime(BASE_DATE + timedelta(days=day))
    msg["Message-ID"] = message_id
    return msg


def plain_umlauts() -> bytes:
    msg = _base("Prüfung der Bestellung", "Jürgen Müller <juergen@example.com>", 0, "<plain-1@example.com>")
    msg.set_content("Grüße aus Köln!\nDie Bestellung über 3 Stück Größe XL ist unterwegs.\n")
    return msg.as_bytes()


def html_only() -> bytes:
    msg = _base("Newsletter", "News <news@example.org>", 1, "<html-1@example.org>")
    msg.set_content(
        "<html><body><h1>Neuigkeiten</h1><p>Hallo <b>Welt</b>, hier ist ein "
        '<a href="https://example.org/x">Link</a>.</p><script>alert(1)</script></body></html>',
        subtype="html",
    )
    return msg.as_bytes()


def with_attachments() -> bytes:
    msg = _base("Rechnung März", "Buchhaltung <billing@example.net>", 2, "<attach-1@example.net>")
    msg.set_content("Anbei die Rechnung und die Positionen.")
    msg.add_attachment(
        "Artikel;Menge\nÄpfel;3\nBirnen;5\n".encode(), maintype="text", subtype="csv", filename="positionen.csv"
    )
    msg.add_attachment(
        minimal_pdf("Rechnung Nummer 42"), maintype="application", subtype="pdf", filename="rechnung.pdf"
    )
    msg.add_attachment(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, maintype="image", subtype="png", filename="logo.png")
    return msg.as_bytes()


def encoded_subject() -> bytes:
    raw = plain_simple("x", "<encoded-1@example.com>", day=3).decode()
    return raw.replace("Subject: x", "Subject: =?utf-8?q?Gr=C3=BC=C3=9Fe_vom_Team?=").encode()


def plain_simple(
    subject: str, message_id: str, day: int = 0, to: str = "support@example.com", body: str = "Hallo."
) -> bytes:
    msg = _base(subject, "Alice <alice@example.com>", day, message_id, to=to)
    msg.set_content(body)
    return msg.as_bytes()


def thread() -> list[bytes]:
    first = _base("Projekt Kickoff", "Alice <alice@example.com>", 4, "<t1@example.com>")
    first.set_content("Wann passt euch der Kickoff?")
    second = _base("Re: Projekt Kickoff", "Bob <bob@example.com>", 5, "<t2@example.com>")
    second["In-Reply-To"] = "<t1@example.com>"
    second["References"] = "<t1@example.com>"
    second.set_content("Dienstag passt.")
    third = _base("Re: Projekt Kickoff", "Alice <alice@example.com>", 6, "<t3@example.com>")
    third["In-Reply-To"] = "<t2@example.com>"
    third["References"] = "<t1@example.com> <t2@example.com>"
    third.set_content("Dann Dienstag um 10 Uhr.")
    return [first.as_bytes(), second.as_bytes(), third.as_bytes()]


def minimal_pdf(text: str) -> bytes:
    """A one-page PDF with a single line of text, enough for pypdf's text extraction."""
    stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
