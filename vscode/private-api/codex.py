r"""Point Codex (VSCode extension + CLI) at a private gateway, and switch models.

Codex does not enumerate `${base_url}/models` the way Claude Code does, so a
private deployment has to be wired up in three parts:

  1. a `[model_providers.<id>]` table in `~/.codex/config.toml` naming the gateway,
     the env var holding its key, and the wire protocol;
  2. a `model` value naming which served model to use;
  3. a **model catalog** -- the JSON file `model_catalog_json` points at. Without
     it the model dropdown lists OpenAI's own lineup (GPT-5.6 Sol/Terra/Luna,
     GPT-5.5, ...) because Codex falls back to the catalogue it fetched from
     `chatgpt_base_url`. None of those exist on the gateway, so picking one fails
     on the first turn. Writing our own catalog *replaces* that list entirely.

`--switch-model` re-writes just (2) from a live `GET /v1/models`.

THE CATALOG FORMAT
------------------
`model_catalog_json` is a path to `{"models": [ <entry>, ... ]}`. serde rejects
the whole file unless every entry carries these keys, which the binary reports one
at a time as `missing field \`x\``:

    slug, display_name, supported_reasoning_levels, shell_type, visibility,
    supported_in_api, priority, support_verbosity, truncation_policy,
    experimental_supported_tools

plus `base_instructions` (or `model_messages.instructions_template`) -- and an
empty `models` array is rejected with "must contain at least one model". The
remaining keys of Codex's own catalogue entries are optional; we set the handful
that change behaviour and leave the rest to their defaults.

Evidence this catalogue is authoritative rather than additive: with a catalog that
lists only gateway models, `-m gpt-5.6-sol` prints "Model metadata for
`gpt-5.6-sol` not found. Defaulting to fallback metadata" while a gateway id runs
clean.

WIRE API
--------
`wire_api = "chat"` was removed in Codex 0.150 ("`wire_api = "chat"` is no longer
supported. How to fix: set `wire_api = "responses"`"). The gateway must therefore
serve `POST /v1/responses`, which LiteLLM does. `probe_wire_api()` verifies that
before writing the config rather than letting it fail later inside the editor.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

import modalities
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
    -- no env var and therefore no editor restart, at the cost of the key sitting
    in a plaintext file. Otherwise Codex reads `env_key` from the environment.
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
    doc.set_table(provider_table(pid), table)
    changed["provider_table"] = provider_table(pid)

    if profile:
        # A profile keeps the user's existing OpenAI/ChatGPT setup intact; the
        # VSCode extension uses the root config, so this is for CLI use.
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
    is that the Reasoning submenu either vanishes entirely or every model starts
    at the same effort regardless of what its catalog entry says. Deleting the
    line lets each model use the level its entry declares, which is the only way
    the per-model reasoning configuration actually takes effect.

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
    removed = []
    for k in OPENAI_ONLY_ROOT_KEYS:
        if doc.get(k) is None:
            continue
        for i, line in enumerate(doc.lines):
            if doc.owner[i] is None and doc._key_of(line) == k:
                del doc.lines[i]
                doc._reparse()
                removed.append(k)
                break
    if removed:
        doc.save()
    return removed


def restore_from_backup(path: Path) -> bool:
    """Put the pristine copy back and drop the backup -- see claude.restore_from_backup."""
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

def set_env_var(name: str, value: str) -> str:
    """Persist an environment variable for the current user. Returns a note.

    On Windows `setx` writes to the user's registry environment; every process
    started AFTER it -- including VSCode -- inherits it. A running editor will not,
    which is the single most common reason a correct config appears not to work.
    """
    if os.name == "nt":
        try:
            subprocess.run(["setx", name, value], check=True, capture_output=True, timeout=30)
            return f"setx {name} (restart VSCode for it to take effect)"
        except Exception as e:  # noqa: BLE001
            return f"FAILED to setx {name}: {e}"

    shell = os.environ.get("SHELL", "")
    rc = Path.home() / (".zshrc" if "zsh" in shell else ".bashrc")
    marker = f"export {name}="
    existing = rc.read_text(encoding="utf-8") if rc.exists() else ""
    if marker in existing:
        return f"{name} already exported in {rc} (edit it there to rotate)"
    with rc.open("a", encoding="utf-8") as fh:
        fh.write(f'\n# added by private-api: Codex gateway key\nexport {name}="{value}"\n')
    return f'appended export {name}=... to {rc} (open a new shell)'


def find_codex_binary() -> Path | None:
    from detect import codex_binary
    return codex_binary()


def run_codex_doctor() -> str:
    """`codex doctor` gives a second opinion on the finished config."""
    exe = find_codex_binary()
    if not exe:
        return "codex binary not found; skipping"
    try:
        out = subprocess.run([str(exe), "doctor"], capture_output=True, text=True, timeout=120)
        return (out.stdout or out.stderr).strip()
    except Exception as e:  # noqa: BLE001
        return f"codex doctor failed: {e}"


# --------------------------------------------------------------------------- #
# the model catalog -- what the model dropdown lists
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
DEFAULT_CONTEXT_WINDOW = 128_000


def catalog_path() -> Path:
    from detect import codex_home
    return codex_home() / CATALOG_FILENAME


def catalog_entry(model: "Model", priority: int) -> dict[str, object]:
    """One catalog entry for a gateway model.

    `slug` is what goes on the wire, so it is the gateway's model id verbatim --
    Codex sends it as `model` and LiteLLM routes on exactly that string.
    """
    raw = model.raw or {}
    ctx = raw.get("max_input_tokens") or DEFAULT_CONTEXT_WINDOW
    try:
        ctx = int(ctx)
    except (TypeError, ValueError):
        ctx = DEFAULT_CONTEXT_WINDOW

    levels, default_level = reasoning.levels_for(model.id, raw)
    if not levels:
        default_level = None

    return {
        "slug": model.id,
        "display_name": model.id,
        "description": f"Private gateway model · {ctx // 1000}K context",
        "priority": priority,
        # "list" is what puts an entry in the dropdown; Codex's own hidden
        # entries (gpt-reserve) use "hide".
        "visibility": "list",
        # Codex renders the thinking-strength submenu from these two fields, and
        # the selected value comes back on the Responses request as
        # `reasoning.effort`. Both are computed together -- see reasoning.py for
        # where the levels come from and why OpenAI's own models are exempt.
        "supported_reasoning_levels": levels,
        "default_reasoning_level": default_level,
        "shell_type": "unified_exec",
        "supported_in_api": True,
        "support_verbosity": False,
        "truncation_policy": dict(TRUNCATION_POLICY),
        "experimental_supported_tools": [],
        "base_instructions": BASE_INSTRUCTIONS,
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

    Image endpoints are left out for the same reason they are left out of the
    Claude Code picker: Codex would accept the slug and then fail on turn one.
    """
    entries = [catalog_entry(m, i) for i, m in enumerate(models) if m.is_chat_capable]
    return {"models": entries}


