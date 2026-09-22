r"""Point the Codex CLI at a private gateway, and keep its model list live.

Codex does not enumerate `${base_url}/models` the way Claude Code does, so a
private deployment has to be wired up in four parts:

  1. a `[model_providers.<id>]` table in `$CODEX_HOME/config.toml` naming the
     gateway, the credential, and the wire protocol;
  2. a `model` value naming which served model to use;
  3. a **model catalog** -- the JSON file `model_catalog_json` points at. Without
     it the model list shows OpenAI's own lineup (GPT-5.6 Sol/Terra/Luna,
     GPT-5.5, ...) because Codex falls back to the catalogue it fetched from
     `chatgpt_base_url`. None of those exist on this gateway, so picking one
     fails on the first turn. Writing our own catalog *replaces* that list
     entirely, which is what makes `/model` show -- and only show -- the
     gateway's models;
  4. `$CODEX_HOME/auth.json`, holding the GATEWAY key. Codex decides whether it
     is signed in by looking for that file, independently of which provider a
     turn will use -- see codex_auth.py. Without it the CLI opens a login gate a
     private gateway can never satisfy.

Parts 3 and 4 are what make "no login, `/model` lists the gateway" work; this
module owns 1-3, `codex_auth.py` owns 4.

KEEPING THE LIST LIVE
---------------------
`refresh_catalog()` re-fetches `GET /v1/models` and rewrites the catalog. It is
deliberately cheap and fail-open, because the installed `codex` wrapper calls it
on every launch: a gateway that is down or slow must delay the user's first
prompt by nothing, never break it. `maybe_refresh()` adds the throttle and the
"never raise" behaviour the wrapper relies on.

THE CATALOG FORMAT, AND WHY IT IS VERSION-SENSITIVE
---------------------------------------------------
`model_catalog_json` is a path to `{"models": [ <entry>, ... ]}`. serde rejects
the whole file unless every entry carries these keys, which the binary reports
one at a time as `missing field \`x\``:

    slug, display_name, supported_reasoning_levels, shell_type, visibility,
    supported_in_api, priority, support_verbosity, truncation_policy,
    experimental_supported_tools

plus `base_instructions` (or `model_messages.instructions_template`) -- and an
empty `models` array is rejected with "must contain at least one model". The
remaining keys of Codex's own catalogue entries are optional; we set the handful
that change behaviour and leave the rest to their defaults.

TARGET VERSION
--------------
This toolkit is written for codex-cli 0.154.0 (`TARGET_VERSION`). Every tool run
prints the binary's version and how it compares, so a box running something else
says so out loud instead of failing later. When a newer release changes the
config or catalog surface, re-adapt against it and move the constant.

The key set above is 0.154.0's, read off its `ModelInfo` struct. It is also
0.145's, plus one key and minus another -- which is the whole reason
`MIN_SUPPORTED_VERSION` exists (each boundary read off that release's source):

    <=0.144  supports_reasoning_summaries          (required, bool)
    0.145+   supports_reasoning_summary_parameter  (renamed; #[serde(default = "default_true")])
    0.155+   supports_reasoning_summaries          (dropped entirely)

    supports_parallel_tool_calls  0.143 .. 0.147, required throughout; off
                                  `ModelInfo` from 0.148

    base_instructions  a plain `ModelInfo` field through 0.146; off it from
                       0.147, but still accepted -- a legacy shim promotes it
                       into `model_messages.instructions_template` and errors
                       if neither is set. We keep sending the old spelling.

A catalogue written for one side of that rename fails on the other with a
`missing field` error before Codex ever starts, which is exactly what happened
on the 0.144.1 box. So we emit ALL THREE spellings. That is safe because
`ModelInfo` is not `#[serde(deny_unknown_fields)]` in any version from 0.143
through main -- an unrecognised key is ignored, so one generated catalog loads
on every release in the supported range, 0.154.0 included.

Both summary flags are `false` on purpose, and it is a 400 either way: setting
`supports_reasoning_summaries`/`..._parameter` is what makes Codex put
`reasoning.summary` on the request, and this gateway answers that with
`400 ... invalid type: map, expected a string` (measured 2026-09-17, one call
per model). The 0.145+ default is `true`, so on a newer binary the explicit
`false` is doing real work rather than just satisfying serde. Parallel tool
calls are off for the same reason -- nothing here proves the backend handles
them, and a wrong `true` surfaces mid-task.

Evidence this catalogue is authoritative rather than additive: with a catalog
that lists only gateway models, `-m gpt-5.6-sol` prints "Model metadata for
`gpt-5.6-sol` not found. Defaulting to fallback metadata" while a gateway id
runs clean.

WIRE API
--------
`wire_api = "chat"` was removed in Codex 0.150 ("`wire_api = "chat"` is no
longer supported. How to fix: set `wire_api = "responses"`"). The gateway must
therefore serve `POST /v1/responses`, which LiteLLM does. `probe_wire_api()`
verifies that before writing the config rather than letting it fail later.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

import modalities
import modelconfig
import reasoning
from tomlpatch import TomlFile

if TYPE_CHECKING:  # pragma: no cover - typing only
    from gateway import Model

DEFAULT_PROVIDER_ID = "private"
DEFAULT_ENV_KEY = "PRIVATE_API_KEY"
PROVIDER_TABLE_FMT = "model_providers.{pid}"
# Codex strips a trailing `/v1` from base_url and re-appends it per endpoint, but
# being explicit matches every provider example in its docs and avoids ambiguity.
WIRE_API = "responses"

# Values that only mean something to OpenAI's own API. Forwarded to a private
# gateway they range from ignored to rejected, so we flag them rather than edit.
OPENAI_ONLY_ROOT_KEYS = ("service_tier",)


def provider_table(pid: str = DEFAULT_PROVIDER_ID) -> str:
    return PROVIDER_TABLE_FMT.format(pid=pid)


def env_key_name(pid: str = DEFAULT_PROVIDER_ID) -> str:
    return DEFAULT_ENV_KEY if pid == DEFAULT_PROVIDER_ID else f"{pid.upper().replace('-', '_')}_API_KEY"


def build_provider_table(base_url: str, *, pid: str = DEFAULT_PROVIDER_ID,
                         env_key: str | None = None, inline_key: str | None = None,
                         name: str = "Private Gateway") -> dict[str, object]:
    """The `[model_providers.<id>]` body.

    `inline_key` writes the secret into config.toml via `experimental_bearer_token`
    -- no environment variable, so it works on a gateway host reached with
    `docker exec` (a non-interactive shell never sources `~/.bashrc` and would
    see no env var at all). The cost is the key sitting in a plaintext file, which
    it already does in `auth.json` either way.

    Without `inline_key` Codex reads `env_key` from the environment instead.
    """
    body: dict[str, object] = {
        "name": name,
        "base_url": base_url,
        "wire_api": WIRE_API,
        # Keep Codex from demanding a ChatGPT login for a provider that will never
        # accept one.
        "requires_openai_auth": False,
    }
    if inline_key:
        body["experimental_bearer_token"] = inline_key
    else:
        body["env_key"] = env_key or env_key_name(pid)
    return body


def apply_config(path: Path, base_url: str, model: str | None, *, pid: str = DEFAULT_PROVIDER_ID,
                 env_key: str | None = None, inline_key: str | None = None,
                 profile: str | None = None) -> dict[str, object]:
    """Write provider + model into config.toml. Returns a summary of what changed."""
    doc = TomlFile(path)
    changed: dict[str, object] = {}

    prev_model = doc.get("model")
    prev_provider = doc.get("model_provider")

    table = build_provider_table(base_url, pid=pid, env_key=env_key, inline_key=inline_key)
    # A provider table we wrote in a previous run may still carry the OTHER
    # credential field (`env_key` when we now inline, or the reverse). Both
    # present is ambiguous -- Codex prefers one of them silently -- so drop it.
    existing = TomlFile(path)
    for stale in ("env_key", "experimental_bearer_token"):
        if stale not in table:
            existing.remove_key(stale, table=provider_table(pid))
    existing.save(backup=False)

    doc = TomlFile(path)
    doc.set_table(provider_table(pid), table)
    changed["provider_table"] = provider_table(pid)

    if profile:
        # A profile keeps any existing OpenAI/ChatGPT setup intact.
        doc.set_table(f"profiles.{profile}", {
            "model": model or "<pick-with --model>",
            "model_provider": pid,
        })
        changed["profile"] = profile
    else:
        if model:
            doc.set_top("model", model)
        doc.set_top("model_provider", pid)

    changed["previous"] = {"model": prev_model, "model_provider": prev_provider}
    doc.save()
    return changed


def warn_openai_only_keys(path: Path) -> list[str]:
    if not path.exists():
        return []
    doc = TomlFile(path)
    return [k for k in OPENAI_ONLY_ROOT_KEYS if doc.get(k) is not None]


# Not in OPENAI_ONLY_ROOT_KEYS: a private gateway accepts the parameter fine. The
# problem is scope, not meaning -- set at the root it becomes a global default and
# outranks each model's `default_reasoning_level` in the catalog, so every model
# in the picker starts at that one level instead of the level its entry chose.
REASONING_OVERRIDE_KEY = "model_reasoning_effort"


def strip_reasoning_override(path: Path) -> str | None:
    """Delete the root-level `model_reasoning_effort` so each catalog entry wins.

    A global `model_reasoning_effort = "none"` (or any other value) at the root
    outranks every model's `default_reasoning_level` in the catalog. The result
    is that the reasoning menu either vanishes entirely or every model starts at
    the same effort regardless of what its catalog entry says. Deleting the line
    lets each model use the level its entry declares, which is the only way the
    per-model reasoning configuration actually takes effect.

    Returns the removed value, or None when the key was not present.
    """
    doc = TomlFile(path)
    value = doc.get(REASONING_OVERRIDE_KEY)
    if value is None:
        return None
    doc.remove_top(REASONING_OVERRIDE_KEY)
    doc.save()
    return value


def strip_openai_only_keys(path: Path) -> list[str]:
    doc = TomlFile(path)
    removed = [k for k in OPENAI_ONLY_ROOT_KEYS if doc.remove_top(k)]
    if removed:
        doc.save()
    return removed


def restore_from_backup(path: Path) -> bool:
    """Put the pristine copy back and drop the backup."""
    bak = path.with_suffix(path.suffix + ".bak")
    if not bak.exists():
        return False
    original = bak.read_bytes()
    if original:
        path.write_bytes(original)
    elif path.exists():
        path.unlink()
    bak.unlink()
    return True


# --------------------------------------------------------------------------- #
# the API key
# --------------------------------------------------------------------------- #

# Shell rc files to append an export to, in the order we would try them. Both
# interactive and login shells have to be covered: an interactive bash reads
# `.bashrc`, a login shell reads `.profile`, and zsh reads `.zshrc`.
RC_FILES = (".bashrc", ".zshrc", ".profile")


def set_env_var(name: str, value: str) -> str:
    """Persist an environment variable by appending an export to a shell rc file.

    Only used with `--use-env-key`; the default is to inline the key in
    config.toml instead. This is the fragile option on a server: `docker exec`
    and cron run non-interactive shells that never source `~/.bashrc`, so the
    variable is simply absent and Codex reports a missing `env_key`.
    """
    from detect import home

    shell = os.environ.get("SHELL", "")
    candidates = [".zshrc"] if "zsh" in shell else list(RC_FILES)
    rc = next((home() / c for c in candidates if (home() / c).exists()),
              home() / candidates[0])

    marker = f"export {name}="
    existing = rc.read_text(encoding="utf-8") if rc.exists() else ""
    if marker in existing:
        return f"{name} 已在 {rc} 中导出（可在该文件里修改以轮换密钥）"

    with rc.open("a", encoding="utf-8") as fh:
        fh.write(f'\n# added by codexcli/private_api.py: Codex gateway key\n'
                 f'export {name}="{value}"\n')
    return f"已把 {marker}... 追加到 {rc}（请打开新终端）"


def env_var_is_set(name: str) -> bool:
    return bool(os.environ.get(name))


# --------------------------------------------------------------------------- #
# reading back what a previous run wrote
# --------------------------------------------------------------------------- #

def saved_base(config: Path) -> str | None:
    """`base_url` from a provider table this tool wrote, unquoted."""
    if not config.exists():
        return None
    doc = TomlFile(config)
    for pid in (DEFAULT_PROVIDER_ID, "private-gateway"):
        v = doc.get("base_url", table=provider_table(pid))
        if v:
            return v.strip("'\"")
    return None


def saved_model(config: Path) -> str | None:
    if not config.exists():
        return None
    v = TomlFile(config).get("model")
    return v.strip("'\"") if v else None


def saved_key(config: Path) -> str | None:
    """The key a previous run left behind, for a refresh that has no --api-key.

    Two places it can be, depending on how the run was invoked: inline in the
    provider table as `experimental_bearer_token` (the default here), or in the
    environment variable that table's `env_key` names. Same secret, same gateway,
    written by this tool -- reading it back is not a new exposure, and it is
    never printed. Returns None when the provider table is not one of ours.
    """
    if not config.exists():
        return None
    doc = TomlFile(config)
    for pid in (DEFAULT_PROVIDER_ID, "private-gateway"):
        table = provider_table(pid)
        if doc.get("base_url", table=table) is None:
            continue
        token = doc.get("experimental_bearer_token", table=table)
        if token:
            return token.strip("'\"")
        name = doc.get("env_key", table=table)
        if name:
            value = os.environ.get(name.strip("'\""))
            if value:
                return value
    return None


# --------------------------------------------------------------------------- #
# the model catalog -- what `/model` lists
# --------------------------------------------------------------------------- #

# Top-level `config.toml` key holding a PATH to the catalog JSON.
CATALOG_KEY = "model_catalog_json"
# Lives beside config.toml so the pair travels together (CODEX_HOME is portable,
# and a profile switch keeps pointing at the same catalog).
CATALOG_FILENAME = "gateway-models.json"

# Codex demands one of these two; `base_instructions` is the simpler of the pair
# and, unlike `model_messages`, needs no template plumbing.
BASE_INSTRUCTIONS = (
    "You are Codex, a coding agent. You and the user share one workspace, and "
    "your job is to collaborate with them until their goal is genuinely handled.\n\n"
    "Use the tools you are given to inspect the repository and change files. "
    "Prefer the `apply_patch` tool for edits. Keep the user informed with short "
    "updates as you work, do not over-format your replies, and verify what you "
    "changed before you claim it is done. Be concise and precise."
)

# What Codex does when a tool result is longer than it wants to carry. 10000
# tokens is the value its own entries use.
TRUNCATION_POLICY = {"mode": "tokens", "limit": 10000}

# Gateways rarely report a context length (LiteLLM only fills `max_input_tokens`
# for models it has metadata for), and an entry without one makes Codex fall back
# to something tiny. 128K is a safe floor for a modern coding model.
#
# NO LONGER THE PRIMARY SOURCE: `context_window_for` reads the user's
# `model-config.jsonc`, then the gateway, then `model-config.seed.jsonc`, and only
# falls back here. Kept as the last resort for an unreadable config file -- the
# per-model resolution order is in that function's docstring.
DEFAULT_CONTEXT_WINDOW = 128_000

# How long a refreshed catalog is trusted for, in the wrapper's fast path. Codex
# re-reads the file at startup, so a stale catalog only matters when the gateway
# gained or lost a model -- five minutes covers "I just added one" without a
# network round trip on every launch.
REFRESH_TTL_SECONDS = 300


# --------------------------------------------------------------------------- #
# which codex versions this catalog works on
# --------------------------------------------------------------------------- #

# The release this toolkit is written for. The config keys it writes, the
# `wire_api` it insists on, and the catalog key set all come from 0.154.0's own
# source. Move this -- and re-check that surface -- when adapting to a newer
# codex, rather than letting the version quietly drift ahead of the code.
TARGET_VERSION = (0, 154, 0)

# The oldest release whose ModelInfo we have checked against its own source. The
# required-key set was stable from here until the 0.145 rename, which we cover by
# emitting both spellings; below this we have no evidence either way, so the
# toolkit says so rather than guessing.
MIN_SUPPORTED_VERSION = (0, 143)

# The newest release the generated catalog has actually been parsed by, through
# `codex debug models` against a real binary (0.153.4, the newest on hand). The
# 0.154.0 key set is verified from source instead; nothing here has run 0.154.0.
TESTED_THROUGH_VERSION = (0, 153, 4)


def parse_version(text: str | None) -> tuple[int, ...] | None:
    """`codex-cli 0.144.1` -> `(0, 144, 1)`. None when there is no version in it.

    Only the leading numeric triple is kept: the binaries append pre-release
    suffixes (`0.155.0-alpha.15`) that must not affect the comparison.
    """
    if not text:
        return None
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    if not m:
        return None
    return tuple(int(g) for g in m.groups() if g is not None)


def _vstr(v: tuple[int, ...]) -> str:
    return ".".join(str(p) for p in v)


def target_version() -> str:
    """The codex release this toolkit is adapted to, as `0.154.0`."""
    return _vstr(TARGET_VERSION)


def version_note(version: str | None) -> tuple[bool, str]:
    """`(ok, one-line verdict)` for the codex version in use.

    `ok` is False only when the binary is provably too old. Anything at or above
    `MIN_SUPPORTED_VERSION` is allowed to run -- including versions past the
    target, which get a warning rather than a refusal, because the catalog has
    survived every rename so far and a false stop is worse than the risk it
    guards against. An unreadable version is reported as ok with a caveat.
    """
    parsed = parse_version(version)
    target = target_version()
    lo = _vstr(MIN_SUPPORTED_VERSION)
    if parsed is None:
        return True, (f"未知 codex 版本 —— 本工具适配的版本是 "
                      f"{target}；可用 `codex --version` 确认")
    if parsed < MIN_SUPPORTED_VERSION:
        return False, (f"{version} 低于最低支持版本（{lo}）。"
                       f"它写入的模型目录可能无法在启动时解析 —— 请升级 codex，"
                       f"否则可能遇到模型目录解析器的 `missing field` 报错。")
    if parsed == TARGET_VERSION:
        return True, f"{version} —— 正是本工具适配的版本"
    if parsed > TARGET_VERSION:
        return True, (f"{version} 高于适配版本（{target}）。"
                      f"此前的配置和目录键名发生过变动，如果 `codex` 无法启动"
                      f"或忽略了这些模型，请按 {version} 重新适配本工具。")
    return True, (f"{version} 低于适配版本（{target}），但仍在支持范围内"
                  f"（{lo}+）；模型目录也兼容较早的键名集合")


def catalog_path() -> Path:
    from detect import codex_home
    return codex_home() / CATALOG_FILENAME


def context_window_for(model_id: str, raw: dict | None = None,
                       toolkit: str = modelconfig.TOOLKIT_CODEX) -> int:
    """How much context Codex should assume for one model.

    Order, most specific first:

      1. `model-config.jsonc` (beside `private_api.py`) -- the user's file. This
         is the only way to PIN a number, and the generated file writes one out
         for every model so the value is visible and editable rather than implied.
      2. The gateway's `max_input_tokens`. LiteLLM only fills it for models it
         has metadata for, so it is frequently absent.
      3. `model-config.seed.jsonc` / `_defaults` from the config file.
      4. DEFAULT_CONTEXT_WINDOW -- last resort for an unreadable config file.

    This is a **compaction trigger, not a client-side cap**: Codex auto-compacts
    at 90% and hard-stops at 95% of it rather than raising. So over-filling it is
    the dangerous direction -- Codex then sends oversized requests the gateway
    400s -- which is why every fallback here is the conservative one.
    """
    meta = raw or {}
    candidates = (
        modelconfig.configured(model_id, modelconfig.FIELD_CONTEXT),
        meta.get("max_input_tokens"),
        modelconfig.suggested_value(model_id, modelconfig.FIELD_CONTEXT, toolkit),
    )
    for candidate in candidates:
        try:
            return int(candidate)
        except (TypeError, ValueError):
            continue
    return DEFAULT_CONTEXT_WINDOW


def catalog_entry(model: "Model", priority: int) -> dict[str, object]:
    """One catalog entry for a gateway model.

    `slug` is what goes on the wire, so it is the gateway's model id verbatim --
    Codex sends it as `model` and LiteLLM routes on exactly that string.
    """
    raw = model.raw or {}
    ctx = context_window_for(model.id, raw)

    levels, default_level = reasoning.levels_for(model.id, raw)
    if not levels:
        default_level = None

    return {
        "slug": model.id,
        "display_name": model.id,
        "description": f"私有网关模型 · {ctx // 1000}K 上下文",
        "priority": priority,
        # "list" is what puts an entry in the picker; Codex's own hidden entries
        # (gpt-reserve) use "hide".
        "visibility": "list",
        # Codex renders the thinking-strength menu from these two fields, and the
        # selected value comes back on the Responses request as
        # `reasoning.effort`. Both are computed together -- see reasoning.py.
        "supported_reasoning_levels": levels,
        "default_reasoning_level": default_level,
        "shell_type": "unified_exec",
        "supported_in_api": True,
        "support_verbosity": False,
        "truncation_policy": dict(TRUNCATION_POLICY),
        "experimental_supported_tools": [],
        # Required through 0.146, and from 0.147 still required but via a legacy
        # shim that maps it into `model_messages.instructions_template`. Works
        # on 0.154.0 -- the target -- and everything older.
        "base_instructions": BASE_INSTRUCTIONS,
        # Required by <=0.144 and by 0.145+ respectively; the two names are the
        # same field before and after a rename. Both false -- see the module
        # docstring: `true` puts `reasoning.summary` on the wire and the gateway
        # 400s it. The `_parameter` spelling is what 0.154.0 reads, and its
        # default is `true`, so this explicit `false` is load-bearing there;
        # emitting the old spelling too is what lets one catalog serve a 0.144.1
        # box and a 0.154.0 box at once.
        "supports_reasoning_summaries": False,
        "supports_reasoning_summary_parameter": False,
        # Required 0.143 .. 0.147, ignored by 0.148+ (it is not a field of
        # ModelInfo there). Nothing proves the backends do parallel tool calls,
        # and a wrong `true` only shows up mid-task.
        "supports_parallel_tool_calls": False,
        "context_window": ctx,
        "max_context_window": ctx,
        # Without this Codex will not offer `apply_patch`, and the agent ends up
        # unable to edit files at all.
        "apply_patch_tool_type": "freeform",
        # Web search would be proxied to OpenAI's backend, not the gateway.
        "supports_search_tool": False,
        # Whether the user can attach an image at all. Codex refuses the
        # attachment client-side from this field alone, so it has to match what
        # the backend can really do -- see modalities.py for the measurement.
        "input_modalities": modalities.modalities_for(model.id, raw),
    }


def build_catalog(models: Iterable["Model"]) -> dict[str, object]:
    """`{"models": [...]}` for every chat-capable gateway model.

    Image endpoints are left out: Codex would accept the slug and then fail on
    turn one.
    """
    entries = [catalog_entry(m, i) for i, m in enumerate(models) if m.is_chat_capable]
    return {"models": entries}


def write_catalog(path: Path, models: Iterable["Model"]) -> list[str]:
    """Write the catalog JSON. Returns the slugs it now lists."""
    catalog = build_catalog(models)
    entries = catalog["models"]
    if not entries:
        raise ValueError("网关没有可用于目录的对话模型")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic: the wrapper may be racing a running Codex that is reading this.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)
    return [str(e["slug"]) for e in entries]


def read_catalog(path: Path) -> list[str]:
    """The slugs the catalog file currently lists, or [] when unreadable."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data.get("models") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    return [str(e.get("slug", "")) for e in entries if isinstance(e, dict)]


