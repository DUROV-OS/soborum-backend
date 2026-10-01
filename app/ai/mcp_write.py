"""Narrow write channel to the knowledge-base MCP server (task 0010).

Unlike the read-only connector in app/ai/engine.py (MCP_READ_ONLY_TOOLS, run
by Anthropic's *hosted* MCP connector during a chat turn - the call happens
on Anthropic's side, our code never sees a JSON-RPC request for it), this
module talks to the same MCP server directly: JSON-RPC 2.0 `tools/call` over
app.core.mcp_remote.RemoteMcp, the transport already used for the
МойСклад/dashboard connectors (see app/agents/connectors.py). The bearer
token comes from app.ai.mcp_auth.get_access_token(db) - the same DB-persisted
credential the read connector uses - instead of RemoteMcp running its own
independent OAuth grant (see the "Разведка" section of
backlog/PROCESS/0010-knowledge-base-write-connector.md for why a second grant
isn't needed).

Reachable ONLY from trusted server code (meeting finish - see
meeting_notes.send_to_knowledge_base_quietly) or an admin manually testing it
via POST /api/ai/mcp/notes. Nothing here is ever added to
engine.MCP_READ_ONLY_TOOLS or a chat's allowed_tools - a regular employee
cannot make Marina call create_note/append_note from free-form tool-use.

IMPORTANT - unverified against a live server: the exact argument shapes below
(`path`+`content` for create_note/edit_note, `path`+`text` for append_note,
create_note failing when the path already exists) were NOT confirmed by a
live call to this project's configured MCP_SERVER_URL - no such server was
reachable from the sandbox this was written in. They come from inspecting
the tool schemas of an identically-shaped MCP server (same 5 read tools as
MCP_READ_ONLY_TOOLS, plus create_note/append_note/edit_note/insert_in_note/
str_replace_note/archive_note) that happened to be available to the coding
agent itself - a schema-only lookup, no live call, no side effect, no data
written anywhere. See the backlog file for the full reasoning and its
limits; this should be exercised against the real server before being
trusted with anything but the manual admin endpoint.
"""

from __future__ import annotations

import logging
import re

import httpx
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.ai import mcp_auth
from app.ai.engine import MCP_WRITE_TOOLS
from app.core.config import settings
from app.core.mcp_remote import McpError, McpTarget, RemoteMcp

logger = logging.getLogger(__name__)


def _target() -> McpTarget:
    return McpTarget(
        name="knowledge-base-write",
        url=settings.mcp_server_url,
        client_id=settings.mcp_oauth_client_id,
        client_secret=settings.mcp_oauth_client_secret,
        scope=settings.mcp_write_scope,
    )


def _require_enabled() -> None:
    if not settings.mcp_configured:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="База знаний не подключена: заполните MCP_SERVER_URL/MCP_OAUTH_CLIENT_ID/MCP_OAUTH_CLIENT_SECRET",
        )
    if not settings.mcp_write_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Запись в базу знаний отключена (MCP_WRITE_ENABLED=false)",
        )


def _client(db: Session) -> RemoteMcp:
    # A fresh RemoteMcp per call (cheap: one extra `initialize` handshake) -
    # simpler than caching one across requests while its token_provider needs
    # a live `db` closed over, and write calls are infrequent (meeting finish,
    # admin testing), so the extra round trip doesn't matter.
    return RemoteMcp(_target(), token_provider=lambda force: mcp_auth.get_access_token(db, force=force))


def _call_tool(db: Session, tool: str, arguments: dict):
    """Raises McpError - callers decide when a failure is fatal (create_note
    falls back to edit_note before giving up)."""
    if tool not in MCP_WRITE_TOOLS:
        raise ValueError(f"{tool} не входит в MCP_WRITE_TOOLS - см. app/ai/engine.py")
    try:
        return _client(db).call_tool(tool, arguments)
    except httpx.HTTPError as error:
        # Server unreachable / DNS / timeout - not something RemoteMcp itself
        # turns into McpError (that only happens for HTTP-level tool errors).
        raise McpError(f"MCP-сервер базы знаний недоступен: {error}") from error


def _slug(source: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", source.strip().lower()).strip("-")
    return slug or "note"


def _frontmatter(*, title: str, kind: str, note_status: str, source: str) -> str:
    # Same keys LocalVaultAdapter._parse reads (app/agents/vault.py):
    # title, kind, status - plus source, used here for idempotency/provenance.
    return "---\n" f"title: {title}\n" f"kind: {kind}\n" f"status: {note_status}\n" f"source: {source}\n" "---\n\n"


def note_path_for_source(source: str, folder: str | None = None) -> str:
    """Deterministic vault-relative path for a given source tag - a resend of
    the same source always targets the same file, which is what makes
    create_note's create-then-fallback-to-edit dance below idempotent."""
    base = (folder or settings.mcp_notes_folder).strip("/")
    return f"{base}/{_slug(source)}.md"


def _write_with_fallback(db: Session, path: str, content: str) -> None:
    try:
        _call_tool(db, "create_note", {"path": path, "content": content})
    except McpError as create_error:
        # create_note's own contract (per its schema - see module docstring)
        # is to fail when the path already exists, exactly the case on a
        # resend of the same source (the path is derived from it). There is
        # no verified way to tell that failure apart from any other McpError
        # without a live probe of the real error shape, so edit_note (full
        # replace) is tried as a fallback for ANY create_note failure; if
        # that also fails, the original create_note error is what surfaces -
        # a network/auth failure fails the same way either path is taken.
        try:
            _call_tool(db, "edit_note", {"path": path, "content": content})
        except McpError:
            logger.warning(
                "mcp_write: create_note (%s) и последующий edit_note оба не удались для %s", create_error, path
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"База знаний не приняла запись: {create_error}",
            ) from create_error


def create_note(
    db: Session,
    *,
    source: str,
    body: str,
    title: str | None = None,
    folder: str | None = None,
    kind: str = "record",
    note_status: str = "active",
    raw: bool = False,
) -> str:
    """Create a note in the knowledge base, or update it in place if a note
    for this exact `source` was already sent before (idempotent resend - see
    task 0010's spec, section 3). Returns the vault-relative path written.

    `raw=False` (default, e.g. the admin test endpoint): `body` is plain
    markdown, a minimal frontmatter block is generated from `title`/`kind`/
    `note_status`/`source`.

    `raw=True` (e.g. meeting_notes.send_to_knowledge_base_quietly): `body` is
    a complete document that already carries its own frontmatter (built by
    meeting_notes.build_document, which independently needs full control over
    that document's shape for its /document download endpoint) - it is
    written as-is, `title`/`kind`/`note_status` are ignored.
    """
    _require_enabled()
    path = note_path_for_source(source, folder)
    if raw:
        content = body
    else:
        if not title:
            raise ValueError("title обязателен, когда raw=False")
        content = _frontmatter(title=title, kind=kind, note_status=note_status, source=source) + body.strip() + "\n"
    _write_with_fallback(db, path, content)
    return path


def append_note(db: Session, *, path: str, text: str) -> None:
    """Append to an existing note without resending its content - for admin/
    manual use. Unlike create_note, not idempotent on its own: calling it
    twice appends twice (append_note has no "already applied" signal to check
    against, per its schema in the module docstring)."""
    _require_enabled()
    try:
        _call_tool(db, "append_note", {"path": path, "text": text})
    except McpError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"База знаний не приняла запись: {error}",
        ) from error
