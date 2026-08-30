"""Endpoint-aware LLM client (US2 / FR-003).

Mirrors the endpoint-aware embedder's house style (``ingest/embedding.py``
``build_endpoint_embedder``): stdlib urllib, Bearer ``llm.api_key`` auth,
pinned ``llm.model`` in the payload, OpenAI-compatible
``{endpoint}/chat/completions`` POST.  Reads ``llm.endpoint`` +
``llm.api_key`` + ``llm.model`` from the config layer so the credential
reaches the client on an ingestion path (FR-003 — the pre-US2 code only
consumed those knobs in ``health.check_llm`` and the 501-surface chat
handlers, which made no LLM call).

The returned client is callable ``(prompt: str) -> str`` and also exposes a
``complete(prompt)`` method for the duck-typed call sites.
"""

from __future__ import annotations

import json as _json
import urllib.error
import urllib.request

from digital_twins.config.schema import get

_HTTP_TIMEOUT_S = 60


class LLMClientError(Exception):
    """The LLM endpoint rejected the request (auth / unreachable / 5xx)."""

    def __init__(self, status, detail):
        super().__init__(f"llm endpoint: {status} — {detail}")
        self.status = status
        self.detail = detail


def build_llm_client(cfg):
    """Build an endpoint-aware LLM client from ``cfg``.

    Returns a callable that POSTs to ``{llm.endpoint}/chat/completions``
    with the Bearer ``llm.api_key`` header and the pinned ``llm.model`` in
    the payload, and returns the response ``choices[0].message.content``.

    Raises :class:`LLMClientError` on 401/403/5xx.  Raises
    ``SchemaError`` when ``llm.endpoint`` is unset (the client is only
    built when the endpoint is configured).
    """
    endpoint = (get(cfg, "llm.endpoint") or "").rstrip("/")
    api_key = get(cfg, "llm.api_key") or ""
    model = get(cfg, "llm.model") or ""
    if not endpoint:
        from digital_twins.config.schema import SchemaError
        raise SchemaError("llm.endpoint is not configured")
    url = endpoint + "/chat/completions"

    def _post(payload: dict, timeout: int = _HTTP_TIMEOUT_S):
        body = _json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(url, data=body, headers=headers,
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def complete(prompt: str) -> str:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        }
        # 5xx retry-once (house style, mirrors health.py's retry semantics).
        status, raw = _post(payload)
        if 500 <= status < 600:
            status, raw = _post(payload)
        if status != 200:
            raise LLMClientError(status, f"HTTP {status} from {url}")
        data = _json.loads(raw.decode("utf-8"))
        choices = data.get("choices") or []
        if not choices:
            raise LLMClientError(status, "no choices in LLM response")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if content is None:
            raise LLMClientError(status, "no message.content in LLM response")
        return content

    # Duck-typed: the RED test calls ``client(prompt)`` OR
    # ``client.complete(prompt)``.  Return a callable object exposing both.
    class _Client:
        def complete(self, prompt):
            return complete(prompt)

        def __call__(self, prompt):
            return complete(prompt)

    return _Client()


# Alias for the test resolver's candidate names.
make_llm_client = build_llm_client
