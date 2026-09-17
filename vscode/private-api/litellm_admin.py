r"""Fix the gateway's model definitions so Codex's Responses traffic can land.

TWO ROOT CAUSES, BOTH FIXED IN THE MODEL DEFINITION
---------------------------------------------------
Codex has no `wire_api = "chat"` any more (removed in 0.150): every turn it sends
goes to `POST /v1/responses`. Two fields in that request break a private model
registered as `custom_llm_provider = "custom_openai"`, and both have to be
answered on the gateway, because Codex's binary cannot be told to change what it
sends.

1. `reasoning: {"effort": ...}` -- LiteLLM maps this to the OpenAI-style
   `reasoning_effort` parameter and then hands it to the model's provider.
   `custom_openai` does not declare that parameter, so LiteLLM refuses the call
   before it ever leaves the proxy:

       litellm.UnsupportedParamsError: custom_openai does not support parameters:
       ['reasoning_effort'], for model=deepseek-v4-flash

   Every Codex turn against every private model fails with HTTP 400, in both
   wire shapes.

2. `client_metadata` -- an object Codex attaches to every request (session and
   turn ids, sandbox mode, install id). LiteLLM 1.100.1 forwards it verbatim
   into the OpenAI SDK, whose `create()` has no such keyword:

       Custom_openaiException - AsyncCompletions.create() got an unexpected
       keyword argument 'client_metadata'

   which surfaces as HTTP 500 on every turn, streamed or not.

Both were reproduced against the live gateway, and both were confirmed fixed by
the parameters below: with them, Codex's exact captured request body -- tools,
instructions, streaming, `reasoning.effort`, `client_metadata` -- returns 200.

THE FIXES
---------
    litellm_params.allowed_openai_params = [..., "reasoning_effort"]

`allowed_openai_params` is the escape hatch LiteLLM names in its own 400 text
("If you want to use these params dynamically send allowed_openai_params=...").
Codex cannot send it per request, so it goes on the model definition.

    litellm_params.additional_drop_params = ["client_metadata"]

`additional_drop_params` makes LiteLLM discard the named fields just before the
provider call. Dropping `client_metadata` costs nothing: it is Codex's own
telemetry envelope, not model input.

WHY THE WHOLE `litellm_params` IS RESENT
----------------------------------------
`POST /model/update` REPLACES the stored `litellm_params` rather than merging
into it. Sending only the new key would drop `api_base` and
`custom_llm_provider` and break the model for everyone on the gateway. So every
plan here carries the model's complete, existing params with the missing keys
added, and `apply_plan()` refuses to send a body that is missing `api_base`.

This module only ever WRITES when the caller passes `--apply-gateway-config`.
The default is to print commands for a human to run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import gateway

# Codex always sends `reasoning.effort`; LiteLLM turns it into this.
REASONING_PARAM = "reasoning_effort"

# Codex always sends this object; the OpenAI SDK cannot accept it.
CLIENT_METADATA_PARAM = "client_metadata"

# The litellm_params key that tells LiteLLM to drop fields before the provider call.
DROP_PARAMS_KEY = "additional_drop_params"

# Providers that need the fixes. `custom_openai` is what LiteLLM uses for a
# hand-registered OpenAI-compatible endpoint, which is how every private model on
# this gateway is defined. A native provider declares `reasoning_effort` itself
# and gets `client_metadata` consumed by LiteLLM, so neither fix applies.
AFFECTED_PROVIDERS = ("custom_openai",)

UPDATE_PATH = "/model/update"
INFO_PATH = "/model/info"


@dataclass
class Plan:
    """One model's complete replacement `litellm_params`."""
    model_name: str
    model_id: str
    params: dict
    already_ok: bool = False
    reason: str = ""
    # The litellm_params keys this plan adds, in the order they are applied.
    fixes: list[str] = field(default_factory=list)

    @property
    def changed_keys(self) -> list[str]:
        return list(self.fixes)


@dataclass
class Report:
    plans: list[Plan] = field(default_factory=list)

    @property
    def todo(self) -> list[Plan]:
        return [p for p in self.plans if not p.already_ok and not p.reason]

    @property
    def ok(self) -> list[Plan]:
        return [p for p in self.plans if p.already_ok]

    @property
    def skipped(self) -> list[Plan]:
        return [p for p in self.plans if p.reason and not p.already_ok]


def fetch_model_info(base: str, api_key: str, *, timeout: float = 30.0) -> list[dict]:
    """`GET /model/info` -- every model definition, encrypted keys masked by proxy."""
    url = gateway.normalize_base(base, keep_v1=False) + INFO_PATH
    status, body = gateway._request(url, api_key, timeout=timeout)
    if status != 200:
        snippet = body[:400].decode(errors="replace")
        raise gateway.GatewayError(
            f"gateway returned HTTP {status} for {url}\n  {snippet}\n"
            "  -> /model/info needs the LiteLLM MASTER key, not a virtual key"
        )
    try:
        payload = json.loads(body.decode("utf-8"))
    except ValueError as e:
        raise gateway.GatewayError(f"{url} did not return JSON: {e}") from e

    entries = payload.get("data") if isinstance(payload, dict) else payload
    return [e for e in (entries or []) if isinstance(e, dict)]