def write_catalog(path: Path, models: Iterable["Model"]) -> list[str]:
    """Write the catalog JSON. Returns the slugs it now lists."""
    catalog = build_catalog(models)
    entries = catalog["models"]
    if not entries:
        raise ValueError("no chat-capable gateway models to put in the catalog")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return [str(e["slug"]) for e in entries]


def saved_key(config: Path) -> str | None:
    """The key a previous run left behind, for a refresh that has no --api-key.

    Two places it can be, depending on how the run was invoked: inline in the
    provider table as `experimental_bearer_token` (--inline-key), or in the
    environment variable that table's `env_key` names. Same secret, same gateway,
    written by this tool -- reading it back is not a new exposure, and it is never
    printed. Returns None when the provider table is not one of ours.
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
# model switching helpers
# --------------------------------------------------------------------------- #

def write_switch_helpers(toolkit_dir: Path) -> list[Path]:
    """Small wrappers so models can be switched later without remembering flags."""
    script = toolkit_dir / "private_api.py"
    out: list[Path] = []

    sh = toolkit_dir / "codex-model.sh"
    sh.write_text(
        "#!/usr/bin/env sh\n"
        "# Switch the Codex model by picking from the private gateway's live model list.\n"
        f'exec python3 "{script}" codex --switch-model "$@"\n',
        encoding="utf-8",
    )
    out.append(sh)

    if os.name == "nt":
        bat = toolkit_dir / "codex-model.bat"
        bat.write_text(
            "@echo off\r\n"
            "rem Switch the Codex model by picking from the private gateway's live model list.\r\n"
            f'python "{script}" codex --switch-model %*\r\n'
            "pause\r\n",
            encoding="utf-8",
        )
        out.append(bat)
    return out
