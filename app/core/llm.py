"""Shared Claude client. Official Anthropic or a custom /v1 proxy."""

from __future__ import annotations

import anthropic

from app.core.config import settings

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


def anthropic_client(*, timeout: float = 60.0, max_retries: int = 1) -> anthropic.Anthropic:
    kwargs: dict = {
        "api_key": settings.anthropic_api_key,
        "timeout": timeout,
        "max_retries": max_retries,
    }
    base = normalize_anthropic_base_url(settings.anthropic_base_url)
    if base:
        kwargs["base_url"] = base
        kwargs["default_headers"] = {"User-Agent": _BROWSER_UA}
    return anthropic.Anthropic(**kwargs)
