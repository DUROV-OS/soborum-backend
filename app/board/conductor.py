"""The "агент-дирижёр" step: before the council convenes over a node, gather
outside context for it to work with —

- a production-system snapshot (first-party DB aggregate, no AI call: reuses
  the exact same section builder app.board.actualize already draws on), and
- a short research briefing from one Claude call that can search the
  company's knowledge base (the same remote MCP connector app.ai.engine uses)
  and the public internet (Anthropic's server-side web_search tool) for
  anything relevant to the node and the employee's request.

Both connectors are server-side: Anthropic (and, through it, the MCP
provider) executes the actual search and injects the results inline within
one messages.create call - there is no local tool-execution loop here, same
as app.ai.engine's use of the MCP connector.

Best-effort by design: if the research call fails (MCP server down, network
error, ...), the council still convenes on the node/production data alone
rather than blocking the whole flow over an enrichment step.
"""

import json

import anthropic
from sqlalchemy.orm import Session

from app.ai import mcp_auth
from app.ai import model_profiles
from app.ai.model_profiles import ModelProfile
from app.board import prompts
from app.core.config import settings
from app.dashboard.service import SECTION_BUILDERS

MCP_BETA = "mcp-client-2025-04-04"
MCP_READ_ONLY_TOOLS = ["read_index", "list_notes", "search_notes", "read_note", "get_unread_files"]
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}


def _production_snapshot(db: Session) -> dict:
    builder = SECTION_BUILDERS["production"][1]
    return builder(db)


def _research_brief(client: anthropic.Anthropic, db: Session, node_ctx: dict, user_message: str) -> str | None:
    payload = {"node": node_ctx, "employee_request": user_message}
    kwargs = {
        **model_profiles.profile_params(ModelProfile.BOARD_AGENT),
        "max_tokens": 1024,
        "system": prompts.CONDUCTOR_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}],
        "tools": [WEB_SEARCH_TOOL],
    }
    try:
        if settings.mcp_configured:
            mcp_server = {
                "type": "url",
                "url": settings.mcp_server_url,
                "name": "knowledge-base",
                "authorization_token": mcp_auth.get_access_token(db),
                "tool_configuration": {"allowed_tools": MCP_READ_ONLY_TOOLS},
            }
            response = client.beta.messages.create(betas=[MCP_BETA], mcp_servers=[mcp_server], **kwargs)
        else:
            response = client.messages.create(**kwargs)
    except Exception:
        # Research is an enrichment step, not a hard dependency - a flaky KB
        # server or search outage shouldn't stop the council from convening.
        return None

    text = "".join(block.text for block in response.content if block.type == "text").strip()
    return text or None


def gather_context(client: anthropic.Anthropic, db: Session, node_ctx: dict, user_message: str) -> dict:
    return {
        "production": _production_snapshot(db),
        "research_brief": _research_brief(client, db, node_ctx, user_message),
    }
