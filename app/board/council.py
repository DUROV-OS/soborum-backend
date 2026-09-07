"""The AI council that drives "внести изменения" in the "Совет директоров"
section, end to end:

1. propose_change() first runs the "дирижёр" (app.board.conductor): a
   production-system snapshot plus a knowledge-base/web research briefing,
   gathered before anyone has an opinion - then gathers one opinion per role
   (7 council members, run concurrently) and synthesizes them into one
   conclusion - stored as the first "round" on a BoardProposal.
2. The conclusion (and, on request, the full transcript) is handed back to
   the caller (app.board.router) to show the employee.
3. add_round() re-runs the council with the employee's rejection comment as
   extra context, appending another round to the same proposal.
4-6. apply_proposal(), once the employee accepts a round, edits the node the
   proposal was about, then cascades the change down through its descendants
   and back up through its ancestors - see cascade_down/cascade_up.

Most stages below make their own Claude call with a forced tool_choice, the
same way the rest of app.ai does: the model can't reply with free text, only
with the structured decision the stage needs. The one exception is stage 1's
per-role opinions (_collect_opinions/_call_subagent): each role is its own
subagent that can search the knowledge-base MCP connector and the web before
settling on its opinion, so tool_choice there is "auto", not forced - see
_call_subagent.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import anthropic
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.ai import mcp_auth
from app.board import conductor as board_conductor
from app.board import prompts
from app.board import service as board_service
from app.board.models import (
    BoardChangeSource,
    BoardChangeType,
    BoardNode,
    BoardNodeChange,
    BoardProposal,
    BoardProposalStatus,
)
from app.core.config import settings
from app.users.models import User

MAX_CASCADE_DEPTH = 4  # tree is only 3 levels deep; this is just a runaway-recursion guard


def _get_client() -> anthropic.Anthropic:
    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ИИ не настроен: не задан ANTHROPIC_API_KEY (см. backend/.env)",
        )
    from app.core.llm import anthropic_client

    return anthropic_client()


def _tool_use_input(response, tool_name: str) -> dict | None:
    block = next((b for b in response.content if b.type == "tool_use" and b.name == tool_name), None)
    return block.input if block else None


def _create_with_search(client: anthropic.Anthropic, mcp_token: str | None, kwargs: dict):
    """Like conductor._research_brief's call, but for a subagent that also
    has to end in a specific structured tool call: attaches the read-only
    knowledge-base MCP connector when configured, on top of whatever tools
    the caller already put in kwargs["tools"]. Anthropic (and, through it,
    the MCP provider) executes the actual search server-side and injects the
    results inline, so this is still a single request - no local loop."""
    if mcp_token is None:
        return client.messages.create(**kwargs)
    mcp_server = {
        "type": "url",
        "url": settings.mcp_server_url,
        "name": "knowledge-base",
        "authorization_token": mcp_token,
        "tool_configuration": {"allowed_tools": board_conductor.MCP_READ_ONLY_TOOLS},
    }
    return client.beta.messages.create(betas=[board_conductor.MCP_BETA], mcp_servers=[mcp_server], **kwargs)


def _call_subagent(
    client: anthropic.Anthropic, mcp_token: str | None, system: str, user_content: str,
    tool_schema: dict, tool_name: str, max_tokens: int,
) -> dict:
    """One subagent turn that can search the knowledge base and the web
    before settling on its answer. tool_choice is "auto" rather than forced
    so the model actually gets the chance to search first and still call
    `tool_name` last in the same response; if it doesn't (ignores the system
    prompt, or the turn got cut short), one clean fallback call forces the
    structured answer - see the comment below for why that fallback can't
    just replay the first response's content as history."""
    kwargs = {
        "model": settings.ai_model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user_content}],
        "tools": [tool_schema, board_conductor.WEB_SEARCH_TOOL],
        "tool_choice": {"type": "auto"},
    }
    response = _create_with_search(client, mcp_token, kwargs)
    result = _tool_use_input(response, tool_name)
    if result is not None:
        return result

    # The model didn't call `tool_name` on its own (truncated by max_tokens,
    # paused mid a long-running search turn, or it just answered in text).
    # response.content can't be safely replayed as history in any of these
    # cases - a server-executed mcp_tool_use/server_tool_use block may be
    # dangling with no paired result block yet, which the API rejects
    # outright if resent. So don't continue the turn: carry forward only
    # whatever plain text the model already wrote (if any) and force the
    # structured answer from a clean, single-turn request instead.
    already_written = "".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", None) == "text").strip()
    fallback_user_content = user_content
    if already_written:
        fallback_user_content += (
            "\n\nЧерновые заметки по итогам предыдущего исследования (могут быть неполными):\n" + already_written
        )
    fallback_response = client.messages.create(
        model=settings.ai_model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": fallback_user_content}],
        tools=[tool_schema],
        tool_choice={"type": "tool", "name": tool_name},
    )
    return _tool_use_input(fallback_response, tool_name) or {}


