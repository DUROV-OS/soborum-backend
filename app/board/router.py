from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, status
from sqlalchemy.orm import Session

from app.board import actualize as board_actualize
from app.board import council as board_council
from app.board import discussion as board_discussion
from app.board import service as board_service
from app.board.models import BoardDiscussion, BoardNodeChange, BoardProposalStatus
from app.board.schemas import (
    ActualizeResultOut,
    ApplyResultOut,
    BoardDiscussionDetailOut,
    BoardDiscussionMessageOut,
    BoardDiscussionOut,
    BoardNodeChangeOut,
    BoardNodeDetailOut,
    BoardNodeOut,
    BoardNodeUpdate,
    BoardProposalOut,
    CreateDiscussionRequest,
    PostDiscussionMessageRequest,
    ProposalDecisionRequest,
    ProposeChangeRequest,
    RenameDiscussionRequest,
)
from app.common.module_access import Module
from app.core.deps import require_edit, require_full, require_view
from app.db.session import get_db
from app.users.models import User, UserRole

app = FastAPI(
    title="Soborbum — Совет директоров",
    description="Дерево стратегических направлений компании: описание, статус критичности по каждой "
    "ветке, ИИ-совет из нескольких ролей, который обсуждает и проводит стратегические изменения по "
    "дереву, и свободные обсуждения с советом (чат без правок дерева).",
    version="0.3",
)

require_board_view = require_view(Module.BOARD)
require_board_edit = require_edit(Module.BOARD)
require_board_full = require_full(Module.BOARD)


@app.get("/tree", response_model=BoardNodeOut)
def get_tree(db: Session = Depends(get_db), _: User = Depends(require_board_view)):
    root = board_service.get_root(db)
    if root is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Дерево ещё не создано")
    return BoardNodeOut.from_model(root)


@app.get("/nodes/{node_id}", response_model=BoardNodeDetailOut)
def get_node(node_id: int, db: Session = Depends(get_db), _: User = Depends(require_board_view)):
    node = board_service.get_node_or_404(db, node_id)
    return BoardNodeDetailOut.from_model_with_path(node, board_service.node_path(node))


@app.patch("/nodes/{node_id}", response_model=BoardNodeDetailOut)
def update_node(
    node_id: int, payload: BoardNodeUpdate, db: Session = Depends(get_db), user: User = Depends(require_board_edit)
):
    node = board_service.get_node_or_404(db, node_id)
    node = board_service.update_node_manual(db, node, payload.title, payload.description, payload.color, user)
    return BoardNodeDetailOut.from_model_with_path(node, board_service.node_path(node))


@app.get("/nodes/{node_id}/changes", response_model=list[BoardNodeChangeOut])
def node_change_history(node_id: int, db: Session = Depends(get_db), _: User = Depends(require_board_view)):
    board_service.get_node_or_404(db, node_id)  # 404 if the node never existed
    return (
        db.query(BoardNodeChange)
        .filter(BoardNodeChange.node_id == node_id)
        .order_by(BoardNodeChange.id.desc())
        .all()
    )


@app.post("/nodes/{node_id}/propose", response_model=BoardProposalOut)
def propose_change(
    node_id: int,
    payload: ProposeChangeRequest,
    include_transcript: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(require_board_edit),
):
    """Stage 1: convenes the council (7 roles + synthesis) over one node and
    stores its conclusion as a pending proposal, for the employee to accept
    or reject via POST /proposals/{id}/respond."""
    node = board_service.get_node_or_404(db, node_id)
    proposal = board_council.propose_change(db, node, user, payload.message)
    return BoardProposalOut.from_model(proposal, include_transcript)


@app.get("/proposals", response_model=list[BoardProposalOut])
def list_proposals(
    node_id: int | None = None,
    proposal_status: BoardProposalStatus | None = None,
    include_transcript: bool = False,
    db: Session = Depends(get_db),
    _: User = Depends(require_board_view),
):
    proposals = board_service.list_proposals(db, node_id, proposal_status)
    return [BoardProposalOut.from_model(p, include_transcript) for p in proposals]


@app.get("/proposals/{proposal_id}", response_model=BoardProposalOut)
def get_proposal(
    proposal_id: int, include_transcript: bool = False, db: Session = Depends(get_db), _: User = Depends(require_board_view)
):
    proposal = board_service.get_proposal_or_404(db, proposal_id)
    return BoardProposalOut.from_model(proposal, include_transcript)


@app.post("/proposals/{proposal_id}/respond", response_model=ApplyResultOut)
def respond_to_proposal(
    proposal_id: int,
    payload: ProposalDecisionRequest,
    include_transcript: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(require_board_edit),
):
    """Stage 2/3/4-6: the employee's decision on the council's latest
    conclusion. "reject" needs a comment and loops back to stage 1 with it as
    context (a new round on the same proposal). "accept" edits the node and
    cascades the change down through its descendants and up through its
    ancestors."""
    proposal = board_service.get_proposal_or_404(db, proposal_id)
    if payload.decision == "reject":
        proposal = board_council.add_round(db, proposal, payload.comment or "")
        return ApplyResultOut(proposal=BoardProposalOut.from_model(proposal, include_transcript), changes=[])

    proposal, changes = board_council.apply_proposal(db, proposal, user)
    return ApplyResultOut(
        proposal=BoardProposalOut.from_model(proposal, include_transcript),
        changes=[BoardNodeChangeOut.model_validate(c) for c in changes],
    )


