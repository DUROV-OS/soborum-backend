#!/usr/bin/env python3
"""Merge KEY=value lines from a file into ./.env without wiping other secrets."""

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
    root = Path.cwd()
    env_path = root / ".env"
    incoming = load_kv(Path(sys.argv[1]))
    if not incoming:
        print("no incoming keys")
        return 0

    existing: dict[str, str] = {}
    order: list[str] = []
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, val = line.split("=", 1)
                key = key.strip()
                existing[key] = val.strip()
                order.append(key)
            else:
                order.append(line)

    for key, val in incoming.items():
        if key not in existing:
            order.append(key)
        existing[key] = val

    out: list[str] = []
    seen: set[str] = set()
    for item in order:
        if item in existing and item not in seen:
            out.append(f"{item}={existing[item]}")
            seen.add(item)
        elif item not in existing:
            out.append(item)
    for key, val in existing.items():
        if key not in seen:
            out.append(f"{key}={val}")

    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print("updated", env_path, "keys", ", ".join(sorted(incoming)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
