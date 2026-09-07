"""Knowledge base adapter.

Reads a local checkout of DUROV-OS/vault_backups (VAULT_ROOT).
Claude Team uses the same files via CLAUDE.md; this is the programmatic twin.

vault-server-MCP stays for chat. Soborbum gather() does not call MCP yet.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from app.agents.types import ContextHit

ALWAYS_PATHS: tuple[str, ...] = (
    "00_Agent/Constitution.md",
    "00_Agent/Operating_Principles.md",
    "00_Agent/Legal_Risk_Filter.md",
    "00_Agent/Shared_Company_Context.md",
    "00_Agent/Agent_Roster_MVP.md",
    "02_Business/00_Decision_Log/MOC_Decision_Log.md",
)

SKIP_DIR_NAMES = {".git", "_trash", "04_Archive", "01_Inbox", "raw", ".obsidian", "_scripts"}
# Process notes for people, not citations for a daily question.
SKIP_REL_PATHS = {
    "00_Agent/Company_Shift.md",
    "00_Agent/Changelog.md",
    "00_Agent/MOC_Agent.md",
}
STOPWORDS = {
    "какой",
    "какая",
    "какие",
    "каких",
    "нужна",
    "нужно",
    "сегодня",
    "можно",
    "для",
    "этот",
    "этого",
    "или",
    "как",
    "что",
    "чтобы",
    "есть",
    "будет",
    "ближайшего",
    "ближайший",
    "ближайшем",
    "просто",
    "ли",
}


@dataclass(frozen=True)
class _Note:
    path: str
    title: str
    kind: str
    status: str
    body: str


class LocalVaultAdapter:
    name = "vault"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def gather(self, query: str, prefixes: list[str], limit: int = 8) -> list[ContextHit]:
        if not self.root.exists():
            return []

        hits = [hit for path in ALWAYS_PATHS if (hit := self._hit(path))]
        tokens = tokenize(query)
        ranked: list[tuple[int, ContextHit]] = []
        seen = {hit.path for hit in hits}
        for note in self._iter(prefixes):
            if note.path in seen or note.status == "deprecated":
                continue
            score = _score(note, tokens)
            if score <= 0:
                continue
            ranked.append((score, _to_hit(note)))
        ranked.sort(key=lambda item: (-item[0], item[1].path or ""))
        for _, hit in ranked[:limit]:
            hits.append(hit)
        return hits

    def search(self, query: str, limit: int = 8) -> list[ContextHit]:
        return self.gather(query, prefixes=[""], limit=limit)

    def _iter(self, prefixes: list[str]) -> list[_Note]:
        notes: list[_Note] = []
        for path in self.root.rglob("*.md"):
            if any(part in SKIP_DIR_NAMES for part in path.parts):
                continue
            rel = path.relative_to(self.root).as_posix()
            if rel in {"README.md", "CLAUDE.md", "CONTRIBUTING.md"} or rel in SKIP_REL_PATHS:
                continue
            if prefixes and not any(rel.startswith(prefix) or prefix in ("", ".") for prefix in prefixes):
                continue
            note = self._parse(path, rel)
            if note:
                notes.append(note)
        return notes

    def _hit(self, rel: str) -> ContextHit | None:
        path = self.root / rel
        if not path.exists():
            return None
        note = self._parse(path, rel)
        return _to_hit(note) if note else None

    def _parse(self, path: Path, rel: str) -> _Note | None:
        raw = path.read_text(encoding="utf-8", errors="ignore")
        meta, body = _frontmatter(raw)
        return _Note(
            path=rel,
            title=meta.get("title") or path.stem.replace("_", " "),
            kind=meta.get("kind") or "record",
            status=meta.get("status") or "",
            body=body,
        )


def vault_root_from_env() -> Path | None:
    raw = os.environ.get("VAULT_ROOT", "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.exists() else None


def _contains(hay: str, token: str) -> bool:
    if token in hay:
        return True
    stem = token[:5] if len(token) >= 5 else token
    return stem in hay


def tokenize(text: str) -> list[str]:
    parts = re.findall(r"[а-яёa-z0-9]{4,}", text.lower())
    return [part for part in parts if part not in STOPWORDS]


def _score(note: _Note, tokens: list[str]) -> int:
    if not tokens:
        return 0
    hay = f"{note.title} {note.path} {note.body}".lower()
    score = 0
    for token in tokens:
        if not _contains(hay, token):
            continue
        score += 3 if _contains(note.title.lower(), token) or _contains(note.path.lower(), token) else 1
    if score == 0:
        return 0
    if note.kind == "fact":
        score += 2
    elif note.kind == "index":
        score -= 1
    return score


def _to_hit(note: _Note) -> ContextHit:
    return ContextHit(
        source="vault",
        title=note.title,
        excerpt=_excerpt(note.body),
        kind=note.kind if note.kind in {"fact", "record", "index"} else "record",
        path=note.path,
    )


def _frontmatter(raw: str) -> tuple[dict[str, str], str]:
    if not raw.startswith("---"):
        return {}, raw
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}, raw
    meta: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip("\"'")
    return meta, parts[2]


def _excerpt(body: str, limit: int = 240) -> str:
    chunks: list[str] = []
    for line in body.splitlines():
        text = line.strip()
        if not text or text.startswith("#") or text.startswith("|") or text.startswith("```"):
            continue
        if text.startswith("(Изменено"):
            continue
        if text == "---":
            continue
        chunks.append(text)
        if len(" ".join(chunks)) >= limit:
            break
    text = " ".join(chunks)
    return text if len(text) <= limit else text[: limit - 1] + "…"