@app.delete("/proposals/{proposal_id}", response_model=BoardProposalOut)
def cancel_proposal(proposal_id: int, db: Session = Depends(get_db), _: User = Depends(require_board_edit)):
    """Discards a pending proposal without looping back - for when the
    employee just wants to drop it instead of rejecting-with-a-comment."""
    proposal = board_service.get_proposal_or_404(db, proposal_id)
    proposal = board_council.cancel_proposal(db, proposal)
    return BoardProposalOut.from_model(proposal, include_transcript=False)


# ------------------------------------------------------------- discussions --
# Free-form chat with the board. Unlike /propose + /proposals, nothing here
# ever edits the tree - it's just talking. `consult_council=True` on a
# message additionally polls the same 7 roles for their individual takes.


def _discussion_owned_or_admin(discussion: BoardDiscussion, user: User) -> None:
    if discussion.created_by_id != user.id and user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Изменять обсуждение может только его автор или администратор"
        )


def _default_discussion_title(payload: CreateDiscussionRequest, node) -> str:
    if payload.title and payload.title.strip():
        return payload.title.strip()
    if payload.message and payload.message.strip():
        snippet = " ".join(payload.message.split())
        return snippet[:60] + ("…" if len(snippet) > 60 else "")
    return f"Обсуждение: {node.title}" if node is not None else "Обсуждение с советом директоров"


@app.post("/discussions", response_model=BoardDiscussionDetailOut, status_code=status.HTTP_201_CREATED)
def create_discussion(
    payload: CreateDiscussionRequest, db: Session = Depends(get_db), user: User = Depends(require_board_edit)
):
    node = board_service.get_node_or_404(db, payload.node_id) if payload.node_id is not None else None
    discussion = board_discussion.create_discussion(db, node, user, _default_discussion_title(payload, node))
    if payload.message and payload.message.strip():
        board_discussion.post_message(db, discussion, user, payload.message, consult_council=False)
        db.refresh(discussion)
    return BoardDiscussionDetailOut.from_model(discussion)


@app.get("/discussions", response_model=list[BoardDiscussionOut])
def list_discussions(
    node_id: int | None = None,
    mine: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(require_board_view),
):
    discussions = board_discussion.list_discussions(db, node_id, user if mine else None)
    return [BoardDiscussionOut.from_model(d) for d in discussions]


@app.get("/discussions/{discussion_id}", response_model=BoardDiscussionDetailOut)
def get_discussion(discussion_id: int, db: Session = Depends(get_db), _: User = Depends(require_board_view)):
    return BoardDiscussionDetailOut.from_model(board_discussion.get_discussion_or_404(db, discussion_id))


@app.post("/discussions/{discussion_id}/messages", response_model=BoardDiscussionMessageOut)
def post_discussion_message(
    discussion_id: int,
    payload: PostDiscussionMessageRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_board_edit),
):
    """Adds the employee's message to the thread and returns the board's
    reply. With `consult_council=true` the 7 council roles are polled first
    and their opinions are attached to the reply (only for a node-pinned
    discussion)."""
    discussion = board_discussion.get_discussion_or_404(db, discussion_id)
    message = board_discussion.post_message(db, discussion, user, payload.message, payload.consult_council)
    return BoardDiscussionMessageOut.model_validate(message)


@app.patch("/discussions/{discussion_id}", response_model=BoardDiscussionOut)
def rename_discussion(
    discussion_id: int,
    payload: RenameDiscussionRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_board_edit),
):
    discussion = board_discussion.get_discussion_or_404(db, discussion_id)
    _discussion_owned_or_admin(discussion, user)
    return BoardDiscussionOut.from_model(board_discussion.rename_discussion(db, discussion, payload.title))


@app.delete("/discussions/{discussion_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_discussion(discussion_id: int, db: Session = Depends(get_db), user: User = Depends(require_board_edit)):
    discussion = board_discussion.get_discussion_or_404(db, discussion_id)
    _discussion_owned_or_admin(discussion, user)
    board_discussion.delete_discussion(db, discussion)


@app.post("/actualize", response_model=ActualizeResultOut)
def actualize(db: Session = Depends(get_db), user: User = Depends(require_board_full)):
    """Refreshes the whole tree's descriptions/statuses from real, current
    operational data (tasks, clients, production, warehouse, ...) - no
    council, no proposal, applied directly. Admin-only: this is a system-wide
    pass over the whole company tree, not a per-node discussion."""
    root = board_service.get_root(db)
    if root is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Дерево ещё не создано")
    changes = board_actualize.actualize(db, root, user)
    return ActualizeResultOut(generated_at=datetime.now(timezone.utc), changes=[BoardNodeChangeOut.model_validate(c) for c in changes])