# ------------------------------------------------------------------ stage 1 --

def _collect_opinions(
    db: Session, client: anthropic.Anthropic, node_ctx: dict, user_message: str, history_note: str | None,
    context: dict,
) -> list[dict]:
    payload = {"node": node_ctx, "request": user_message, "production_snapshot": context["production"]}
    if context.get("research_brief"):
        payload["research_brief"] = context["research_brief"]
    if history_note:
        payload["previous_round"] = history_note
    content = json.dumps(payload, ensure_ascii=False, default=str)

    # Fetched once, up front - mcp_auth.get_access_token() touches the DB
    # session, which the thread pool below can't safely share.
    mcp_token = mcp_auth.get_access_token(db) if settings.mcp_configured else None

    def _one(role_key: str) -> dict:
        result = _call_subagent(
            client, mcp_token, prompts.ROLE_PROMPTS[role_key], content,
            prompts.opinion_tool_schema(), prompts.OPINION_TOOL_NAME, max_tokens=2048,
        )
        return {
            "role": role_key,
            "role_label": prompts.ROLE_LABELS[role_key],
            "opinion": result.get("opinion", ""),
            "stance": result.get("stance", "caution"),
        }

    with ThreadPoolExecutor(max_workers=len(prompts.ROLE_PROMPTS)) as pool:
        return list(pool.map(_one, prompts.ROLE_PROMPTS.keys()))


def gather_role_opinions(
    db: Session, client: anthropic.Anthropic, node_ctx: dict, question: str, history_note: str | None, context: dict
) -> list[dict]:
    """Public entry point for polling the 7 council roles over an arbitrary
    question about a node, without the proposal/synthesis machinery around
    it - used by app.board.discussion so a free-form thread can still ask
    "что думает совет?" and get the same per-role takes."""
    return _collect_opinions(db, client, node_ctx, question, history_note, context)


def _synthesize(
    client: anthropic.Anthropic, node_ctx: dict, user_message: str, history_note: str | None, opinions: list[dict],
    context: dict,
) -> dict:
    payload = {
        "node": node_ctx, "request": user_message, "council_opinions": opinions,
        "production_snapshot": context["production"],
    }
    if context.get("research_brief"):
        payload["research_brief"] = context["research_brief"]
    if history_note:
        payload["previous_round"] = history_note
    response = client.messages.create(
        model=settings.ai_model,
        max_tokens=1024,
        system=prompts.SYNTHESIS_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}],
        tools=[prompts.synthesis_tool_schema()],
        tool_choice={"type": "tool", "name": prompts.SYNTHESIS_TOOL_NAME},
    )
    result = _tool_use_input(response, prompts.SYNTHESIS_TOOL_NAME)
    if result is None:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Совет директоров не смог прийти к заключению")
    return result


def _run_council(db: Session, client: anthropic.Anthropic, node: BoardNode, user_message: str, history_note: str | None) -> dict:
    node_ctx = board_service.node_context(node)
    # The conductor gathers production data and a knowledge-base/web research
    # briefing before the council opinions and synthesis are drafted, so both
    # are grounded in current, outside information rather than just the
    # node's own text and the employee's request.
    context = board_conductor.gather_context(client, db, node_ctx, user_message)
    opinions = _collect_opinions(db, client, node_ctx, user_message, history_note, context)
    conclusion = _synthesize(client, node_ctx, user_message, history_note, opinions, context)
    recommendation = conclusion.get("recommendation")
    if recommendation not in ("change", "no_change"):
        recommendation = "no_change"
    proposed_color = board_service.safe_color(conclusion.get("proposed_color"))
    return {
        "user_message": user_message,
        "council": opinions,
        "summary": conclusion.get("summary", ""),
        "recommendation": recommendation,
        "proposed_title": conclusion.get("proposed_title") or None,
        "proposed_description": conclusion.get("proposed_description") or None,
        "proposed_color": proposed_color.value if proposed_color else None,
        "decision": "pending",
        "production_snapshot": context["production"],
        "research_brief": context.get("research_brief"),
    }


def propose_change(db: Session, node: BoardNode, user: User, message: str) -> BoardProposal:
    client = _get_client()
    round_data = _run_council(db, client, node, message, history_note=None)
    proposal = BoardProposal(node_id=node.id, requested_by_id=user.id, rounds=[round_data])
    db.add(proposal)
    db.commit()
    db.refresh(proposal)
    return proposal


# ------------------------------------------------------------------ stage 3 --

