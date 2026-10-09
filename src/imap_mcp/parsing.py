"""MIME parsing: headers, readable body text, attachment listing and text extraction."""

from __future__ import annotations

import email
import email.policy
import io
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.utils import getaddresses, parsedate_to_datetime

import html2text

log = logging.getLogger(__name__)

TEXT_ATTACHMENT_TYPES = {
    "text/plain",
    "text/csv",
    "text/markdown",
    "text/html",
    "text/calendar",
    "application/json",
    "application/xml",
    "text/xml",
    "application/csv",
}
PDF_TYPES = {"application/pdf", "application/x-pdf"}


def parse_bytes(raw: bytes) -> EmailMessage:
    return email.message_from_bytes(raw, policy=email.policy.default)  # type: ignore[return-value]


def decode_mime_words(value: str | bytes | None) -> str:
    """Decode RFC 2047 encoded words; tolerant towards broken input."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def addresses(value: str | None) -> list[str]:
    if not value:
        return []
    result = []
    for name, addr in getaddresses([str(value)]):
        name = decode_mime_words(name)
        result.append(f"{name} <{addr}>" if name else addr)
    return result


def iso_date(value: str | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def html_to_text(html: str) -> str:
    converter = html2text.HTML2Text()
    converter.body_width = 0
    converter.ignore_images = True
    converter.ignore_emphasis = False
    converter.unicode_snob = True
    return converter.handle(html).strip()


def truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars] + "\n[… truncated]", True


def _decode_text(part: Message) -> str:
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class Part:
    """A leaf MIME part with its IMAP-style section number (e.g. '2' or '1.2')."""

    section: str
    message: Message

    @property
    def content_type(self) -> str:
        return self.message.get_content_type()

    @property
    def filename(self) -> str | None:
        name = self.message.get_filename()
        return decode_mime_words(name) if name else None

    @property
    def is_attachment(self) -> bool:
        disposition = (self.message.get_content_disposition() or "").lower()
        if disposition == "attachment":
            return True
        if disposition == "inline" and self.filename:
            return not self.content_type.startswith("text/")
        return bool(self.filename) or not (
            self.content_type.startswith("text/") or self.content_type.startswith("multipart/")
        )

    def size(self) -> int:
        return len(self.payload())

    def payload(self) -> bytes:
        if self.content_type == "message/rfc822":
            inner = self.message.get_payload()
            return inner[0].as_bytes() if isinstance(inner, list) and inner else b""
        return self.message.get_payload(decode=True) or b""


def walk_parts(msg: Message) -> Iterator[Part]:
    """Yield leaf parts numbered like IMAP body sections (RFC 3501, 6.4.5)."""
    yield from _walk(msg, "")


def _walk(msg: Message, prefix: str) -> Iterator[Part]:
    if msg.is_multipart():
        for index, child in enumerate(msg.get_payload(), start=1):  # type: ignore[arg-type]
            section = f"{prefix}.{index}" if prefix else str(index)
            if child.is_multipart() and child.get_content_type() != "message/rfc822":
                yield from _walk(child, section)
            else:
                yield Part(section, child)
    else:
        yield Part(prefix or "1", msg)


def body_text(msg: Message) -> tuple[str, str]:
    """Return (text, source) where source is 'text/plain', 'text/html' or 'none'."""
    plain: str | None = None
    html: str | None = None
    for part in walk_parts(msg):
        if part.is_attachment:
            continue
        if part.content_type == "text/plain" and plain is None:
            plain = _decode_text(part.message)
        elif part.content_type == "text/html" and html is None:
            html = _decode_text(part.message)
    if plain and plain.strip():
        return plain.strip(), "text/plain"
    if html:
        return html_to_text(html), "text/html"
    return "", "none"


def attachments(msg: Message) -> list[dict]:
    return [
        {
            "part": part.section,
            "filename": part.filename,
            "content_type": part.content_type,
            "size": part.size(),
        }
        for part in walk_parts(msg)
        if part.is_attachment
    ]


def find_part(msg: Message, section: str) -> Part | None:
    for part in walk_parts(msg):
        if part.section == section:
            return part
    return None


def headers(msg: Message) -> dict:
    return {
        "from": addresses(msg.get("From")),
        "to": addresses(msg.get("To")),
        "cc": addresses(msg.get("Cc")),
        "reply_to": addresses(msg.get("Reply-To")),
        "subject": decode_mime_words(msg.get("Subject")),
        "date": iso_date(msg.get("Date")),
        "message_id": (msg.get("Message-ID") or "").strip() or None,
    }


def thread_ids(msg: Message) -> list[str]:
    """Message-IDs referenced by this message (References + In-Reply-To), oldest first."""
    ids: list[str] = []
    for header in ("References", "In-Reply-To"):
        for token in str(msg.get(header) or "").split():
            token = token.strip()
            if token.startswith("<") and token.endswith(">") and token not in ids:
                ids.append(token)
    return ids


def extract_attachment_text(part: Part, max_chars: int) -> tuple[str | None, str]:
    """Return (text, note). Text is None when the type is not supported for extraction."""
    content_type = part.content_type
    payload = part.payload()
    filename = (part.filename or "").lower()
    if content_type in PDF_TYPES or filename.endswith(".pdf"):
        return _pdf_text(payload, max_chars)
    if content_type == "message/rfc822":
        inner = parse_bytes(payload)
        meta = headers(inner)
        text, _ = body_text(inner)
        head = f"From: {', '.join(meta['from'])}\nSubject: {meta['subject']}\nDate: {meta['date']}\n\n"
        text, cut = truncate(head + text, max_chars)
        return text, "truncated" if cut else "complete"
    if content_type in TEXT_ATTACHMENT_TYPES or filename.endswith((".txt", ".csv", ".md", ".json", ".xml")):
        text = _decode_text(part.message)
        if content_type == "text/html":
            text = html_to_text(text)
        text, cut = truncate(text, max_chars)
        return text, "truncated" if cut else "complete"
    return None, f"no text extraction for {content_type}"


def _pdf_text(payload: bytes, max_chars: int) -> tuple[str | None, str]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(payload))
        if reader.is_encrypted:
            return None, "PDF is encrypted"
        chunks: list[str] = []
        total = 0
        for page in reader.pages:
            text = page.extract_text() or ""
            chunks.append(text)
            total += len(text)
            if total > max_chars:
                break
    except Exception as exc:
        log.info("pdf extraction failed error=%s", type(exc).__name__)
        return None, "PDF could not be read"
    text, cut = truncate("\n\n".join(chunks).strip(), max_chars)
    if not text:
        return None, "PDF contains no extractable text (scanned?)"
    return text, "truncated" if cut else "complete"
