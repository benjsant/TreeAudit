"""Client of the local LLM server (expert mode, D-040), standard library only.

The server is llama.cpp in its own container, reachable from the app
container through an internal network only. Only local addresses are
accepted, and the answer is always forced into a JSON schema: what comes back
is data to validate, never instructions.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from treeaudit.core.errors import TreeAuditError

LLM_URL_ENV_VAR = "TREEAUDIT_LLM_URL"
MAX_RESPONSE_BYTES = 1_000_000


class LlmError(TreeAuditError):
    """The LLM could not give a usable answer."""


@dataclass(frozen=True, slots=True)
class Answer:
    data: dict[str, Any]
    seconds: float
    tokens: int
    tokens_per_second: float


def _check_local(url: str) -> str:
    """Only the LLM container (a bare service name) or this machine."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    local = host in ("localhost", "127.0.0.1", "::1") or (host.isalnum() and "." not in host)
    if parts.scheme != "http" or not local or parts.path not in ("", "/"):
        raise LlmError(f"adresse du LLM refusée : {url} (seulement le conteneur local)")
    return url.rstrip("/")


class LlmClient:
    def __init__(self, url: str, *, timeout: float = 180.0) -> None:
        self.url = _check_local(url)
        self.timeout = timeout

    def _request(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(  # noqa: S310 - local http URL checked above
            self.url + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="GET" if body is None else "POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            detail = exc.read(10_000).decode("utf-8", "replace")
            if "context" in detail:
                raise LlmError(
                    "demande trop longue pour le modèle (trop d'éléments à la fois)"
                ) from exc
            raise LlmError(f"le LLM a refusé la demande (HTTP {exc.code})") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise LlmError(f"le LLM ne répond pas : {exc}") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise LlmError("réponse du LLM trop longue")
        try:
            decoded: Any = json.loads(raw)
        except ValueError as exc:
            raise LlmError("réponse du LLM illisible") from exc
        if not isinstance(decoded, dict):
            raise LlmError("réponse du LLM inattendue")
        return decoded

    def healthy(self) -> bool:
        try:
            return self._request("/health").get("status") == "ok"
        except LlmError:
            return False

    def wait_until_ready(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.healthy():
                return True
            time.sleep(1.0)
        return False

    def ask(
        self,
        system: str,
        user: str,
        schema: Mapping[str, Any],
        *,
        max_tokens: int = 400,
    ) -> Answer:
        """One question, one answer forced into ``schema`` (low temperature)."""
        started = time.monotonic()
        reply = self._request(
            "/v1/chat/completions",
            {
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.1,
                "max_tokens": max_tokens,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "answer", "schema": dict(schema), "strict": True},
                },
            },
        )
        seconds = time.monotonic() - started
        try:
            content = reply["choices"][0]["message"]["content"]
            data = json.loads(content)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LlmError("le LLM n'a pas respecté le format demandé") from exc
        if not isinstance(data, dict):
            raise LlmError("le LLM n'a pas respecté le format demandé")
        raw_timings = reply.get("timings")
        timings: dict[str, Any] = raw_timings if isinstance(raw_timings, dict) else {}
        tokens = int(timings.get("predicted_n", 0) or 0)
        speed = float(timings.get("predicted_per_second", 0.0) or 0.0)
        return Answer(data=data, seconds=seconds, tokens=tokens, tokens_per_second=speed)


def client_from_environment(environ: Mapping[str, str] | None = None) -> LlmClient | None:
    """The LLM of the expert mode, if it is configured (Docker, TREEAUDIT_EXPERT=1)."""
    env = os.environ if environ is None else environ
    url = env.get(LLM_URL_ENV_VAR, "")
    return LlmClient(url) if url else None
