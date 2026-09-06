"""Общий помощник для задач, которые пишут в базу знаний через Claude.

В отличие от чата Марины (app/ai/engine.py), где удалённый MCP-коннектор
намеренно ограничен чтением и только для админа, фоновые задачи —
доверенный код без человека в контуре, поэтому здесь MCP-коннектору
разрешены и инструменты записи (append_note / create_note / …).

MCP-коннектор Anthropic исполняет инструменты на своей стороне в рамках
одного вызова messages.create (та же схема, что в app/board/conductor.py):
локального цикла исполнения инструментов тут нет, мы лишь дочитываем
итоговый текст и, если ответ упёрся в лимит длины, просим продолжить.
"""

from __future__ import annotations

import logging

import anthropic
from sqlalchemy.orm import Session

from app.ai import mcp_auth
from app.ai import model_profiles
from app.ai.model_profiles import ModelProfile
from app.core.config import settings

logger = logging.getLogger(__name__)

MCP_BETA = "mcp-client-2025-04-04"

# Полный набор инструментов KB-сервера: чтение + запись. Имена — как их
# отдаёт MCP-сервер базы знаний.
KB_READ_TOOLS = ["read_index", "list_notes", "search_notes", "read_note", "get_unread_files"]
KB_WRITE_TOOLS = ["create_note", "append_note", "edit_note", "str_replace_note", "insert_in_note"]

MAX_CONTINUATIONS = 4


class KbAgentUnavailable(RuntimeError):
    """MCP не настроен или нет ключа Anthropic — задача пропускается, не падает."""


def _client() -> anthropic.Anthropic:
    if not settings.anthropic_api_key:
        raise KbAgentUnavailable("ANTHROPIC_API_KEY не задан")
    if not settings.mcp_configured:
        raise KbAgentUnavailable("MCP базы знаний не настроен (MCP_SERVER_URL/CLIENT_ID/SECRET)")
    return anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=120.0, max_retries=1)


def run_kb_agent(
    db: Session,
    *,
    system: str,
    user_content: str | list[dict],
    allow_write: bool = True,
    max_tokens: int = 8192,
    profile: ModelProfile = ModelProfile.DAILY_JOB,
) -> str:
    """Один «проход» агента над базой знаний. Возвращает весь текст, который
    модель написала (для лога/итога). Бросает KbAgentUnavailable, если
    интеграция не сконфигурирована.

    profile — какая модель/усилие: по умолчанию DAILY_JOB (Sonnet, low),
    для еженедельных задач передайте ModelProfile.WEEKLY_JOB (Sonnet, medium)."""
    client = _client()
    call_params = model_profiles.profile_params(profile)

    allowed = KB_READ_TOOLS + (KB_WRITE_TOOLS if allow_write else [])
    mcp_server = {
        "type": "url",
        "url": settings.mcp_server_url,
        "name": "knowledge-base",
        "authorization_token": mcp_auth.get_access_token(db),
        "tool_configuration": {"allowed_tools": allowed},
    }

    messages: list[dict] = [
        {"role": "user", "content": user_content if isinstance(user_content, list) else [
            {"type": "text", "text": user_content}
        ]}
    ]

    collected: list[str] = []
    for _ in range(MAX_CONTINUATIONS):
        response = client.beta.messages.create(
            betas=[MCP_BETA],
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            mcp_servers=[mcp_server],
            **call_params,
        )
        text = "".join(
            b.text for b in response.content if getattr(b, "type", None) == "text"
        ).strip()
        if text:
            collected.append(text)

        if response.stop_reason != "max_tokens":
            break

        # Упёрлись в лимит длины — просим продолжить с того же места.
        messages.append({"role": "assistant", "content": [b.model_dump(mode="json") for b in response.content]})
        messages.append({"role": "user", "content": [{"type": "text", "text": "Продолжай."}]})

    return "\n\n".join(collected).strip()
