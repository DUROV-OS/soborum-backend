"""Server-owned pilot policy. Model output is untrusted, including tool names and arguments."""
import math
from datetime import datetime, timezone

from fastapi import HTTPException

from app.ai.models import Chat, ChatMode
from app.ai.tools import DOMAIN_TOOLS, TOOLS, ToolDef
from app.common.module_access import Module
from app.users.models import User

POLICY_VERSION = "pilot-guardian-v1"


def _validate(value, schema: dict, path: str = "arguments") -> None:
    kind = schema.get("type")
    valid = {
        "object": lambda: isinstance(value, dict),
        "array": lambda: isinstance(value, list),
        "string": lambda: isinstance(value, str),
        "boolean": lambda: isinstance(value, bool),
        "integer": lambda: type(value) is int,
        "number": lambda: type(value) in (int, float) and math.isfinite(value),
    }
    if kind in valid and not valid[kind]():
        raise HTTPException(422, f"Некорректный тип аргумента: {path}")
    if "enum" in schema and value not in schema["enum"]:
        raise HTTPException(422, f"Недопустимое значение: {path}")
    if kind == "object":
        properties = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", [])):
            raise HTTPException(422, f"Не заполнены обязательные аргументы: {path}")
        if schema.get("additionalProperties") is False and value.keys() - properties.keys():
            raise HTTPException(422, f"Неизвестные аргументы: {path}")
        for key, child in value.items():
            if key in properties:
                _validate(child, properties[key], f"{path}.{key}")
    if kind == "array":
        for child in value:
            _validate(child, schema.get("items", {}), path)


def authorize_tool(chat: Chat, user: User, name: str, tool_input: dict, *, approved: bool = False,
                   proposal: bool = False) -> ToolDef:
    if chat.owner_id != user.id or not user.is_active or not user.has_access(Module.AI):
        raise HTTPException(403, "Нет полномочий на действие в этом чате")
    tool = TOOLS.get(name)
    if tool is None or name not in DOMAIN_TOOLS[chat.domain]:
        raise HTTPException(403, "Инструмент недоступен в этом разделе")
    if not user.has_access(tool.required_module):
        raise HTTPException(403, "Доступ к разделу действия отсутствует или был отозван")
    if not tool.read_only:
        if chat.mode == ChatMode.NO_ACTIONS:
            raise HTTPException(403, "В режиме анализа изменение данных запрещено")
        # A3/A4 require a separate approved rule and limits. A chat switch is not that rule.
        if not (approved or proposal):
            raise HTTPException(403, "Для изменения данных необходимо подтверждение человека")
    schema = {**tool.schema["input_schema"], "additionalProperties": False}
    _validate(tool_input, schema)
    return tool


def decision_record(user: User, *, approved: bool = False) -> dict:
    return {"policy_version": POLICY_VERSION, "actor_id": user.id,
            "checked_at": datetime.now(timezone.utc).isoformat(), "autonomy": "A2" if approved else "A0"}
