import json

from sqlalchemy import text


def apply_actor(connection, actor):
    if connection.dialect.name == "postgresql":
        connection.execute(text("SELECT set_config('durov.actor', :actor, true)"), {
            "actor": json.dumps(actor, ensure_ascii=False, separators=(",", ":")),
        })


def bind_actor(db, user):
    # Never accept actor IDs from a request body. get_current_user resolved this
    # active account from the signed token and the current database row.
    actor = {"kind": "authenticated_user", "id": user.id, "name": user.full_name, "role": user.role.value}
    db.info["integrity_actor"] = actor
    apply_actor(db.connection(), actor)