def add_round(db: Session, proposal: BoardProposal, comment: str) -> BoardProposal:
    if proposal.status != BoardProposalStatus.PENDING:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Это предложение уже обработано")
    if not comment or not comment.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Нужно указать комментарий, почему изменения не принимаются"
        )

    rounds = list(proposal.rounds)
    last_round = rounds[-1]
    rounds[-1] = {**last_round, "decision": "rejected"}

    history_note = (
        f"Это уже повторное обсуждение той же ноды. Предыдущий вывод совета: «{last_round.get('summary', '')}» "
        f"(рекомендация: {last_round.get('recommendation')}). Сотрудник не согласился и написал: «{comment.strip()}» "
        "- учти это при новом обсуждении."
    )

    client = _get_client()
    round_data = _run_council(db, client, proposal.node, comment.strip(), history_note)
    rounds.append(round_data)

    proposal.rounds = rounds
    db.add(proposal)
    db.commit()
    db.refresh(proposal)
    return proposal


def cancel_proposal(db: Session, proposal: BoardProposal) -> BoardProposal:
    if proposal.status != BoardProposalStatus.PENDING:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Это предложение уже обработано")
    proposal.status = BoardProposalStatus.CANCELLED
    db.add(proposal)
    db.commit()
    db.refresh(proposal)
    return proposal


# --------------------------------------------------------------- stage 4-6 --

def _decide_node_edit(client: anthropic.Anthropic, node: BoardNode, round_data: dict) -> dict:
    payload = {
        "node": board_service.node_context(node),
        "council_summary": round_data.get("summary"),
        "council_recommendation": round_data.get("recommendation"),
        "council_proposed_description": round_data.get("proposed_description"),
        "council_proposed_color": round_data.get("proposed_color"),
        "employee_request": round_data.get("user_message"),
    }
    response = client.messages.create(
        model=settings.ai_model,
        max_tokens=1024,
        system=prompts.EDITOR_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}],
        tools=[prompts.editor_tool_schema(node.level)],
        tool_choice={"type": "tool", "name": prompts.EDITOR_TOOL_NAME},
    )
    result = _tool_use_input(response, prompts.EDITOR_TOOL_NAME)
    if result is None:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="ИИ не применил решение совета")
    return result


def _apply_node_edit(
    db: Session, node: BoardNode, edit: dict, source: BoardChangeSource, proposal_id: int | None, actor: User,
    changes: list[BoardNodeChange],
) -> None:
    old_description, old_color = node.description, node.color.value
    new_description = edit.get("new_description")
    if new_description:
        node.description = new_description
        node.summary = edit.get("summary") or node.summary
    new_color = board_service.safe_color(edit.get("new_color"))
    if new_color:
        node.color = new_color
    new_title = edit.get("new_title")
    if new_title:
        node.title = str(new_title).strip()[:255]
    db.flush()
    changes.append(
        board_service.log_change(
            db, node, BoardChangeType.UPDATED, source, proposal_id, actor,
            old_description=old_description, new_description=node.description,
            old_color=old_color, new_color=node.color.value, note=edit.get("change_summary"),
        )
    )
    structural = edit.get("structural_changes")
    if structural:
        board_service.apply_structural_ops(db, node, structural, source, proposal_id, actor, changes)


def _review_children(client: anthropic.Anthropic, parent: BoardNode, change_note: str) -> dict | None:
    payload = {
        "parent_change": change_note,
        "parent_level": parent.level,
        "children": [
            {"id": c.id, "title": c.title, "description": c.description, "color": c.color.value}
            for c in parent.children
        ],
    }
    response = client.messages.create(
        model=settings.ai_model,
        max_tokens=2048,
        system=prompts.CASCADE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}],
        tools=[prompts.cascade_tool_schema()],
        tool_choice={"type": "tool", "name": prompts.CASCADE_TOOL_NAME},
    )
    return _tool_use_input(response, prompts.CASCADE_TOOL_NAME)


def cascade_down(
    db: Session, client: anthropic.Anthropic, parent: BoardNode, change_note: str, source: BoardChangeSource,
    proposal_id: int | None, actor: User, changes: list[BoardNodeChange], depth: int = 0,
) -> None:
    if not parent.children or depth > MAX_CASCADE_DEPTH:
        return

    data = _review_children(client, parent, change_note)
    if data is None:
        return

    by_id = {c.id: c for c in parent.children}
    changed_ids: list[int] = []
    for update in data.get("updates", []):
        child = by_id.get(update.get("child_id"))
        if child is None or not update.get("needs_change"):
            continue
        old_description, old_color = child.description, child.color.value
        new_description = update.get("new_description")
        if new_description:
            child.description = new_description
            child.summary = update.get("summary") or child.summary
        new_color = board_service.safe_color(update.get("new_color"))
        if new_color:
            child.color = new_color
        db.flush()
        changes.append(
            board_service.log_change(
                db, child, BoardChangeType.UPDATED, source, proposal_id, actor,
                old_description=old_description, new_description=child.description,
                old_color=old_color, new_color=child.color.value, note=update.get("change_summary"),
            )
        )
        changed_ids.append(child.id)

    structural = data.get("structural_changes")
    if structural:
        board_service.apply_structural_ops(db, parent, structural, source, proposal_id, actor, changes)

    for child_id in changed_ids:
        child = by_id.get(child_id)
        if child is not None:
            cascade_down(
                db, client, child, f"Изменение в «{child.title}»: {child.description}",
                source, proposal_id, actor, changes, depth + 1,
            )


