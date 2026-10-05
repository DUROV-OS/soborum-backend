"""Приведение ответов MAX к простым JSON-структурам для фронта.

Перенос функций-«шейперов» из Desktop/max_idi_nahuy/api.py. Всё read-only.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
from fastapi import HTTPException, status

from app.max import realtime
from app.max.client import ContactError, MediaError, UploadError, session


def _sid(v: Any) -> str | None:
    """ID-снежинки MAX (message id, fileId, ...) не влезают в JS Number
    (> 2**53), поэтому отдаём их строкой — иначе фронт округлит и не
    сможет вернуть точное значение в /attachment."""
    return None if v is None else str(v)


def _fmt_attach(a: dict) -> dict:
    d = {
        "type": a.get("_type"),
        "name": a.get("name"),
        "fileId": _sid(a.get("fileId")),
        "photoId": _sid(a.get("photoId")),
        "videoId": _sid(a.get("videoId")),
        "audioId": _sid(a.get("audioId")),
        "size": a.get("size"),
        "baseUrl": a.get("baseUrl"),
        "url": a.get("url"),
        "title": a.get("title"),
        # VIDEO: длительность (мс), кадр-постер (data:image/webp) и его URL;
        # AUDIO (голосовое): длительность и картинка-волна (data:image/webp).
        "duration": a.get("duration"),
        "previewData": a.get("previewData"),
        "thumbnail": a.get("thumbnail"),
        "wave": a.get("wave"),
    }
    return {k: v for k, v in d.items() if v is not None}


# CONTROL-вложения MAX — служебные события чата (вступил / вышел / переименовал).
# Показываем их отдельной строкой, а не как чьё-то сообщение.
_CONTROL_EVENT_TEXT = {
    "new": "чат создан",
    "add": "добавил участника",
    "remove": "удалил участника",
    "leave": "вышел из чата",
    "joinByLink": "присоединился по ссылке",
    "title": "изменил название чата",
    "pin": "закрепил сообщение",
    "unpin": "открепил сообщение",
    "photo": "изменил фото чата",
}


def _system_text(m: dict) -> str | None:
    for a in m.get("attaches", []):
        if a.get("_type") == "CONTROL":
            ev = a.get("event")
            if ev == "title" and a.get("title"):
                return f"изменил название чата на «{a['title']}»"
            return _CONTROL_EVENT_TEXT.get(ev, "служебное сообщение")
    return None


def _fmt_msg(
    m: dict | None, viewer_id: str = "", contacts: dict | None = None
) -> dict | None:
    if not m:
        return None
    contacts = contacts or {}
    sender = m.get("sender")
    sender_id = _sid(sender)
    is_system = any(
        a.get("_type") == "CONTROL" for a in m.get("attaches", [])
    )
    return {
        "id": _sid(m.get("id")),
        "time": m.get("time"),
        # id участника MAX (стабильный, строкой) — по нему фронт группирует
        # подряд идущие сообщения и подписывает автора в группах.
        "senderId": sender_id,
        "senderName": _contact_name(contacts.get(sender_id)) if sender_id else None,
        # исходящее = автор совпал с текущим пользователем; тип чата ни при чём.
        # служебное событие никогда не «исходящее».
        "isOutgoing": (
            not is_system and viewer_id != "" and str(sender) == str(viewer_id)
        ),
        "isSystem": is_system,
        "systemText": _system_text(m) if is_system else None,
        "type": m.get("type"),
        "status": m.get("status"),
        "text": m.get("text", ""),
        "elements": m.get("elements", []),
        "attaches": [_fmt_attach(a) for a in m.get("attaches", [])],
        "forwarded": _fmt_forwarded(m, contacts),
    }


def _fmt_forwarded(m: dict, contacts: dict) -> dict | None:
    """Пересланное сообщение (0098): у самого сообщения текст и вложения
    пустые, оригинал — в ``link.message``, исходный чат — в ``link.chatId``."""
    link = m.get("link") or {}
    if link.get("type") != "FORWARD":
        return None
    orig = link.get("message") or {}
    sender_id = _sid(orig.get("sender"))
    return {
        "senderId": sender_id,
        "senderName": _contact_name(contacts.get(sender_id)) if sender_id else None,
        "chatId": link.get("chatId"),
        "text": orig.get("text", ""),
        "attaches": [_fmt_attach(a) for a in orig.get("attaches", [])],
    }


def _contact_name(ct: dict | None) -> str | None:
    if not ct:
        return None
    names = ct.get("names")
    if isinstance(names, list) and names:
        n = names[0]
        cand = n.get("name") or " ".join(
            x for x in (n.get("firstName"), n.get("lastName")) if x
        )
        if cand:
            return cand
    return ct.get("name") or ct.get("firstName") or ct.get("phone")


def _dialog_peer_id(chat_id, viewer_id: str) -> str | None:
    """Собеседник личного диалога: id диалога MAX = id аккаунта XOR id
    собеседника (так считает web.max.ru; сверено на всех диалогах аккаунта).
    У групповых чатов id отрицательный — для них собеседника нет."""
    try:
        cid, vid = int(chat_id), int(viewer_id)
    except (TypeError, ValueError):
        return None
    if cid <= 0 or not vid:
        return None
    return str(cid ^ vid)


def _chat_title(c: dict | None, contacts: dict, viewer_id: str = "") -> str | None:
    if not c:
        return None
    if c.get("title"):
        return c["title"]
    # диалог: имя собеседника
    peers = [p for p in (c.get("participants") or {}) if str(p) != viewer_id]
    for pid in (peers or list(c.get("participants") or {})):
        name = _contact_name(contacts.get(str(pid)))
        if name:
            return name
    return str(c.get("id"))


def _fmt_chat(c: dict, last_map: dict, contacts: dict, viewer_id: str = "") -> dict:
    cid = c.get("id")
    msgs = last_map.get(str(cid)) or last_map.get(cid) or []
    last = c.get("lastMessage") or (msgs[-1] if msgs else None)
    return {
        "id": cid,
        "type": c.get("type"),
        "title": _chat_title(c, contacts, viewer_id),
        "unread": c.get("newMessages", c.get("unreadCount", 0)),
        "lastEventTime": c.get("lastEventTime") or c.get("lastFireTime") or (last or {}).get("time"),
        "lastMessage": _fmt_msg(last, viewer_id, contacts),
    }


def _is_group_chat(meta: dict | None) -> bool:
    """Групповой чат MAX — всё, что не диалог 1:1 (тип ``DIALOG``)."""
    return bool(meta) and meta.get("type") != "DIALOG"


def list_chats(limit: int | None = None) -> dict[str, Any]:
    with session() as s:
        contacts = s.contacts_by_id()
        last_map = s.last_messages()
        vid = s.viewer_id()
        items = [_fmt_chat(c, last_map, contacts, vid) for c in s.chats()]
    items.sort(key=lambda x: x.get("lastEventTime") or 0, reverse=True)
    if limit:
        items = items[:limit]
    return {"count": len(items), "chats": items}


def get_chat(chat_id, limit: int = 50, backward: int = 0) -> dict[str, Any]:
    with session() as s:
        contacts = s.contacts_by_id()
        vid = s.viewer_id()
        meta = next((c for c in s.chats() if c.get("id") == chat_id), None)
        msgs = s.history(chat_id, forward=limit, backward=backward)
    title = _chat_title(meta, contacts, vid)
    if meta is None:
        # диалога нет среди последних чатов аккаунта — например, только что
        # добавленный контакт без сообщений (0093): имя берём из контакта.
        title = _contact_name(contacts.get(_dialog_peer_id(chat_id, vid) or ""))
    return {
        "chatId": chat_id,
        "title": title,
        "viewerId": vid,
        "isGroup": _is_group_chat(meta),
        "count": len(msgs),
        "messages": [_fmt_msg(m, vid, contacts) for m in msgs],
    }


def send_message(
    chat_id,
    text: str,
    notify: bool = True,
    file: tuple[bytes, str, str] | None = None,
) -> dict[str, Any]:
    """Текст и/или файл (``file`` — ``(данные, имя, content_type)``). Пустой
    текст допустим только вместе с файлом — иначе отправлять нечего."""
    text = (text or "").strip()
    if not text and not file:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Пустой текст или вложение")
    with session() as s:
        attaches = None
        if file is not None:
            data, filename, content_type = file
            try:
                uploaded = s.upload_file(data, filename, content_type)
            except UploadError as exc:
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
            attaches = [{"_type": "FILE", "fileId": uploaded["fileId"]}]
        try:
            payload = s.send_message(chat_id, text, notify=notify, attaches=attaches)
        except (TimeoutError, RuntimeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
            ) from exc
        vid = s.viewer_id()
        contacts = s.contacts_by_id()
    # коллеги с этим чатом в соседних вкладках увидят ответ сразу (0092)
    realtime.chat_updated(payload.get("chatId", chat_id))
    return {
        "chatId": payload.get("chatId", chat_id),
        "message": _fmt_msg(payload.get("message"), vid, contacts),
    }


def forward_message(from_chat_id, message_id: str, to_chat_id, notify: bool = True) -> dict[str, Any]:
    """Переслать сообщение ``message_id`` из ``from_chat_id`` в ``to_chat_id``."""
    with session() as s:
        try:
            payload = s.forward_message(to_chat_id, from_chat_id, message_id, notify=notify)
        except (TimeoutError, RuntimeError) as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        vid = s.viewer_id()
        contacts = s.contacts_by_id()
    realtime.chat_updated(payload.get("chatId", to_chat_id))
    return {
        "chatId": payload.get("chatId", to_chat_id),
        "message": _fmt_msg(payload.get("message"), vid, contacts),
    }


def edit_message(chat_id, message_id: str, text: str) -> dict[str, Any]:
    """Изменить текст своего сообщения. MSG_EDIT заменяет вложения целиком,
    поэтому FILE передаём заново, а сообщения с другими вложениями (фото,
    видео, голосовые...) не правим: их сохранение при правке не проверено, и
    MAX мог бы их удалить."""
    text = (text or "").strip()
    with session() as s:
        try:
            orig = s.get_message(chat_id, message_id)
        except (TimeoutError, RuntimeError) as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        if not orig:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Сообщение не найдено")
        vid = s.viewer_id()
        if str(orig.get("sender")) != str(vid):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Изменить можно только своё сообщение")
        if (orig.get("link") or {}).get("type") == "FORWARD":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Пересланное сообщение изменить нельзя")
        attaches = orig.get("attaches") or []
        if any(a.get("_type") != "FILE" for a in attaches):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Сообщение с фото, видео или другим вложением изменить нельзя — MAX удалит вложение",
            )
        files = [{"_type": "FILE", "fileId": a["fileId"]} for a in attaches]
        if not text and not files:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Пустой текст")
        try:
            payload = s.edit_message(chat_id, message_id, text, files)
        except (TimeoutError, RuntimeError) as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        contacts = s.contacts_by_id()
    realtime.chat_updated(chat_id)
    return {"chatId": chat_id, "message": _fmt_msg(payload.get("message"), vid, contacts)}


def normalize_phone(raw: str) -> str:
    """Номер к виду MAX: только цифры с кодом страны. ``8XXXXXXXXXX`` и
    10 цифр без кода — российский номер, приводим к ``7XXXXXXXXXX``."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    if not 11 <= len(digits) <= 15:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Некорректный номер телефона")
    return digits


