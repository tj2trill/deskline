"""Single place where outbound HTTP clients are built, so tests can swap the
transport and every integration shares timeouts and error handling."""
from __future__ import annotations

import httpx

# Tests set this to an httpx.MockTransport.
TRANSPORT: httpx.BaseTransport | None = None
USER_AGENT = "Deskline/1.0 (+https://github.com/)"


class ProviderError(RuntimeError):
    """An upstream API rejected a request. Carries the provider's own message."""

    def __init__(self, provider: str, status: int, detail: str):
        self.provider = provider
        self.status = status
        self.detail = detail
        super().__init__(f"{provider} returned {status}: {detail[:500]}")


class NotConfigured(RuntimeError):
    """A feature was used without its API key; the message says which setting to add."""


def client(timeout: float = 20.0, follow_redirects: bool = True, **kwargs) -> httpx.Client:
    """follow_redirects=False for user-supplied URLs: each hop must be re-checked (SSRF)."""
    headers = {"User-Agent": USER_AGENT, **kwargs.pop("headers", {})}
    return httpx.Client(timeout=timeout, transport=TRANSPORT, headers=headers, follow_redirects=follow_redirects, **kwargs)


def check(provider: str, resp: httpx.Response) -> httpx.Response:
    if resp.status_code >= 400:
        try:
            body = resp.json()
            detail = body.get("message") or body.get("error") or body
            if isinstance(detail, dict):
                detail = detail.get("message") or str(detail)
            if isinstance(detail, list):
                detail = "; ".join(map(str, detail))
        except Exception:
            detail = resp.text
        raise ProviderError(provider, resp.status_code, str(detail))
    return resp
