"""Shared Claude client. Official Anthropic or a custom /v1 proxy."""

from __future__ import annotations

import anthropic

from app.core.config import settings

# Anthropic's own default. We always pass base_url explicitly: docker-compose
# injects ANTHROPIC_BASE_URL into the container even when unset, as "", and the
# SDK reads that env var itself - an empty string is not None, so it becomes a
# schemeless base_url and every call dies as APIConnectionError.
_DEFAULT_BASE_URL = "https://api.anthropic.com"

# Some Cloudflare workers reject the default Python client signature (error 1010).
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def normalize_anthropic_base_url(raw: str) -> str | None:
    url = (raw or "").strip().rstrip("/")
    if not url:
        return None
    if url.endswith("/v1"):
        url = url[:-3].rstrip("/")
    return url or None


def anthropic_client(*, timeout: float = 60.0, max_retries: int = 3) -> anthropic.Anthropic:
    base = normalize_anthropic_base_url(settings.anthropic_base_url)
    kwargs: dict = {
        "api_key": settings.anthropic_api_key,
        "timeout": timeout,
        "max_retries": max_retries,
        "base_url": base or _DEFAULT_BASE_URL,
    }
    if base:
        kwargs["default_headers"] = {"User-Agent": _BROWSER_UA}
    return anthropic.Anthropic(**kwargs)
