"""Восстановление старой структуры «Совета директоров» (задача 0028):
6 направлений / 27 поднаправлений вместо временных 4/12, плюс аудит-запись
о самом факте замены дерева."""

from app.board.models import BoardChangeSource, BoardChangeType, BoardNode, BoardNodeChange, BoardNodeColor
from app.board.seed import DIRECTIONS, restore_previous_structure
from app.board.service import get_root

EXPECTED_DIRECTIONS = ["Маркетинг", "Производство", "Персонал", "Рынки", "Экономика", "Soborbum"]
EXPECTED_SUBCOUNTS = {"Маркетинг": 5, "Производство": 5, "Персонал": 4, "Рынки": 4, "Экономика": 5, "Soborbum": 4}


def test_directions_constant_matches_spec():
    """Sanity check on the seed data itself, independent of any DB call."""
    assert [d[0] for d in DIRECTIONS] == EXPECTED_DIRECTIONS
    for title, _description, _color, subdirections in DIRECTIONS:
        assert len(subdirections) == EXPECTED_SUBCOUNTS[title]
    assert sum(len(subs) for _, _, _, subs in DIRECTIONS) == 27


def test_restore_on_empty_board_creates_root_and_full_tree(db, make_user):
    admin = make_user(admin=True)

    assert get_root(db) is None

    root = restore_previous_structure(db, admin)

    assert root.title == "durov.house"
    assert root.parent_id is None

    directions = list(root.children)
    assert [d.title for d in directions] == EXPECTED_DIRECTIONS
    total_subs = 0
    for direction in directions:
        subs = list(direction.children)
        total_subs += len(subs)
        assert len(subs) == EXPECTED_SUBCOUNTS[direction.title]
        for sub in subs:
            assert sub.description.strip() != ""
    assert total_subs == 27

    # audit entry for the replacement itself, even though the tree was empty
    changes = db.query(BoardNodeChange).filter(BoardNodeChange.node_id == root.id).all()
    assert len(changes) == 1
    assert changes[0].source == BoardChangeSource.MANUAL
    assert changes[0].change_type == BoardChangeType.UPDATED
    assert "Арсения" in changes[0].note


def test_restore_replaces_existing_populated_tree_and_logs_change(db, make_user):
    """Simulates the real scenario from the task: a DB already has the "new"
    4-direction tree, plus a manual edit on top of one of its sub-nodes
    (like board_node_changes/board_proposals would leave behind) - restore
    must wholesale-replace it with the old 6-direction/27-sub structure and
    still record a single new audit entry for the replacement, without
    needing to preserve the old layered edits."""
    admin = make_user(admin=True)

    root = BoardNode(
        parent_id=None, level=0, sort_order=0, title="durov.house", description="старое описание корня",
        color=BoardNodeColor.GREEN,
    )
    db.add(root)
    db.flush()
    old_direction = BoardNode(
        parent_id=root.id, level=1, sort_order=0, title="Новые рынки", description="временное", color=BoardNodeColor.YELLOW
    )
    db.add(old_direction)
    db.flush()
    old_sub = BoardNode(
        parent_id=old_direction.id, level=2, sort_order=0, title="Регионы РФ", description="временное",
        color=BoardNodeColor.YELLOW,
    )
    db.add(old_sub)
    db.commit()

    # a prior manual edit layered on top of the old tree, before restore runs
    db.add(
        BoardNodeChange(
            node_id=old_sub.id, source=BoardChangeSource.MANUAL, change_type=BoardChangeType.UPDATED,
            title=old_sub.title, note="какая-то более ранняя правка",
        )
    )
    db.commit()
    changes_before = db.query(BoardNodeChange).count()

    restored_root = restore_previous_structure(db, admin)

    assert restored_root.id == root.id  # root itself is preserved, not recreated
    assert restored_root.description == "старое описание корня"  # restore doesn't touch the root's own fields

    directions = list(restored_root.children)
    assert [d.title for d in directions] == EXPECTED_DIRECTIONS
    assert sum(len(d.children) for d in directions) == 27

    # the old nodes are gone from the tree entirely (not just detached from the
    # root) - total node count is exactly root + 6 directions + 27 sub-directions,
    # and none of the old titles survive anywhere in the tree. (Not asserting on
    # old_direction.id/old_sub.id directly: sqlite recycles rowids for freshly
    # inserted rows once the old ones are deleted, so an id-based lookup could
    # coincidentally hit one of the *new* nodes instead of proving absence.)
    all_titles = {n.title for n in db.query(BoardNode).all()}
    assert "Новые рынки" not in all_titles
    assert "Регионы РФ" not in all_titles
    assert db.query(BoardNode).count() == 1 + 6 + 27

    # ...but exactly one new audit entry was added on top of what was already there
    changes_after = db.query(BoardNodeChange).order_by(BoardNodeChange.id).all()
    assert len(changes_after) == changes_before + 1
    latest = changes_after[-1]
    assert latest.node_id == restored_root.id
    assert latest.source == BoardChangeSource.MANUAL
    assert latest.change_type == BoardChangeType.UPDATED
    assert "Новые рынки" in latest.note


def test_restore_colors_match_spec_for_a_sample_of_nodes(db, make_user):
    """Spot-checks a few colors called out explicitly in the task spec, so a
    typo in the DIRECTIONS table would fail loudly."""
    admin = make_user(admin=True)
    root = restore_previous_structure(db, admin)

    by_title = {}
    for direction in root.children:
        by_title[direction.title] = direction.color
        for sub in direction.children:
            by_title[sub.title] = sub.color

    assert by_title["Маркетинг"] == BoardNodeColor.YELLOW
    assert by_title["Производство"] == BoardNodeColor.GREEN
    assert by_title["Персонал"] == BoardNodeColor.RED
    assert by_title["поисковый трафик"] == BoardNodeColor.RED
    assert by_title["контроль качества"] == BoardNodeColor.GREEN
    assert by_title["система найма"] == BoardNodeColor.RED
    assert by_title["маржа"] == BoardNodeColor.RED
    assert by_title["амортизация"] == BoardNodeColor.RED
    assert by_title["использумаемость"] == BoardNodeColor.GREEN