def existing_catalog_path(config: Path) -> str | None:
    """The path `model_catalog_json` names, unquoted. None when unset."""
    if not config.exists():
        return None
    raw = TomlFile(config).get(CATALOG_KEY)
    return raw.strip("'\"") if raw else None


def apply_catalog(config: Path, catalog_file: Path) -> str | None:
    """Point `model_catalog_json` at `catalog_file`. Returns the previous value."""
    doc = TomlFile(config)
    previous = doc.get(CATALOG_KEY)
    doc.set_top(CATALOG_KEY, str(catalog_file))
    doc.save()
    return previous


def clear_catalog(config: Path) -> bool:
    """Drop the `model_catalog_json` line so Codex falls back to its own catalog."""
    doc = TomlFile(config)
    if not doc.remove_top(CATALOG_KEY):
        return False
    doc.save(backup=False)
    return True


def catalog_is_ours(config: Path) -> bool:
    """True when `model_catalog_json` points at a file this tool generated.

    Used by `--restore`: a catalog the user pointed at by hand must survive, and
    so must the file it names.
    """
    value = existing_catalog_path(config)
    if not value:
        return False
    return Path(value).name == CATALOG_FILENAME


def remove_catalog_file(path: Path) -> bool:
    if not path.exists():
        return False
    path.unlink()
    return True


