#!/usr/bin/env python3
"""Write KEY=value lines into .env.mcp (deploy-user writable overlay)."""

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
    existing.update(incoming)
    lines = [f"{key}={existing[key]}" for key in sorted(existing)]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("updated", target, "keys", ", ".join(sorted(incoming)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
