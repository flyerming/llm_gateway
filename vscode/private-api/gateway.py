"""Talk to the private gateway: list models, check reachability, probe the wire API.

Both Claude Code and Codex talk to the same LiteLLM-style gateway but want the
base URL in different shapes:

  * Claude Code wants `${ANTHROPIC_BASE_URL}` WITHOUT `/v1` -- it appends
    `/v1/messages` itself.
  * Codex wants `base_url` WITH `/v1` -- it appends `/responses`.

`normalize_base()` papers over that, and accepts either shape on the command line
so the user can paste whatever their ops team handed them.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass


class GatewayError(RuntimeError):
    pass


def normalize_base(base: str, *, keep_v1: bool) -> str:
    """Canonicalise a gateway URL. `keep_v1=False` strips a trailing `/v1`."""
    b = base.strip().rstrip("/")
    if b.endswith("/v1"):
        b = b[: -len("/v1")]
    b = b.rstrip("/")
    if not b.startswith(("http://", "https://")):
        b = "http://" + b
    return b + "/v1" if keep_v1 else b


def _headers(api_key: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    h = {
        "Authorization": f"Bearer {api_key}",
        # LiteLLM accepts either; some Anthropic-shaped gateways only honour this one.
        "x-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def _request(url: str, api_key: str, *, method: str = "GET", body: dict | None = None,
             timeout: float = 30.0) -> tuple[int, bytes]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=_headers(api_key), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # noqa: BLE001 - surface the raw cause to the user
        raise GatewayError(f"{type(e).__name__}: {e}\n  while calling {url}") from e


@dataclass
class Model:
    id: str
    owned_by: str = ""
    created: int = 0
    raw: dict | None = None

    @property
    def is_image(self) -> bool:
        return any(t in self.id.lower() for t in ("image", "dall-e", "flux", "sd-", "stable-diffusion"))

    @property
    def is_embedding(self) -> bool:
        return "embed" in self.id.lower()

    @property
    def is_chat_capable(self) -> bool:
        return not (self.is_image or self.is_embedding)


def list_models(base: str, api_key: str, *, timeout: float = 30.0) -> list[Model]:
    """GET `<base>/v1/models`. Raises GatewayError with a readable message."""
    url = normalize_base(base, keep_v1=True) + "/models"
    status, body = _request(url, api_key, timeout=timeout)
    if status != 200:
        snippet = body[:400].decode(errors="replace")
        raise GatewayError(
            f"gateway returned HTTP {status} for {url}\n  {snippet}\n"
            "  -> check --api-base and --api-key"
        )
    try:
        payload = json.loads(body.decode("utf-8"))
    except ValueError as e:
        raise GatewayError(f"{url} did not return JSON: {e}") from e

    data = payload.get("data", payload if isinstance(payload, list) else [])
    models: list[Model] = []
    for entry in data:
        if isinstance(entry, str):
            models.append(Model(id=entry))
        elif isinstance(entry, dict):
            mid = entry.get("id") or entry.get("model") or entry.get("name")
            if mid:
                models.append(
                    Model(
                        id=str(mid),
                        owned_by=str(entry.get("owned_by", "") or ""),
                        created=int(entry.get("created", 0) or 0),
                        raw=entry,
                    )
                )
    models.sort(key=lambda m: m.id)
    return models


def probe_wire_api(base: str, api_key: str, model: str, *, timeout: float = 60.0) -> dict[str, bool]:
    """Ask which API shapes the gateway actually serves for this model.

    Codex 0.150 dropped `wire_api = "chat"`: it only speaks `/responses`. A gateway
    that only proxies `/chat/completions` therefore cannot drive Codex at all, and
    it is far better to learn that here than from a cryptic failure in the editor.
    `max_output_tokens`/`max_tokens` are tiny so the probe costs almost nothing.
    """
    results: dict[str, bool] = {}

    status, _ = _request(
        normalize_base(base, keep_v1=True) + "/responses",
        api_key,
        method="POST",
        body={"model": model, "input": "ping", "max_output_tokens": 16},
        timeout=timeout,
    )
    results["responses"] = status == 200

    status, _ = _request(
        normalize_base(base, keep_v1=True) + "/chat/completions",
        api_key,
        method="POST",
        body={"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 16},
        timeout=timeout,
    )
    results["chat"] = status == 200
    return results


def probe_anthropic_messages(base: str, api_key: str, model: str, *, timeout: float = 60.0) -> bool:
    """Confirm `<base>/v1/messages` works -- the endpoint Claude Code actually uses."""
    status, _ = _request(
        normalize_base(base, keep_v1=True) + "/messages",
        api_key,
        method="POST",
        body={"model": model, "max_tokens": 16, "messages": [{"role": "user", "content": "ping"}]},
        timeout=timeout,
    )
    return status == 200