# --------------------------------------------------------------------------- #
# the fast path the `codex` wrapper calls
# --------------------------------------------------------------------------- #

def stamp_path() -> Path:
    from detect import codex_home
    return codex_home() / ".gateway-models.stamp"


def mark_refreshed(models: Iterable["Model"], when: float) -> None:
    """Record what we last pulled, so the wrapper can decide to skip a fetch."""
    from detect import codex_home
    payload = {"at": when, "count": len(list(models))}
    p = codex_home() / ".gateway-models.stamp"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    except OSError:
        pass


def last_refresh_age(now: float) -> float | None:
    """Seconds since the last successful refresh, or None if there never was one."""
    p = stamp_path()
    if not p.exists():
        return None
    try:
        at = float(json.loads(p.read_text(encoding="utf-8")).get("at", 0))
    except (OSError, ValueError, AttributeError):
        return None
    if not at:
        return None
    return max(0.0, now - at)


# --------------------------------------------------------------------------- #
# the installed `codex` wrapper
# --------------------------------------------------------------------------- #

WRAPPER_KIND = "codexcli-private-api-wrapper"

# Where the wrapper goes. `~/.local/bin` is on PATH by default on most distros
# and needs no root; it is also where a user-installed codex often already
# lives, which is exactly why we check what we might be shadowing first.
WRAPPER_DIR = "~/.local/bin"

