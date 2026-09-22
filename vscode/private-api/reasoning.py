r"""Which thinking levels each model offers Codex, and which one it starts on.

THE SAME FILE NAME AS THE VSCode TOOLKIT, ON PURPOSE
----------------------------------------------------
The store is `$CODEX_HOME/private-reasoning.json`, byte-compatible with the one
`vscode/private-api/reasoning.py` writes. A machine that has both toolkits
configured reads the SAME per-model levels: whichever one wrote last wins, and
neither has to be re-taught. Same for `private-modalities.json`.

WHAT DRIVES THE MENU
--------------------
Codex renders its reasoning choice purely from the model catalog we generate
(`model_catalog_json`). Two fields decide it:

    "supported_reasoning_levels": [{"effort": "low", "description": "..."}, ...]
    "default_reasoning_level": "low"

They are OBJECTS, not bare strings. `["low","high"]` makes the whole catalog
fail to parse, which stops Codex from starting rather than degrading.

THE PRIVATE DEFAULT IS THREE RUNGS: low / high / max
----------------------------------------------------
The design brief asks for three, so DEFAULT_EFFORTS carries three and a private
model gets that menu out of the box. The vscode toolkit stops at `xhigh` instead
of `max` -- its webview could not draw a `max` row for a private provider (it
showed Light/High and silently dropped everything above), so it tops out where
its menu does. The CLI has no such limit, so it goes to the real ceiling.

Measured against this deployment's backends (2026-09-16, `POST /v1/responses`
with `reasoning.effort` set, one call per model per level):

    deepseek-v4.1-flash        low, high, xhigh, max  -> 200
                               minimal, medium        -> 400
                                 "DeepSeek V4.1 reasoning_effort must be low,
                                  high, xhigh, max, or an integer within [1, 10]"
                               (measured under the slug `deepseek-v4.1-flash-test`,
                                which the gateway has since renamed -- the levels
                                travelled with the backend, only the id changed)
    deepseek-v4-flash          every value -> 200  (the backend does not
    glm-5.3-flash              validate at all -- it accepts `medium` and
    qwen3-5-397b               quietly ignores it, so 200 proves nothing)
    xinghai-ultra

The strict backend accepts FOUR values, and `xhigh` is one of them; it is not a
synonym for `max` but the rung below it. It is left out of the default because
the brief asks for three -- but it is one flag away, as the `xhigh` shorthand
(`--reasoning-levels xhigh`), for a model where the extra rung is wanted.

The cost of dropping it from the default is a TUI detail: Codex files `max`
(and `ultra`) under the **"Advanced Reasoning"** submenu, so the ordinary
reasoning menu now offers only `low` / `high` and `max` needs that submenu.
`xhigh`, had it been kept, would have been reachable from the ordinary menu.

`minimal` and `medium` are deliberately absent: on the strict backend they are
an HTTP 400 on every turn, and on the permissive ones they are indistinguishable
from `high` -- a level that means nothing but looks like a choice.

WHERE THE MENU ACTUALLY COMES FROM
----------------------------------
The live value is in `$CODEX_HOME/model-config.jsonc`, which the user owns and
edits; `model-config.seed.jsonc` carries the table above as data. That split is
deliberate -- the reasoning levels were source constants until 2026-09-17, and so
was the input-modality table, which is how a gateway slug rename turned into
Codex refusing every image paste with no fix short of a new release. See
modelconfig.py. This module keeps the ENUM (`KNOWN_EFFORTS`), the menu text, and
the shorthands; the policy lives in the seed, and `DEFAULT_EFFORTS` below is only
the last resort for an unreadable config file.

OVERRIDING
----------
Per model, with `--configure-reasoning` (which writes the older
`private-reasoning.json` store), or by editing `model-config.jsonc` directly --
the config file wins. An empty list is meaningful and kept: it means "no reasoning
menu for this model", which is right for a plain chat model whose backend has no
such knob.

OPENAI'S OWN MODELS ARE LEFT ALONE
----------------------------------
For `gpt-*` / `o*` slugs the levels come out of `$CODEX_HOME/models_cache.json`
verbatim, because the catalog we write REPLACES Codex's own and an entry we emit
for `gpt-5.6-sol` overrides whatever OpenAI shipped. A headless box that has
never signed in has no cache; those models then get OPENAI_FALLBACK_EFFORTS.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import modelconfig
from detect import codex_home, codex_models_cache

# >>> ONE OF THE TWO LINES THAT DIFFER FROM `codexcli/private-api/reasoning.py` <<<
# This copy is the vscode one, so `_toolkits.vscode` in `model-config.jsonc` is
# what applies (it stops at `xhigh` -- the webview cannot draw a `max` row).
# Declared here rather than threaded through every call site: a default argument
# that silently means "codexcli" in a vscode install is exactly the kind of thing
# that gets forgotten.
TOOLKIT = modelconfig.TOOLKIT_VSCODE

# The other differing line is `DEFAULT_EFFORTS` further down, for the same reason.
# Everything else must stay identical to the codexcli copy --
# `diff codexcli/private-api/reasoning.py vscode/private-api/reasoning.py`

# --------------------------------------------------------------------------- #
# the store
# --------------------------------------------------------------------------- #

STORE_FILENAME = "private-reasoning.json"

# Descriptions are copied from OpenAI's own catalog so the menu reads the same
# whichever model the user picks.
EFFORT_DESCRIPTIONS: dict[str, str] = {
    "none": "不进行思考",
    "minimal": "几乎不思考",
    "low": "快速响应，思考较少",
    "medium": "在速度与思考深度之间平衡，适合日常任务",
    "high": "更强的思考深度，适合复杂问题",
    "xhigh": "极高的思考深度，适合复杂问题",
    "max": "最大思考深度，适合最难的问题",
    "ultra": "最大思考深度，并自动委派子任务",
    "persistent": "整个会话期间持续保持思考",
}

# Every value Codex's own enum accepts. A level outside this set makes the
# catalog fail to parse, which takes Codex down on startup rather than
# degrading, so input is validated rather than trusted.
KNOWN_EFFORTS = tuple(EFFORT_DESCRIPTIONS)

# What a private (custom_openai) model gets when nobody has configured it: the
# three rungs the design brief asks for. See the module docstring -- the strict
# backend also accepts `xhigh`, which is reachable via the `xhigh` shorthand
# below rather than being part of the default.
#
# NO LONGER THE PRIMARY SOURCE. The live menu comes from the config file
# (`model-config.jsonc`, seeded by `model-config.seed.jsonc`), which is what makes
# it user-editable and what makes a per-toolkit divergence possible -- vscode stops
# at `xhigh` because its menu cannot draw `max`. These two survive as the LAST
# resort for an unreadable config file: a hand-edited or truncated
# `model-config.jsonc` must cost the user their overrides, not their model menu.
#
# >>> THE ONE LINE THAT DIFFERS FROM `codexcli/private-api/reasoning.py` <<<
# It has to: this is the toolkit's own last resort, and a last resort that hands
# vscode a `max` rung revives the very display bug the `_toolkits.vscode` block
# exists to avoid (the webview silently drops the row). Keep in sync with
# `model-config.seed.jsonc`'s `_toolkits.vscode.reasoning_levels`.
DEFAULT_EFFORTS = ("low", "high", "xhigh")
DEFAULT_EFFORT = "high"

# Shorthands for `--reasoning-levels`, which accepts these names directly.
PROFILE_PRIVATE = "private"   # low,high,max        (the default)
PROFILE_THREE = "three"       # low,high,max        (alias of `private`)
PROFILE_XHIGH = "xhigh"       # low,high,xhigh,max  (the strict backend's full set)
PROFILE_NONE = "none"         # []  -- no reasoning menu at all
PROFILES: dict[str, tuple[str, ...]] = {
    PROFILE_PRIVATE: DEFAULT_EFFORTS,
    PROFILE_THREE: DEFAULT_EFFORTS,
    PROFILE_XHIGH: ("low", "high", "xhigh", "max"),
    PROFILE_NONE: (),
}

# Used for OpenAI's models only when `models_cache.json` cannot be read (a
# headless box that never signed in). Conservative on purpose: a level the model
# does not really support is worse than a missing one, because the user only
# finds out mid-task.
OPENAI_FALLBACK_EFFORTS = ("low", "medium", "high", "xhigh")


def store_path() -> Path:
    return codex_home() / STORE_FILENAME


def _levels(efforts: list[str] | tuple[str, ...] | str) -> list[dict[str, str]]:
    """Turn effort names into catalog objects, dropping anything Codex rejects."""
    if isinstance(efforts, str):
        efforts = [e.strip() for e in efforts.split(",")]
    out: list[dict[str, str]] = []
    for e in efforts:
        name = str(e).strip()
        if name and name in KNOWN_EFFORTS and name not in {x["effort"] for x in out}:
            out.append({"effort": name, "description": EFFORT_DESCRIPTIONS[name]})
    return out


def resolve_profile(spec: str) -> tuple[str, ...]:
    """`--reasoning-levels` value -> effort tuple. `private`/`three`/`none` are
    shorthands for the sets this deployment is known to accept."""
    key = spec.strip().lower()
    if key in PROFILES:
        return PROFILES[key]
    return tuple(e.strip() for e in spec.split(",") if e.strip())


def normalize_entries(raw: object) -> list[dict[str, str]]:
    """Accept either effort names or full objects, return catalog objects.

    Gateways describe their reasoning support inconsistently -- some send
    `["low","high"]`, some send `[{"effort": "high"}]`. Both are worth honouring.
    """
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            names.append(str(item.get("effort", "")))
        else:
            names.append(str(item))
    return _levels(names)


# --------------------------------------------------------------------------- #
# who is "OpenAI's own"
# --------------------------------------------------------------------------- #

# `gpt-5.6-sol`, `codex-mini`, `chatgpt-4o`, `o3-mini` ... A bare `gpt` with no
# separator is included because the catalog uses it for the default entry.
_OPENAI_RE = re.compile(r"^(?:gpt|chatgpt|codex)(?:[-.]|$)|^o[1-9](?:[-.]|$)", re.I)


def is_openai_official(model_id: str) -> bool:
    """True for models OpenAI itself serves, whose levels we must not override."""
    return bool(_OPENAI_RE.match(model_id.strip()))


def openai_levels_from_cache(model_id: str) -> tuple[list[dict[str, str]], str | None]:
    """Codex's own levels for one OpenAI model, straight out of its cache.

    Returns ([], None) when the cache is missing or has nothing for this slug;
    callers fall back to OPENAI_FALLBACK_EFFORTS.
    """
    path = codex_models_cache()
    if not path.exists():
        return [], None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], None

    entries = data.get("models") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return [], None

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("slug", "")) != model_id:
            continue
        levels = normalize_entries(entry.get("supported_reasoning_levels"))
        default = entry.get("default_reasoning_level")
        return levels, (str(default) if default else None)
    return [], None


# --------------------------------------------------------------------------- #
# user overrides
# --------------------------------------------------------------------------- #

def load() -> dict[str, dict[str, object]]:
    """The `$CODEX_HOME/private-reasoning.json` map, or {} when absent/broken.

    A malformed file must not take the whole toolkit down: the catalog can always
    be regenerated from defaults, so this degrades instead of raising.
    """
    path = store_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def save(store: dict[str, dict[str, object]]) -> Path:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        bak = path.with_suffix(path.suffix + ".bak")
        if not bak.exists():  # only ever snapshot the original
            bak.write_bytes(path.read_bytes())
    path.write_text(
        json.dumps(store, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def configure(model_id: str, efforts: list[str] | tuple[str, ...] | str,
              default: str | None = None) -> dict[str, object]:
    """Store one model's levels. Returns the entry that was written.

    An empty `efforts` is meaningful and is kept: it means "no reasoning menu for
    this model", which is how a plain non-reasoning chat model should look.
    """
    levels = _levels(efforts)
    entry: dict[str, object] = {"levels": levels}
    if levels:
        chosen = default or DEFAULT_EFFORT
        # A default outside the offered set would make Codex open the menu on a
        # value the user cannot even see.
        if chosen not in {x["effort"] for x in levels}:
            raise ValueError(
                f"默认档位 {chosen!r} 不在已配置的档位里"
                f"（{', '.join(x['effort'] for x in levels)}）"
            )
        entry["default"] = chosen

    store = load()
    store[model_id] = entry
    save(store)
    return entry


def clear(model_id: str) -> bool:
    store = load()
    if model_id not in store:
        return False
    store.pop(model_id)
    save(store)
    return True


def clear_all() -> int:
    store = load()
    n = len(store)
    save({})
    return n


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #

def levels_for(model_id: str, raw_meta: dict | None = None,
               toolkit: str = TOOLKIT) -> tuple[list[dict[str, str]], str | None]:
    """The catalog levels + default for one model. `([], None)` = no reasoning menu.

    Order, most specific first:

      1. OpenAI's own models -- always `models_cache.json`, never a config file.
         Their behaviour is OpenAI's to define.
      2. `$CODEX_HOME/model-config.jsonc` -- the user's file.
      3. `private-reasoning.json` -- the older per-model store. Still read so a
         machine configured before the config file existed keeps its choices, but
         no longer written.
      4. What the gateway advertises in `GET /v1/models` for that model.
      5. `model-config.seed.jsonc` for this slug, else `_defaults` /
         `_toolkits[<toolkit>]` from the config file.
      6. DEFAULT_EFFORTS -- last resort, for when the config file is unreadable.
    """
    if is_openai_official(model_id):
        levels, default = openai_levels_from_cache(model_id)
        if levels:
            return levels, default
        return _levels(OPENAI_FALLBACK_EFFORTS), OPENAI_FALLBACK_EFFORTS[0]

    levels = normalize_entries(modelconfig.configured(model_id, modelconfig.FIELD_LEVELS))
    if levels:
        default = modelconfig.configured(model_id, modelconfig.FIELD_DEFAULT_LEVEL)
        return levels, _checked_default(levels, default)

    entry = load().get(model_id)
    if entry is not None:
        levels = normalize_entries(entry.get("levels"))
        default = entry.get("default")
        return levels, (str(default) if default else None)

    meta = raw_meta or {}
    for key in ("supported_reasoning_levels", "reasoning_levels"):
        levels = normalize_entries(meta.get(key))
        if levels:
            default = meta.get("default_reasoning_level") or meta.get("default_reasoning_effort")
            return levels, _checked_default(levels, default)

    levels = _levels(modelconfig.suggested_value(
        model_id, modelconfig.FIELD_LEVELS, toolkit) or ())
    if levels:
        default = modelconfig.suggested_value(
            model_id, modelconfig.FIELD_DEFAULT_LEVEL, toolkit)
        return levels, _checked_default(levels, default)

    return _levels(DEFAULT_EFFORTS), DEFAULT_EFFORT


def _checked_default(levels: list[dict[str, str]], default: object) -> str | None:
    """Coerce a default that is not one of the offered levels.

    A `default_reasoning_level` outside `supported_reasoning_levels` opens Codex's
    picker on a value the user cannot see, so a hand-edited mismatch resolves to
    the first level rather than being passed through.
    """
    names = {x["effort"] for x in levels}
    if not names:
        return None
    text = str(default) if default is not None else ""
    return text if text in names else levels[0]["effort"]
