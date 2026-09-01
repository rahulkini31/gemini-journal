"""Tiny retrying JSON client over urllib. No third-party dependencies."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request


# Cloudflare rejects urllib's default "Python-urllib/x.y" signature with
# error 1010 ("banned based on your browser's signature"). Featherless sits
# behind Cloudflare, so every request needs a real User-Agent.
USER_AGENT = "bookbound/0.1 (+https://github.com/alpacahq/alpaca-mcp-server)"


class ApiError(RuntimeError):
    def __init__(self, status: int, body: str, url: str) -> None:
        super().__init__(f"HTTP {status} for {url}: {body[:400]}")
        self.status = status
        self.body = body
        self.url = url


def get_json(
    url: str,
    headers: dict[str, str],
    params: dict[str, object] | None = None,
    *,
    max_retries: int = 3,
    timeout: int = 30,
) -> dict:
    """GET returning parsed JSON, retrying 429 and 5xx with backoff.

    Basic-plan accounts are capped at 200 requests/minute, so callers should
    batch rather than lean on these retries.
    """
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        url = f"{url}?{urllib.parse.urlencode(clean)}"

    last: Exception | None = None
    for attempt in range(max_retries):
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, **headers}, method="GET"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            if exc.code in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
                retry_after = exc.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else 2.0 ** attempt
                time.sleep(min(delay, 30.0))
                last = exc
                continue
            raise ApiError(exc.code, body, url) from exc
        except urllib.error.URLError as exc:
            if attempt < max_retries - 1:
                time.sleep(2.0 ** attempt)
                last = exc
                continue
            raise
    raise RuntimeError(f"unreachable: {last}")


def post_json(
    url: str,
    headers: dict[str, str],
    payload: dict,
    *,
    timeout: int = 30,
) -> dict:
    """POST JSON. Deliberately does NOT retry - order submission must be
    idempotent by client_order_id, not by hopeful repetition."""
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        url,
        data=body,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise ApiError(exc.code, exc.read().decode(errors="replace"), url) from exc