_WRAPPER_SH = """#!/usr/bin/env sh
# {kind}
#
# Refresh the gateway model catalog, then run the real codex. Installed by
# codexcli/private_api.py --install-wrapper; remove with --uninstall-wrapper.
#
# The refresh is throttled and ALWAYS exits 0: if the gateway is slow, down, or
# the token expired, you get the catalog from last time and codex starts anyway.
CODEXCLI_HOME="{home}"
CODEXCLI_PY="{python}"
REAL_CODEX="{real}"
THROTTLE="${{CODEXCLI_REFRESH_TTL:-{ttl}}}"

if [ -x "$CODEXCLI_PY" ] || command -v "$CODEXCLI_PY" >/dev/null 2>&1; then
    CODEX_HOME_DIR="${{CODEX_HOME:-$HOME/.codex}}"
    STAMP="$CODEX_HOME_DIR/.gateway-models.stamp"
    if [ -f "$STAMP" ] && [ -n "$THROTTLE" ]; then
        AGE=$(( $(date +%s) - $(stat -c %Y "$STAMP" 2>/dev/null || echo 0) ))
        [ "$AGE" -lt "$THROTTLE" ] && exec "$REAL_CODEX" "$@"
    fi
    "$CODEXCLI_PY" "$CODEXCLI_HOME/private_api.py" --sync --quiet || true
fi

exec "$REAL_CODEX" "$@"
"""


