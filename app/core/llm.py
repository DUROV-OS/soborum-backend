"""Shared LLM client. Claude (Anthropic) or ChatGPT (OpenAI), picked by
settings.ai_provider.

Every AI feature is written against the Anthropic Messages shape: system +
messages of content blocks (text / image / document / tool_use / tool_result),
tools with input_schema, tool_choice, and an anthropic.types.Message back.
Chat history is persisted in that shape too. So for OpenAI we do not rewrite
the callers - llm_client() hands back an adapter with the same
messages.create(...) surface that translates to the OpenAI Responses API and
converts the answer back into an anthropic.types.Message.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import anthropic
import httpx
import openai
from anthropic.types import Message

from app.core.config import settings

log = logging.getLogger("app.core.llm")

# Anthropic's own default. We always pass base_url explicitly: docker-compose
# injects ANTHROPIC_BASE_URL into the container even when unset, as "", and the
# SDK reads that env var itself - an empty string is not None, so it becomes a
# schemeless base_url and every call dies as APIConnectionError.
_DEFAULT_BASE_URL = "https://api.anthropic.com"
# Same trap with OPENAI_BASE_URL in the openai SDK.
_OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"

# Some Cloudflare workers reject the default Python client signature (error 1010).
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# What a provider call can raise, whichever provider is active. Callers catch
# this instead of anthropic.APIError.
LLM_ERRORS: tuple[type[BaseException], ...] = (anthropic.APIError, openai.APIError)


def normalize_anthropic_base_url(raw: str) -> str | None:
    url = (raw or "").strip().rstrip("/")
    if not url:
        return None
    if url.endswith("/v1"):
        url = url[:-3].rstrip("/")
    return url or None


def anthropic_client(*, timeout: float = 60.0, max_retries: int = 3) -> anthropic.Anthropic:
    base = normalize_anthropic_base_url(settings.anthropic_base_url)
    kwargs: dict = {
        "api_key": settings.anthropic_api_key,
        "timeout": timeout,
        "max_retries": max_retries,
        "base_url": base or _DEFAULT_BASE_URL,
    }
    if base:
        kwargs["default_headers"] = {"User-Agent": _BROWSER_UA}
    return anthropic.Anthropic(**kwargs)


def openai_client(*, timeout: float = 60.0, max_retries: int = 3) -> openai.OpenAI:
    base = (settings.openai_base_url or "").strip().rstrip("/")
    return openai.OpenAI(
        api_key=settings.openai_api_key,
        base_url=base or _OPENAI_DEFAULT_BASE_URL,
        timeout=timeout,
        max_retries=max_retries,
    )


def llm_client(*, timeout: float = 60.0, max_retries: int = 3):
    """Client of the active provider with the Anthropic messages.create surface."""
    if settings.ai_provider == "openai":
        return OpenAIMessagesClient(openai_client(timeout=timeout, max_retries=max_retries))
    return anthropic_client(timeout=timeout, max_retries=max_retries)


# --- OpenAI adapter -----------------------------------------------------------

# Anthropic effort levels -> OpenAI reasoning.effort. OpenAI has no xhigh/max.
_EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}
# On reasoning models (gpt-5*, o*) hidden reasoning tokens are counted against
# max_output_tokens. Callers size max_tokens for the visible answer only (some
# as low as 1024), so without headroom the budget can be spent on thinking and
# the answer comes back empty. Unused headroom is not billed.
_REASONING_HEADROOM_TOKENS = 8000
# Callers that pass no effort (every one-shot extraction call) get "low":
# those are structured tool calls, and Claude runs them without thinking.
_DEFAULT_REASONING_EFFORT = "low"


def _is_reasoning_model(model: str) -> bool:
    name = (model or "").lower()
    return name.startswith(("gpt-5", "o1", "o3", "o4"))


class OpenAIMessagesClient:
    """Anthropic-shaped facade over openai.OpenAI().responses.

    client.beta.messages is the same object: the only beta our callers use is
    the MCP connector, and Responses supports remote MCP natively."""

    def __init__(self, client: openai.OpenAI):
        self._client = client
        self.messages = _Messages(client)
        self.beta = SimpleNamespace(messages=self.messages)


class _Messages:
    def __init__(self, client: openai.OpenAI):
        self._client = client

    def create(self, *, betas=None, mcp_servers=None, **kwargs) -> Message:
        response = self._client.responses.create(**to_openai_request(mcp_servers=mcp_servers, **kwargs))
        return to_anthropic_message(response)

    def stream(self, *, betas=None, mcp_servers=None, **kwargs) -> "_MessageStream":
        return _MessageStream(self._client, to_openai_request(mcp_servers=mcp_servers, **kwargs))


def _event(type_: str, **fields) -> SimpleNamespace:
    return SimpleNamespace(type=type_, **fields)


class _MessageStream:
    """Context manager with the slice of anthropic's MessageStream that
    app.ai.engine reads: iterate content_block_start / content_block_delta
    (text_delta) / content_block_stop events, then get_final_message()."""

    def __init__(self, client: openai.OpenAI, request: dict):
        self._client = client
        self._request = request
        self._source = None
        self._final = None

    def __enter__(self) -> "_MessageStream":
        self._source = self._client.responses.create(**self._request, stream=True)
        return self

    def __exit__(self, *exc) -> None:
        close = getattr(self._source, "close", None)
        if close is not None:
            close()

    def __iter__(self):
        for event in self._source:
            etype = getattr(event, "type", "")
            if etype == "response.output_item.added":
                start = _block_start(event.item)
                if start is not None:
                    yield _event("content_block_start", content_block=start)
            elif etype == "response.output_text.delta":
                yield _event("content_block_delta", delta=SimpleNamespace(type="text_delta", text=event.delta))
            elif etype == "response.output_item.done":
                if getattr(event.item, "type", None) == "message":
                    yield _event("content_block_stop")
            elif etype in ("response.completed", "response.incomplete"):
                self._final = event.response
            elif etype == "response.failed":
                error = getattr(event.response, "error", None)
                raise _stream_error(getattr(error, "message", None) or "response failed", error)
            elif etype == "error":
                raise _stream_error(getattr(event, "message", None) or "stream error", event)

    def get_final_message(self) -> Message:
        if self._final is None:
            for _ in self:
                pass
        if self._final is None:
            raise _stream_error("stream ended without a final response", None)
        return to_anthropic_message(self._final)


def _block_start(item) -> SimpleNamespace | None:
    """Output item -> the content_block the engine reads for its status line."""
    itype = getattr(item, "type", None)
    if itype == "message":
        return SimpleNamespace(type="text", text="")
    if itype == "function_call":
        return SimpleNamespace(type="tool_use", name=item.name)
    if itype == "web_search_call":
        return SimpleNamespace(type="server_tool_use", name="web_search")
    if itype == "mcp_call":
        return SimpleNamespace(type="server_tool_use", name=f"{item.server_label}:{item.name}")
    return None


def _stream_error(message: str, body) -> openai.APIError:
    # Same exception family as a failed HTTP call, so LLM_ERRORS catches it.
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    return openai.APIError(message, request, body=body)


def to_openai_request(
    *,
    model: str,
    max_tokens: int,
    messages: list[dict],
    system: str | list | None = None,
    tools: list[dict] | None = None,
    tool_choice: dict | None = None,
    output_config: dict | None = None,
    mcp_servers: list[dict] | None = None,
    **ignored,
) -> dict:
    """Anthropic messages.create kwargs -> OpenAI responses.create kwargs.
    Anthropic-only knobs (thinking, temperature on reasoning models, metadata,
    ...) are dropped rather than guessed at."""
    if ignored:
        log.debug("openai adapter drops unsupported kwargs: %s", sorted(ignored))
    request: dict = {
        "model": model,
        "input": _to_input(messages),
        "max_output_tokens": max_tokens,
        # We replay the full history every turn, nothing to keep on OpenAI's side.
        "store": False,
    }
    instructions = _system_text(system)
    if instructions:
        request["instructions"] = instructions
    converted_tools = [t for t in (_to_tool(tool) for tool in tools or []) if t is not None]
    converted_tools += [_to_mcp_tool(server) for server in mcp_servers or []]
    if converted_tools:
        request["tools"] = converted_tools
        if tool_choice:
            request["tool_choice"] = _to_tool_choice(tool_choice)
    if _is_reasoning_model(model):
        effort = _EFFORT.get(str((output_config or {}).get("effort") or "").lower(), _DEFAULT_REASONING_EFFORT)
        request["reasoning"] = {"effort": effort}
        request["max_output_tokens"] = max_tokens + _REASONING_HEADROOM_TOKENS
    return request


def _system_text(system) -> str:
    if not system:
        return ""
    if isinstance(system, str):
        return system
    return "\n\n".join(b.get("text", "") for b in system if isinstance(b, dict))


def _to_tool(tool: dict) -> dict | None:
    if "input_schema" in tool:
        return {
            "type": "function",
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool["input_schema"],
            # Our schemas are not written for strict mode (optional fields,
            # no additionalProperties=false everywhere).
            "strict": False,
        }
    server_type = str(tool.get("type") or "")
    if server_type.startswith("web_search"):
        # Anthropic's max_uses has no per-tool OpenAI counterpart.
        return {"type": "web_search"}
    if server_type.startswith("web_fetch"):
        # No OpenAI equivalent; web_search alone covers "look it up online".
        return None
    log.warning("openai adapter: tool %r has no OpenAI equivalent, skipped", server_type or tool.get("name"))
    return None


def _to_mcp_tool(server: dict) -> dict:
    """Anthropic beta mcp_servers entry -> OpenAI remote MCP tool. Keeps the
    read-only allowlist, and OpenAI calls the server itself (no approval hop),
    same as Anthropic's connector."""
    tool: dict = {
        "type": "mcp",
        "server_label": server["name"],
        "server_url": server["url"],
        "require_approval": "never",
    }
    if server.get("authorization_token"):
        tool["authorization"] = server["authorization_token"]
    allowed = (server.get("tool_configuration") or {}).get("allowed_tools")
    if allowed:
        tool["allowed_tools"] = list(allowed)
    return tool