def _review_ancestor(client: anthropic.Anthropic, ancestor: BoardNode, siblings: list[BoardNode], change_note: str) -> dict | None:
    payload = {
        "change_note": change_note,
        "self": {
            "title": ancestor.title, "level": ancestor.level,
            "description": ancestor.description, "color": ancestor.color.value,
        },
        "other_children": [{"id": c.id, "title": c.title, "color": c.color.value} for c in siblings],
    }
    response = client.messages.create(
        model=settings.ai_model,
        max_tokens=1024,
        system=prompts.ANCESTOR_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}],
        tools=[prompts.ancestor_tool_schema(ancestor.level)],
        tool_choice={"type": "tool", "name": prompts.ANCESTOR_TOOL_NAME},
    )
    return _tool_use_input(response, prompts.ANCESTOR_TOOL_NAME)


def cascade_up(
    db: Session, client: anthropic.Anthropic, changed_node: BoardNode, change_note: str, source: BoardChangeSource,
    proposal_id: int | None, actor: User, changes: list[BoardNodeChange],
) -> None:
    ancestor = changed_node.parent
    just_changed_id = changed_node.id

    while ancestor is not None:
        siblings = [c for c in ancestor.children if c.id != just_changed_id]
        data = _review_ancestor(client, ancestor, siblings, change_note)
        if data is None:
            break

        if data.get("needs_own_change"):
            old_description, old_color = ancestor.description, ancestor.color.value
            new_description = data.get("new_description")
            if new_description:
                ancestor.description = new_description
                ancestor.summary = data.get("summary") or ancestor.summary
            new_color = board_service.safe_color(data.get("new_color"))
            if new_color:
                ancestor.color = new_color
            db.flush()
            changes.append(
                board_service.log_change(
                    db, ancestor, BoardChangeType.UPDATED, source, proposal_id, actor,
                    old_description=old_description, new_description=ancestor.description,
                    old_color=old_color, new_color=ancestor.color.value, note=data.get("change_summary"),
                )
            )
            change_note = f"Изменение в «{ancestor.title}»: {ancestor.description}"

        structural = data.get("structural_changes")
        if structural:
            board_service.apply_structural_ops(db, ancestor, structural, source, proposal_id, actor, changes)

        siblings_by_id = {c.id: c for c in siblings}
        for child_id in data.get("delegate_child_ids") or []:
            sibling = siblings_by_id.get(child_id)
            if sibling is not None:
                # down-cascade only, per spec: delegating a sibling never re-triggers the upward pass.
                cascade_down(db, client, sibling, change_note, source, proposal_id, actor, changes)

        just_changed_id = ancestor.id
        ancestor = ancestor.parent


def apply_proposal(db: Session, proposal: BoardProposal, user: User) -> tuple[BoardProposal, list[BoardNodeChange]]:
    if proposal.status != BoardProposalStatus.PENDING:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Это предложение уже обработано")

    rounds = list(proposal.rounds)
    round_data = {**rounds[-1], "decision": "accepted"}
    rounds[-1] = round_data
    proposal.rounds = rounds

    node = proposal.node
    client = _get_client()
    changes: list[BoardNodeChange] = []

    # stage 4: edit the node the proposal was originally about
    edit = _decide_node_edit(client, node, round_data)
    _apply_node_edit(db, node, edit, BoardChangeSource.COUNCIL, proposal.id, user, changes)
    change_note = f"Изменение в «{node.title}»: {node.description}"

    # stage 5: cascade down through node's own descendants
    cascade_down(db, client, node, change_note, BoardChangeSource.COUNCIL, proposal.id, user, changes)

    # stage 6: cascade up through node's ancestors
    cascade_up(db, client, node, change_note, BoardChangeSource.COUNCIL, proposal.id, user, changes)

    proposal.status = BoardProposalStatus.APPLIED
    proposal.applied_at = datetime.now(timezone.utc)
    db.add(proposal)
    db.commit()
    db.refresh(proposal)
    return proposal, changes