def wrapper_dir() -> Path:
    return Path(WRAPPER_DIR).expanduser()


def wrapper_path() -> Path:
    return wrapper_dir() / "codex"


def wrapper_is_ours(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return False
    return WRAPPER_KIND in head


def find_real_codex() -> Path | None:
    """The codex binary to exec -- i.e. `which codex` with our own wrapper skipped."""
    from detect import codex_binary
    return codex_binary(exclude={wrapper_path()})


def install_wrapper(real_codex: Path, *, ttl: int = REFRESH_TTL_SECONDS) -> Path:
    """Write the `codex` wrapper. Returns its path."""
    from detect import python_binary
    target = wrapper_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        _WRAPPER_SH.format(
            kind=WRAPPER_KIND,
            home=Path(__file__).resolve().parent.parent,
            python=python_binary(),
            real=str(real_codex),
            ttl=ttl,
        ),
        encoding="utf-8",
    )
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def uninstall_wrapper() -> bool:
    target = wrapper_path()
    if not wrapper_is_ours(target):
        return False
    target.unlink()
    return True


def path_has_wrapper_dir() -> bool:
    """True when the wrapper directory comes before whatever `codex` resolves to."""
    d = str(wrapper_dir())
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        if os.path.realpath(entry) == os.path.realpath(d):
            return True
    return False