def _allows_reasoning(params: dict) -> bool:
    allowed = params.get("allowed_openai_params")
    if not isinstance(allowed, list):
        return False
    return any(str(x) == REASONING_PARAM for x in allowed)


def _drops_client_metadata(params: dict) -> bool:
    dropped = params.get(DROP_PARAMS_KEY)
    if not isinstance(dropped, list):
        return False
    return any(str(x) == CLIENT_METADATA_PARAM for x in dropped)


def describe(plan: Plan) -> str:
    """One line naming what this plan adds, for reports and command comments."""
    if plan.already_ok:
        return "already accepts Codex's Responses fields"
    if plan.reason:
        return plan.reason
    parts = []
    if REASONING_PARAM in plan.fixes:
        parts.append(f'allowed_openai_params += "{REASONING_PARAM}"')
    if DROP_PARAMS_KEY in plan.fixes:
        parts.append(f'{DROP_PARAMS_KEY} += "{CLIENT_METADATA_PARAM}"')
    return "; ".join(parts)


def build_report(entries: list[dict]) -> Report:
    """Work out which models need the fixes, and what their new params should be."""
    report = Report()

    for entry in entries:
        name = str(entry.get("model_name") or "")
        params = entry.get("litellm_params") or {}
        info = entry.get("model_info") or {}
        if not isinstance(params, dict) or not isinstance(info, dict):
            continue

        provider = str(params.get("custom_llm_provider") or "")
        plan = Plan(model_name=name, model_id=str(info.get("id") or ""), params=dict(params))

        needs_reasoning = not _allows_reasoning(params)
        needs_drop = not _drops_client_metadata(params)

        if not needs_reasoning and not needs_drop:
            plan.already_ok = True
        elif provider not in AFFECTED_PROVIDERS:
            # A native provider already accepts the parameter, or is not a chat
            # model at all. Saying so beats leaving the user wondering why a
            # model is missing from the list.
            plan.reason = f"provider {provider or '(unset)'} is out of scope"
        elif not plan.model_id:
            plan.reason = "no model_info.id; cannot target it with /model/update"
        elif not params.get("api_base"):
            # Refuse loudly rather than emit a body that would wipe api_base.
            plan.reason = "no api_base in litellm_params; refusing to resend a partial body"
        else:
            if needs_reasoning:
                allowed = params.get("allowed_openai_params")
                merged = list(allowed) if isinstance(allowed, list) else []
                merged.append(REASONING_PARAM)
                plan.params["allowed_openai_params"] = merged
                plan.fixes.append(REASONING_PARAM)

            if needs_drop:
                dropped = params.get(DROP_PARAMS_KEY)
                merged = list(dropped) if isinstance(dropped, list) else []
                merged.append(CLIENT_METADATA_PARAM)
                plan.params[DROP_PARAMS_KEY] = merged
                plan.fixes.append(DROP_PARAMS_KEY)

        report.plans.append(plan)

    report.plans.sort(key=lambda p: p.model_name)
    return report


def update_body(plan: Plan) -> dict:
    return {"model_info": {"id": plan.model_id}, "litellm_params": plan.params}


def emit_commands(base: str, plans: list[Plan], *, key_var: str = "LITELLM_MASTER_KEY") -> str:
    """Ready-to-paste curl for each model, one per line.

    The key is referenced through a shell variable on purpose: these commands get
    pasted into tickets and chat, and the master key opens the whole gateway.
    """
    endpoint = gateway.normalize_base(base, keep_v1=False) + UPDATE_PATH
    lines: list[str] = []

    for p in plans:
        body = json.dumps(update_body(p), ensure_ascii=False)
        lines.append(f"# {p.model_name}  ({describe(p)})")
        lines.append(
            f"curl -sS -X POST {endpoint} \\\n"
            f"  -H \"Authorization: Bearer ${key_var}\" \\\n"
            f"  -H \"Content-Type: application/json\" \\\n"
            f"  -d '{body}'"
        )
        lines.append("")
    return "\n".join(lines).rstrip()


def apply_plan(base: str, api_key: str, plan: Plan, *, timeout: float = 60.0) -> None:
    """Send one model's new params. Raises GatewayError on refusal.

    Note this REPLACES litellm_params server-side -- `plan.params` is the model's
    full existing set with the missing keys added, never just the delta.
    """
    if not plan.params.get("api_base"):
        raise gateway.GatewayError(
            f"refusing to update {plan.model_name}: the new litellm_params has no "
            "api_base, which would break the model for every client"
        )

    url = gateway.normalize_base(base, keep_v1=False) + UPDATE_PATH
    status, body = gateway._request(
        url, api_key, method="POST", body=update_body(plan), timeout=timeout,
    )
    if status != 200:
        snippet = body[:500].decode(errors="replace")
        raise gateway.GatewayError(
            f"gateway refused the update for {plan.model_name} (HTTP {status})\n  {snippet}"
        )