def start_dialog(phone: str, first_name: str, last_name: str | None = None) -> dict[str, Any]:
    """Найти человека в MAX по номеру и вернуть его личный диалог (0093).

    Нет в контактах аккаунта — добавляем с введённым именем; уже есть —
    не трогаем (имя в MAX не переписываем). Сообщений не отправляет: диалог
    появится в списке чатов после первого сообщения сотрудника."""
    phone = normalize_phone(phone)
    first_name = first_name.strip()
    last_name = (last_name or "").strip() or None
    if not first_name:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Укажите имя контакта")
    with session() as s:
        vid = s.viewer_id()
        contacts = s.contacts_by_id()
        try:
            found = s.contact_by_phone(phone)
        except ContactError as exc:
            if exc.code == "not.found":
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Этот номер не зарегистрирован в MAX"
                ) from exc
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        contact_id = _sid(found.get("id"))
        if not contact_id:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="MAX не вернул контакт по номеру")
        if contact_id == vid:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Это номер аккаунта организации"
            )

        already = contact_id in contacts
        contact = contacts.get(contact_id) or found
        if not already:
            try:
                added = s.add_contact(phone, first_name, last_name)
            except (ContactError, TimeoutError) as exc:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY, detail=f"MAX не добавил контакт: {exc}"
                ) from exc
            contact = added.get("contact") or contact
    name = _contact_name(contact) or " ".join(x for x in (first_name, last_name) if x)
    return {
        # id личного диалога — XOR id аккаунта и собеседника (см. _dialog_peer_id)
        "chatId": int(contact_id) ^ int(vid),
        "contactId": contact_id,
        "name": name,
        "alreadyContact": already,
    }