# --------------------------------------------------------------------------- #
# model switching helpers
# --------------------------------------------------------------------------- #

def write_switch_helpers(toolkit_dir: Path) -> list[Path]:
    """`codexcli/bin/codex-model` -- switch models without remembering flags.

    Runs the picker against the live gateway list and rewrites `config.toml`,
    which is the same thing `--switch-model` does; the point is only that the
    user does not have to recall a flag name months later. The interpreter is
    the one running this script rather than a bare `python3`, because on a box
    where the toolkit was set up from a venv, `python3` is not that venv.
    """
    from detect import python_binary

    script = toolkit_dir / "private_api.py"
    sh = toolkit_dir / "bin" / "codex-model"
    sh.parent.mkdir(parents=True, exist_ok=True)
    sh.write_text(
        "#!/usr/bin/env sh\n"
        "# Switch the Codex model by picking from the private gateway's live model list.\n"
        "# Written by codexcli/private_api.py; safe to delete.\n"
        f'exec "{python_binary()}" "{script}" --switch-model "$@"\n',
        encoding="utf-8",
    )
    sh.chmod(sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return [sh]


def clean_model_id(model_id: str) -> str:
    """Strip a `<provider>/<model>` prefix a user may have copied from elsewhere.

    LiteLLM routes on the bare name; `openai/gpt-5.5` is a LiteLLM-internal
    spelling that the gateway would reject as a model id.
    """
    return re.sub(r"^[a-z0-9_.-]+/", "", model_id.strip())
