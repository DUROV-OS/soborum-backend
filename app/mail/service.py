"""Server-side IMAP. EXAMINE and BODY.PEEK never change shared read flags."""

import imaplib
import re
import ssl
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from threading import BoundedSemaphore

from fastapi import HTTPException

from app.core.config import settings

MAX_MESSAGE_BYTES = 20 * 1024 * 1024
_slots = BoundedSemaphore(4)


def configured():
    return bool(settings.shared_mail_enabled and settings.shared_mail_password.get_secret_value())


def checked(result):
    status, data = result
    if status != "OK":
        raise HTTPException(502, "Почтовый сервер не выполнил запрос. Повторите позже.")
    return data


@contextmanager
def inbox(validity: str | None = None):
    if not configured():
        raise HTTPException(503, "Общий почтовый ящик ещё не подключён. Обратитесь к администратору.")
    if not _slots.acquire(blocking=False):
        raise HTTPException(503, "Почта занята. Повторите через несколько секунд.")
    connection = None
    try:
        connection = imaplib.IMAP4_SSL(
            settings.shared_mail_host, settings.shared_mail_port,
            ssl_context=ssl.create_default_context(), timeout=20,
        )
        checked(connection.login(settings.shared_mail_address, settings.shared_mail_password.get_secret_value()))
        checked(connection.select("INBOX", readonly=True))
        _, values = connection.response("UIDVALIDITY")
        current = values[0].decode("ascii") if values and values[0] else ""
        if not current.isdecimal():
            raise HTTPException(502, "Сервер не подтвердил версию почтового ящика.")
        if validity is not None and current != validity:
            raise HTTPException(409, "Почтовый ящик изменился. Обновите список писем.")
        yield connection, current
    except (imaplib.IMAP4.error, OSError, UnicodeError):
        # Provider errors may echo account information. Do not return/log them.
        raise HTTPException(502, "Не удалось подключиться к почте NIC.RU. Администратору нужно проверить доступ и настройки.") from None
    finally:
        if connection is not None:
            try:
                connection.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
        _slots.release()


def headers(message):
    return {key: str(message.get(name, "")) for key, name in (
        ("subject", "Subject"), ("sender", "From"), ("to", "To"),
        ("cc", "Cc"), ("date", "Date"),
    )}


def list_messages(before: int | None = None, limit: int = 30):
    with inbox() as (connection, validity):
        # UIDs, not sequence numbers: incoming/deleted messages cannot shift pages.
        criterion = "ALL" if before is None else f"UID 1:{before - 1}"
        data = checked(connection.uid("search", None, criterion)) if before != 1 else [b""]
        uids = sorted({int(uid) for uid in (data[0] or b"").split()}, reverse=True)
        if before is not None:
            uids = [uid for uid in uids if uid < before]
        chosen = uids[:limit]
        items = []
        if chosen:
            data = checked(connection.uid("fetch", ",".join(map(str, chosen)),
                "(UID FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO CC DATE)])"))
            for row in data:
                if not isinstance(row, tuple):
                    continue
                meta, raw = row
                uid_match = re.search(rb"\bUID (\d+)", meta)
                size_match = re.search(rb"\bRFC822.SIZE (\d+)", meta)
                if not uid_match:
                    continue
                uid = int(uid_match[1])
                if uid not in chosen:
                    continue
                message = BytesParser(policy=policy.default).parsebytes(raw)
                items.append({"uid": uid, **headers(message),
                    "unread": b"\\Seen" not in imaplib.ParseFlags(meta),
                    "size": int(size_match[1]) if size_match else None})
        return {"items": sorted(items, key=lambda item: item["uid"], reverse=True),
                "uidvalidity": validity, "next_before": chosen[-1] if len(uids) > limit else None}


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head"}:
            self.hidden += 1
        elif not self.hidden and tag in {"p", "br", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def message_text(message):
    if not message.is_multipart():
        body = message if message.get_content_type() in {"text/plain", "text/html"} else None
    else:
        body = message.get_body(preferencelist=("plain", "html"))
    if body is None:
        return ""
    raw = body.get_payload(decode=True) or b""
    try:
        text = raw.decode(body.get_content_charset() or "utf-8", errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
    if body.get_content_type() == "text/html":
        parser = TextOnly()
        parser.feed(text)
        return "".join(parser.parts).strip()
    return text


def read_raw(uid: int, validity: str):
    with inbox(validity) as (connection, _):
        sizes = checked(connection.uid("fetch", str(uid), "(UID RFC822.SIZE)"))
        meta = b" ".join(row for row in sizes if isinstance(row, bytes))
        match = re.search(rb"\bRFC822.SIZE (\d+)", meta)
        if not match:
            raise HTTPException(404, "Письмо больше не найдено. Обновите список.")
        if int(match[1]) > MAX_MESSAGE_BYTES:
            raise HTTPException(413, "Письмо больше 20 МБ. Откройте его в веб-почте NIC.RU.")
        data = checked(connection.uid("fetch", str(uid), f"(UID BODY.PEEK[]<0.{MAX_MESSAGE_BYTES + 1}>)"))
        for row in data:
            if isinstance(row, tuple) and re.search(rb"\bUID " + str(uid).encode() + rb"\b", row[0]):
                if len(row[1]) > MAX_MESSAGE_BYTES:
                    raise HTTPException(413, "Письмо больше 20 МБ. Откройте его в веб-почте NIC.RU.")
                return BytesParser(policy=policy.default).parsebytes(row[1])
        raise HTTPException(404, "Письмо больше не найдено. Обновите список.")


def attachments(message):
    # Include inline files; never render them or fetch remote image URLs.
    return [(index, part) for index, part in enumerate(message.walk())
            if not part.is_multipart() and (part.get_filename() or part.get_content_disposition() == "attachment")]


def read_message(uid: int, validity: str):
    message = read_raw(uid, validity)
    return {"uid": uid, **headers(message), "text": message_text(message),
            "attachments": [{"part": index, "filename": part.get_filename() or "attachment",
                             "size": len(part.get_payload(decode=True) or b"")}
                            for index, part in attachments(message)]}