def get_attachment_url(chat_id, message_id, file_id) -> str:
    with session() as s:
        try:
            return s.attach_url(file_id, chat_id, message_id)
        except TimeoutError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
            ) from exc


# Расширения, которые можно показать прямо в браузере (0031). Список — сами
# content-type: MAX не отдаёт mime-type вложения, только имя файла, поэтому
# тип определяем строго по этой карте, не через mimetypes.guess_type — так
# эндпоинт не превращается в открытый прокси произвольного content-type по
# присланному расширению.
PREVIEWABLE_EXTENSIONS: dict[str, str] = {
    "pdf": "application/pdf",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "txt": "text/plain; charset=utf-8",
}

# Крупный файл не тащим в браузер — сразу отдаём как для скачивания
# (см. GET /attachment, одноразовая ссылка без прокси).
PREVIEW_MAX_SIZE = 15 * 1024 * 1024


def get_attachment_preview(chat_id, message_id, file_id, filename: str) -> tuple[bytes, str]:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    content_type = PREVIEWABLE_EXTENSIONS.get(ext)
    if content_type is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Предпросмотр не поддерживается для .{ext or '?'} — скачайте файл",
        )

    url = get_attachment_url(chat_id, message_id, file_id)
    try:
        with httpx.stream("GET", url, timeout=30, follow_redirects=True) as resp:
            resp.raise_for_status()
            content_length = resp.headers.get("content-length")
            if content_length and int(content_length) > PREVIEW_MAX_SIZE:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="Файл больше 15 МБ — скачайте вместо предпросмотра",
                )
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total > PREVIEW_MAX_SIZE:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="Файл больше 15 МБ — скачайте вместо предпросмотра",
                    )
                chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    return b"".join(chunks), content_type


def _best_mp4(payload: dict) -> str | None:
    """Из ответа opcode 83 выбираем самый качественный прямой MP4."""
    def _res(key: str) -> int:
        tail = key.split("_", 1)[1]
        return int(tail) if tail.isdigit() else 0

    keys = sorted((k for k in payload if k.startswith("MP4_")), key=_res, reverse=True)
    return payload[keys[0]] if keys else None


def get_media_url(chat_id, message_id, media_id) -> dict[str, Any]:
    """Воспроизводимая ссылка на VIDEO или AUDIO (голосовое) вложение.

    ``media_id`` — ``videoId`` либо ``audioId`` из attach (строка-снежинка).
    Возвращает ``{ "url": <прямой MP4 | None>, "external": <веб-плеер | None> }``.
    """
    with session() as s:
        try:
            payload = s.media_url(media_id, chat_id, message_id)
        except MediaError as exc:
            # вложение недоступно/удалено — фронт покажет заглушку, не крутилку
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        except TimeoutError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
            ) from exc

    url = _best_mp4(payload)
    external = payload.get("EXTERNAL")
    if not url and not external:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="MAX не вернул воспроизводимую ссылку",
        )
    return {"url": url, "external": external}
