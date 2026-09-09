#!/usr/bin/env python3
"""Write KEY=value lines into .env.mcp (deploy-user writable overlay).

The incoming file is the source of truth: a `KEY=` line with an empty value
removes that key from .env.mcp, so clearing a GitHub Secret actually
propagates to prod instead of leaving a stale override behind.
"""

from __future__ import annotations

import sys
from pathlib import Path


def load_kv(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, val = raw.split("=", 1)
        data[key.strip()] = val.strip()
    return data


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: merge_env.py /path/to/incoming.env", file=sys.stderr)
        return 2
    target = Path.cwd() / ".env.mcp"
    incoming = load_kv(Path(sys.argv[1]))
    if not incoming:
        print("no incoming keys")
        return 0
    existing = load_kv(target)
    removed = [key for key, val in incoming.items() if not val]
    for key in removed:
        existing.pop(key, None)
    existing.update({key: val for key, val in incoming.items() if val})
    lines = [f"{key}={existing[key]}" for key in sorted(existing)]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    set_keys = sorted(key for key, val in incoming.items() if val)
    print("updated", target, "set", ", ".join(set_keys) or "-", "removed", ", ".join(sorted(removed)) or "-")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
