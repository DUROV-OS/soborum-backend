"""Приведение ответов MAX к простым JSON-структурам для фронта.

Перенос функций-«шейперов» из Desktop/max_idi_nahuy/api.py. Всё read-only.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status

from app.max.client import MediaError, session


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
        "lastMessage": _fmt_msg(last, viewer_id),
    }


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
    return {
        "chatId": chat_id,
        "title": _chat_title(meta, contacts, vid),
        "viewerId": vid,
        "count": len(msgs),
        "messages": [_fmt_msg(m, vid) for m in msgs],
    }


def send_message(chat_id, text: str, notify: bool = True) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Пустой текст")
    with session() as s:
        try:
            payload = s.send_message(chat_id, text, notify=notify)
        except (TimeoutError, RuntimeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
            ) from exc
        vid = s.viewer_id()
    return {
        "chatId": payload.get("chatId", chat_id),
        "message": _fmt_msg(payload.get("message"), vid),
    }


def get_attachment_url(chat_id, message_id, file_id) -> str:
    with session() as s:
        try:
            return s.attach_url(file_id, chat_id, message_id)
        except TimeoutError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
            ) from exc


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