def _to_tool_choice(choice: dict):
    kind = choice.get("type")
    if kind == "tool":
        return {"type": "function", "name": choice["name"]}
    if kind == "any":
        return "required"
    if kind == "none":
        return "none"
    return "auto"


def _to_input(messages: list[dict]) -> list[dict]:
    items: list[dict] = []
    for message in messages:
        role = message["role"]
        content = message["content"]
        if isinstance(content, str):
            items.append(_message_item(role, [{"type": "text", "text": content}]))
            continue
        # Anthropic interleaves tool calls/results with text inside one message;
        # Responses wants them as separate items, in the same order.
        pending: list[dict] = []
        for block in content:
            btype = block.get("type") if isinstance(block, dict) else None
            if btype == "tool_use":
                if pending:
                    items.append(_message_item(role, pending))
                    pending = []
                items.append({
                    "type": "function_call",
                    "call_id": block["id"],
                    "name": block["name"],
                    "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                })
            elif btype == "tool_result":
                if pending:
                    items.append(_message_item(role, pending))
                    pending = []
                items.append({
                    "type": "function_call_output",
                    "call_id": block["tool_use_id"],
                    "output": _tool_result_text(block),
                })
            elif btype in ("text", "image", "document"):
                pending.append(block)
            # Anything else (thinking, Anthropic provider-side tool blocks) has
            # no OpenAI input form; the visible text around it is kept.
        if pending:
            items.append(_message_item(role, pending))
    return items


def _tool_result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, list):
        text = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    elif content is None:
        text = ""
    else:
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
    if block.get("is_error"):
        text = "ERROR: " + text
    return text


def _message_item(role: str, blocks: list[dict]) -> dict:
    if role == "assistant":
        # Assistant turns replay as output_text; images/documents only ever
        # come from the user side.
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        return {"role": "assistant", "content": [{"type": "output_text", "text": text or " "}]}
    return {"role": "user", "content": [_user_part(b) for b in blocks]}


def _user_part(block: dict) -> dict:
    btype = block["type"]
    if btype == "text":
        return {"type": "input_text", "text": block.get("text", "")}
    source = block.get("source") or {}
    if btype == "image":
        if source.get("type") == "base64":
            return {"type": "input_image", "detail": "auto",
                    "image_url": f"data:{source['media_type']};base64,{source['data']}"}
        return {"type": "input_image", "detail": "auto", "image_url": source.get("url", "")}
    # document
    title = block.get("title") or "document"
    if source.get("type") == "base64":
        return {"type": "input_file", "filename": title if title.lower().endswith(".pdf") else f"{title}.pdf",
                "file_data": f"data:{source['media_type']};base64,{source['data']}"}
    # Plain-text document: inline it, same content the model would read.
    return {"type": "input_text", "text": f"Файл «{title}»:\n{source.get('data', '')}"}


def to_anthropic_message(response) -> Message:
    """OpenAI Response -> anthropic.types.Message, so callers read .content /
    .stop_reason / .usage exactly as with Claude."""
    content: list[dict] = []
    has_call = False
    for item in response.output or []:
        itype = getattr(item, "type", None)
        if itype == "message":
            text = "".join(
                getattr(part, "text", "") or getattr(part, "refusal", "") or ""
                for part in item.content or []
            )
            if text:
                content.append({"type": "text", "text": text})
        elif itype == "function_call":
            has_call = True
            content.append({
                "type": "tool_use",
                "id": item.call_id,
                "name": item.name,
                "input": _parse_arguments(item.arguments),
            })
        # reasoning / web_search_call / mcp_call / mcp_list_tools items are provider-side
        # bookkeeping: their effect is already in the text, and they cannot be
        # replayed with store=False.

    incomplete = getattr(response, "incomplete_details", None)
    if getattr(response, "status", None) == "incomplete" and getattr(incomplete, "reason", None) == "max_output_tokens":
        stop_reason = "max_tokens"
    elif has_call:
        stop_reason = "tool_use"
    else:
        stop_reason = "end_turn"

    usage = getattr(response, "usage", None)
    return Message.model_validate({
        "id": response.id,
        "type": "message",
        "role": "assistant",
        "model": response.model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": getattr(usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        },
    })


def _parse_arguments(raw: str | None) -> dict:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        log.warning("openai adapter: function call arguments are not JSON: %.200s", raw)
        return {}
    return parsed if isinstance(parsed, dict) else {}
