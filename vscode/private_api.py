#!/usr/bin/env python3
"""Point the Claude Code and Codex VSCode extensions at a private API gateway.

    python private_api.py --target claude --api-base http://10.0.0.5:4000 --api-key sk-xxx
    python private_api.py --target codex  --api-base http://10.0.0.5:4000 --api-key sk-xxx
    python private_api.py --target both   --api-base http://10.0.0.5:4000 --api-key sk-xxx
    python private_api.py --detect          # just show where the config files are
    python private_api.py --status          # what is configured right now
    python private_api.py --target codex --switch-model
    python private_api.py --target codex --refresh-models   # regen the model catalog
    python private_api.py --fix-login                       # stop the Codex login prompt
    python private_api.py --configure-reasoning --reasoning-model <id> \
        --reasoning-levels low,high,max --reasoning-default high
    python private_api.py --emit-gateway-config             # print the gateway fix
    python private_api.py --apply-gateway-config            # ...and send it
    python private_api.py --restore --target both

Run with `--target` omitted on a terminal and it will walk you through it.

WHAT IT TOUCHES
---------------
Claude Code:
  * `<editor>/User/settings.json`  -> `claudeCode.environmentVariables`
  * `~/.claude/settings.json`      -> `env`, and `modelPicker`, which replaces the
                                      built-in /model lineup (Opus/Sonnet/Haiku,
                                      not served by a private gateway) with the
                                      gateway's own models
  * the bundled `claude.exe`       -> the hardcoded `/(claude|anthropic)/i` model
                                      filter (see claude_patch.py). The curated
                                      `modelPicker` does not need it; it is kept
                                      for `--keep-builtin-models` and as a
                                      fallback if the setting is ever removed.

Codex:
  * `~/.codex/config.toml`         -> `[model_providers.private]` + `model`
                                      + `model_catalog_json`
  * `~/.codex/gateway-models.json` -> the generated model catalog. Codex's model
                                      dropdown otherwise lists OpenAI's own lineup
                                      (GPT-5.6 Sol/Terra/Luna, GPT-5.5, ...), none
                                      of which the gateway serves; a catalog
                                      REPLACES that list with the gateway's own
  * `~/.codex/auth.json`           -> only with `--fix-login`. Codex decides it is
                                      signed in by looking for this file, whatever
                                      `model_provider` says, so without it a private
                                      gateway still gets a login prompt it cannot
                                      complete. Holds the GATEWAY key, not an OpenAI
                                      one -- see codex_auth.py
  * `~/.codex/private-reasoning.json` -> per-model thinking levels, written by
                                      `--configure-reasoning`. OpenAI's own models
                                      are exempt and keep Codex's levels
  * `~/.codex/private-modalities.json` -> per-model input types (text/image),
                                      written by `--configure-modalities`. Only
                                      created when you override something; the
                                      default comes from the measured table in
                                      modalities.py, so an absent file is normal
  * the `PRIVATE_API_KEY` user environment variable (or an inline key)

The gateway itself:
  * `litellm_params.allowed_openai_params` and `litellm_params.additional_drop_params`
    on the private models -> only with `--apply-gateway-config`. Codex always sends
    `reasoning.effort` (which LiteLLM maps to `reasoning_effort` and then refuses
    for `custom_openai` providers -> HTTP 400) and `client_metadata` (which LiteLLM
    forwards into the OpenAI SDK, which has no such kwarg -> HTTP 500). EVERY Codex
    turn against a private model fails until both are set. The default
    `--emit-gateway-config` only prints the commands.

Every file is backed up to `<file>.bak` before it is written.

LAYOUT
------
This is the entry point and lives at the top of `vscode/`. The modules it drives
are in `vscode/private-api/`; nothing else in that folder needs to be invoked
directly. `vscode/README.md` is the full write-up.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

# This file is the entry point that sits at the top of vscode/; the modules it
# drives live in ./private-api/.
HERE = Path(__file__).resolve().parent
LIB = HERE / "private-api"
sys.path.insert(0, str(LIB))

import claude as claude_mod
import claude_patch
import codex as codex_mod
import codex_auth
import detect
import gateway
import litellm_admin
import modalities
import reasoning

# --------------------------------------------------------------------------- #

EXAMPLES = """\
  --api-base examples
      http://10.18.219.156:4000          LiteLLM gateway on the LAN
      http://127.0.0.1:4000              same box, docker-compose default
      https://llm.corp.example.com       behind an ingress (a trailing /v1 is OK)

  --api-key examples
      sk-xxxxxxxxxxxxxxxxxxxxxxxx         LiteLLM master key / virtual key
      <your gateway's sk-... token>

  full invocation
      python private_api.py --target both \\
          --api-base http://10.18.219.156:4000 \\
          --api-key sk-XXXXXXXX
"""


def self_cmd() -> str:
    """How to re-invoke this script: relative to cwd when that is meaningful."""
    me = Path(__file__).resolve()
    try:
        return str(me.relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(me)


def out(msg: str = "") -> None:
    print(msg, flush=True)


def rule(title: str = "") -> None:
    out()
    out(f"--- {title} " + "-" * max(0, 66 - len(title)))


# --------------------------------------------------------------------------- #
# interaction
# --------------------------------------------------------------------------- #

def is_tty() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:  # noqa: BLE001
        return False


def ask(prompt: str, default: str | None = None, secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            if secret:
                val = getpass.getpass(f"{prompt}{suffix}: ")
            else:
                val = input(f"{prompt}{suffix}: ")
        except (EOFError, KeyboardInterrupt):
            out()
            raise SystemExit("aborted")
        val = val.strip() or (default or "")
        if val:
            return val
        out("  (required -- press Ctrl+C to abort)")


def confirm(prompt: str, default: bool = True) -> bool:
    if not is_tty():
        return default
    d = "Y/n" if default else "y/N"
    try:
        ans = input(f"{prompt} [{d}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        out()
        raise SystemExit("aborted")
    if not ans:
        return default
    return ans in ("y", "yes")


def pick_model(models: list[gateway.Model], title: str = "pick a model") -> str:
    """Numbered picker over the live gateway list. Returns the chosen id."""
    chat = [m for m in models if m.is_chat_capable]
    other = [m for m in models if not m.is_chat_capable]

    out()
    out(f"{title} (from the gateway, {len(models)} served):")
    for i, m in enumerate(chat, 1):
        out(f"  {i:>3}. {m.id}")
    for i, m in enumerate(other, len(chat) + 1):
        out(f"  {i:>3}. {m.id}   <- not a chat model, will not work")

    # Deliberately no isatty() gate: piped input (`printf '3\n' | ...`) is a
    # legitimate way to script this, and `ask()` turns EOF into a clear abort.
    ordered = chat + other
    while True:
        raw = ask("number, or a substring to filter, or the exact model id").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(ordered):
            return ordered[int(raw) - 1].id
        matches = [m for m in models if raw.lower() in m.id.lower()]
        if len(matches) == 1:
            return matches[0].id
        if len(matches) > 1:
            out("  ambiguous:")
            for m in matches:
                out(f"    {m.id}")
            continue
        if any(m.id == raw for m in models):
            return raw
        out("  no match, try again")


# --------------------------------------------------------------------------- #
# shared setup
# --------------------------------------------------------------------------- #

def resolve_credentials(args, saved_base: str | None,
                        saved_key: str | None = None) -> tuple[str, str]:
    """Fill in --api-base / --api-key, prompting or showing examples as needed."""
    base, key = args.api_base, args.api_key

    # The gateway in the settings file is one this tool wrote on an earlier run,
    # so reusing it is the obvious default -- asking every time is just noise, and
    # it made `--refresh-models` unusable from anything but a terminal.
    if not base and saved_base:
        base = saved_base
        out(f"  found an existing gateway in your settings: {saved_base}")

    # The key for that gateway is usually already in the settings file this tool
    # wrote. Reusing it is what makes `--refresh-models` a one-liner -- but only
    # when the base is the same one, or it would silently point an old key at a
    # different gateway.
    if (not key and saved_key and saved_base and base
            and gateway.normalize_base(base, keep_v1=False)
            == gateway.normalize_base(saved_base, keep_v1=False)):
        key = saved_key
        out(f"  reusing the API key already in your settings ({_mask(key)})")

    if not base:
        out()
        out("No --api-base given.")
        out(EXAMPLES)
        if not is_tty() or args.yes:
            raise SystemExit("--api-base is required (see the examples above)")
        base = ask("API base URL", default=saved_base)

    if not key:
        out()
        out("No --api-key given.")
        out(EXAMPLES)
        if not is_tty() or args.yes:
            raise SystemExit("--api-key is required (see the examples above)")
        key = ask("API key", secret=True)

    return gateway.normalize_base(base, keep_v1=False), key


def fetch_models(base: str, key: str, *, required: bool) -> list[gateway.Model]:
    try:
        models = gateway.list_models(base, key)
    except gateway.GatewayError as e:
        if required:
            raise SystemExit(f"could not list models: {e}")
        out(f"  ! could not list models ({e})")
        out("    continuing -- you can point the client at the gateway anyway.")
        return []
    out(f"  gateway OK: {len(models)} models served")
    return models


# --------------------------------------------------------------------------- #
# claude
# --------------------------------------------------------------------------- #

def run_claude(args, rep: detect.Report) -> int:
    # Undoing does not need a gateway address or a key -- ask for neither, and let
    # restore_claude() own the section header.
    if args.restore:
        return restore_claude(rep)

    rule("Claude Code")

    paths = [*rep.claude_editor_settings, *rep.claude_cli_settings]
    base, key = resolve_credentials(args, claude_mod.existing_base_url(paths),
                                    claude_mod.existing_api_key(paths))
    models = fetch_models(base, key, required=False)

    if models:
        visible, hidden = claude_mod.classify_models([m.id for m in models])
        out()
        out(f"  models Claude Code can see WITHOUT the binary patch: {len(visible)}")
        for m in visible:
            out(f"      {m}")
        if hidden:
            out(f"  models it silently drops: {len(hidden)}")
            for m in hidden:
                out(f"      {m}")
            out("  -> the binary patch below is what recovers these.")

    model = args.model
    if not model and models and is_tty() and not args.yes and not args.no_prompt_model:
        if confirm("  also pin a default model now?", False):
            model = pick_model(models, "pick the default Claude Code model")

    if not rep.claude_editor_settings and not rep.claude_cli_settings:
        out("  ! no Claude Code settings file found; is the extension installed?")
        raise SystemExit(1)

    # `claude_cli_settings()` lists only files that exist, which on a machine that
    # has never run Claude Code is none of them -- including the user-level file
    # that both the `env` block and the /model picker list have to go into. Write
    # it regardless; save() creates ~/.claude/ if needed.
    cli_paths = list(rep.claude_cli_settings)
    if user_claude_settings() not in cli_paths:
        cli_paths.insert(0, user_claude_settings())

    # Before anything is written -- the same file is edited twice below (env, then
    # the picker), and .bak has to mean "before this run", not "before this save".
    snapshot([*rep.claude_editor_settings, *cli_paths])

    written: list[Path] = []

    for path in rep.claude_editor_settings:
        before, after = claude_mod.apply_editor_settings(path, base, key, model)
        written.append(path)
        out()
        out(f"  wrote {path}")
        for k in claude_mod.MANAGED:
            if k in after:
                mark = " " if before.get(k) == after[k] else "*"
                shown = after[k] if "TOKEN" not in k and "KEY" not in k else _mask(after[k])
                out(f"    {mark} {k} = {shown}")
        out(f"    (also claudeCode.disableLoginPrompt = true)")

    for path in cli_paths:
        before, after = claude_mod.apply_cli_settings(path, base, key, model)
        written.append(path)
        out()
        out(f"  wrote {path}  (env block, for `claude` in a terminal)")

    if models:
        write_model_picker(args, models)

    warn_workspace_override(rep)

    if not args.no_patch:
        patch_claude_binary(args, rep)

    out()
    out("  next: reopen VSCode, then /model in Claude Code should list the gateway's models.")
    return 0


def _mask(secret: str) -> str:
    return secret[:6] + "..." + secret[-4:] if len(secret) > 12 else "***"


def snapshot(paths: list[Path]) -> None:
    """Record the pre-run contents of every file this run is about to touch.

    `JsoncFile.save` snapshots only when it is the first writer, which is wrong
    for a file edited more than once in a run -- on a machine with no
    `~/.claude/settings.json`, the env block is written first and the /model
    picker second, so the lazy snapshot would capture our own first output and
    `--restore` would leave the env block behind.

    A file that does not exist yet gets an empty `.bak`, which is the record of
    "absent"; restoring it deletes the file again. (The one ambiguity is a file
    that existed but was empty -- restoring that deletes it rather than
    recreating an empty file, which no editor or CLI actually leaves behind.)
    """
    for p in paths:
        bak = p.with_suffix(p.suffix + ".bak")
        if bak.exists():
            continue  # never overwrite: .bak must stay the original
        p.parent.mkdir(parents=True, exist_ok=True)  # e.g. no ~/.claude yet
        if p.exists():
            bak.write_bytes(p.read_bytes())
        else:
            bak.write_bytes(b"")


def user_claude_settings() -> Path:
    """The user-level `~/.claude/settings.json`, never a project checkout.

    `modelPicker` is honoured from managed settings, --settings/SDK and *user*
    settings only; a project `.claude/settings.json` is ignored for this key, so
    writing there would look like it worked and quietly do nothing. Created on
    first use if the machine has never run Claude Code.
    """
    return detect.claude_user_settings()


def refresh_models(args, rep: detect.Report) -> int:
    """Re-fetch the gateway list and rewrite only the /model picker rows.

    Separate from a full run because that is the common follow-up: the gateway
    gained a model, and the user wants it in the picker without touching env
    vars or re-patching anything.
    """
    rule("refresh /model picker")
    paths = [*rep.claude_editor_settings, *rep.claude_cli_settings]
    base, key = resolve_credentials(args, claude_mod.existing_base_url(paths),
                                    claude_mod.existing_api_key(paths))
    models = fetch_models(base, key, required=True)
    snapshot([user_claude_settings()])
    write_model_picker(args, models)
    report_and_clear_cache()
    out()
    out("  next: reload the VSCode window for the picker to pick this up.")
    return 0


def write_model_picker(args, models: list[gateway.Model]) -> None:
    """Replace the built-in /model lineup with the gateway's live model list."""
    out()
    path = user_claude_settings()
    rows, _ = claude_mod.apply_model_picker(
        path, models, replace_builtin=not args.keep_builtin_models)

    out(f"  wrote {path}  (modelPicker)")
    if args.keep_builtin_models:
        out(f"    + {len(rows)} gateway row(s) appended after the built-in lineup")
        return

    out(f"    {len(rows)} row(s); Claude Code's built-in lineup is hidden:")
    for r in rows:
        out(f"      {r['model']:32} {r['description']}")

    hidden = [m.id for m in models if not m.is_chat_capable]
    if hidden:
        out(f"    ({len(hidden)} non-chat endpoint(s) omitted: {', '.join(hidden)})")
    out("    the Default row remains -- it resolves to `model` in this file.")


def warn_workspace_override(rep: detect.Report) -> None:
    for ws in rep.claude_workspace_settings:
        try:
            from jsonc import read_jsonc
            data = read_jsonc(ws)
        except Exception:  # noqa: BLE001
            continue
        if data.get("claudeCode.environmentVariables"):
            out()
            out(f"  ! {ws}")
            out("    also defines claudeCode.environmentVariables. Workspace settings")
            out("    REPLACE the user-level array, so your gateway config there wins.")
            out("    Remove it, or re-run this script against that file.")


def patch_claude_binary(args, rep: detect.Report) -> None:
    rule("claude.exe model filter")
    target = Path(args.claude_binary) if args.claude_binary else (
        rep.claude_binaries[0] if rep.claude_binaries else None
    )
    if not target or not target.exists():
        out("  ! no bundled claude.exe found -- skipping the filter patch.")
        out("    (pass --claude-binary <path> if you know where it lives)")
        return

    state = claude_patch.status(target)
    out(f"  binary: {target}")
    out(f"  state : {state}")

    if state == "PATCHED":
        out("  already patched, binary up to date.")
        report_and_clear_cache()
        return
    if not state.startswith("ORIGINAL"):
        out("  ! unexpected binary contents; refusing to patch. Reinstall the extension.")
        return

    patched, hits, changed = claude_patch.build_patched(target)
    out(f"  built : {patched.name}  ({hits} filters neutralised, {changed} bytes changed)")
    swap, restore_bat = claude_patch.write_swap_scripts(target)

    alive = claude_patch.running_processes()
    if alive:
        out()
        out(f"  ! still running: {', '.join(alive)}")
        out("    Quit VSCode COMPLETELY, then either:")
        out(f'      double-click {swap}')
        out(f"      or re-run: python {self_cmd()} --target claude --patch-only")
        return

    claude_patch.install(target, patched)
    out(f"  installed -> {claude_patch.status(target)}")
    out(f"  backup: {target.with_suffix(target.suffix + '.bak')}")
    report_and_clear_cache()


def report_and_clear_cache(force: bool = False) -> None:
    """Drop the gateway model-list cache, which survives the binary patch.

    Claude Code caches what discovery returned -- AFTER the claude/anthropic
    filter has already run. A single unpatched fetch therefore leaves a poisoned
    cache behind, and the /model picker keeps reading it no matter how many times
    the binary is re-patched. Reporting the cached contents by name makes the
    symptom ("only one model, and it is a stale name") self-explanatory.
    """
    path = claude_mod.gateway_cache_path()
    cache = claude_mod.read_gateway_cache(path)
    if cache is None:
        if force:
            out(f"  model cache: none at {path}")
        return

    models = [str(m.get("id", "")) for m in (cache.get("models") or [])]
    out()
    out(f"  model cache: {path}")
    out(f"      {len(models)} cached model(s): {', '.join(models) or '(empty)'}")
    if not models and not force:
        return

    if claude_mod.cache_looks_filtered(cache):
        out("      ! every cached name contains claude/anthropic -- this is a list")
        out("        captured BEFORE the patch, and the picker reads it in preference")
        out("        to re-fetching. Clearing it forces a fresh discovery.")

    out(f"      cached from: {cache.get('baseUrl', '?')}")
    bak = claude_mod.clear_gateway_cache(path)
    out(f"      cleared (backup: {bak.name if bak else 'none'}) -- restart VSCode to refetch")


def restore_claude(rep: detect.Report) -> int:
    rule("restore Claude Code")
    # The user-level file is listed even when it is not on disk any more -- its
    # backup is the zero-byte "did not exist" record and still needs acting on.
    settings_files = [*rep.claude_editor_settings, *rep.claude_cli_settings]
    if user_claude_settings() not in settings_files:
        settings_files.append(user_claude_settings())
    for path in settings_files:
        if claude_mod.restore_from_backup(path):
            out(f"  restored {path} from {path.name}.bak")
        else:
            out(f"  no backup for {path} -- leaving as is")
    for b in rep.claude_binaries:
        if claude_patch.status(b) == "PATCHED" and not claude_patch.running():
            claude_patch.restore(b)
            out(f"  restored original {b.name}")
        elif claude_patch.status(b) == "PATCHED":
            out(f"  ! {b.name} is patched but VSCode is running; close it and re-run")
    # A cache written by the patched binary would keep showing models the original
    # binary is once again filtering out.
    if claude_mod.clear_gateway_cache():
        out(f"  cleared gateway model cache {claude_mod.gateway_cache_path().name}")
    # Restoring from .bak already removed the modelPicker block, but only if a
    # backup was there to restore from -- belt and braces for the other case.
    user_settings = user_claude_settings()
    if claude_mod.clear_model_picker(user_settings):
        out(f"  dropped modelPicker from {user_settings}")
    return 0


# --------------------------------------------------------------------------- #
# codex
# --------------------------------------------------------------------------- #

def run_codex(args, rep: detect.Report) -> int:
    cfg = rep.codex_config or detect.codex_config()
    if args.restore:
        return restore_codex(cfg)

    rule("Codex")

    # Reuse whatever a previous run left in config.toml before falling back to
    # Claude Code's settings -- on a machine set up for both, they share a gateway.
    claude_paths = [*rep.claude_editor_settings, *rep.claude_cli_settings]
    saved = _codex_saved_base(cfg) or claude_mod.existing_base_url(claude_paths)
    base, key = resolve_credentials(
        args, saved,
        codex_mod.saved_key(cfg) or claude_mod.existing_api_key(claude_paths))
    base_v1 = gateway.normalize_base(base, keep_v1=True)

    models = fetch_models(base, key, required=args.list_models or args.switch_model)

    if args.list_models:
        for m in models:
            flag = "" if m.is_chat_capable else "   <- not chat-capable"
            out(f"    {m.id}{flag}")
        if not args.switch_model:
            return 0

    current = _codex_saved_model(cfg)
    if current:
        out(f"  current model in config.toml: {current}")

    model = args.model
    if not model and models:
        if args.switch_model or (is_tty() and not args.yes):
            model = pick_model(models, "pick the model Codex should use")
    if not model and not args.model and not current:
        out("  ! no model chosen; pass --model <id> or run --switch-model")
        return 1

    # Codex 0.150 only speaks /responses. Verify the gateway serves it before
    # writing a config that would fail on the first prompt.
    if model and not args.skip_probe:
        out(f"  probing /v1/responses with {model} ...")
        try:
            result = gateway.probe_wire_api(base_v1, key, model)
        except gateway.GatewayError as e:
            out(f"  ! probe failed: {e}")
        else:
            if result.get("responses"):
                out("  /v1/responses  OK")
            else:
                out("  ! /v1/responses NOT served by this gateway")
                if result.get("chat"):
                    out("    /v1/chat/completions works, but Codex 0.150 dropped")
                    out('    wire_api = "chat" -- Codex cannot drive this gateway')
                    out("    until it proxies /responses (LiteLLM does).")
                return 1

    inline_key = key if args.inline_key else None
    snapshot([cfg])
    summary = codex_mod.apply_config(
        cfg, base_v1, model,
        pid=args.provider_id, env_key=args.env_key, inline_key=inline_key,
        profile=args.profile,
    )

    out()
    out(f"  wrote {cfg}")
    out(f"    [{summary['provider_table']}]")
    out(f"      base_url = {base_v1}")
    out(f'      wire_api = "{codex_mod.WIRE_API}"')
    if inline_key:
        out(f"      experimental_bearer_token = {_mask(key)}   (inline, no restart needed)")
    else:
        out(f"      env_key  = {codex_mod.env_key_name(args.provider_id)}")
    if summary.get("profile"):
        out(f"    [profiles.{summary['profile']}]  -> use with `codex --profile {summary['profile']}`")
    elif model:
        out(f'    model = "{model}"')
        out(f'    model_provider = "{args.provider_id}"')

    if not inline_key:
        name = codex_mod.env_key_name(args.provider_id)
        note = codex_mod.set_env_var(name, key)
        out()
        out(f"  api key: {note}")

    if models:
        write_codex_catalog(cfg, models)
        strip_blocking_keys(cfg)
        warn_gateway_params(base, key)

    helpers = codex_mod.write_switch_helpers(HERE)
    out()
    out("  switch models later with:")
    for h in helpers:
        out(f"      {h}")
    return 0


def catalog_target(cfg: Path) -> Path:
    """Where the catalog JSON goes.

    A catalog a previous run of this tool wrote is reused so the file stays where
    the user last saw it; otherwise we fall back to `~/.codex/gateway-models.json`.
    A `model_catalog_json` pointing anywhere ELSE is left alone -- that one is the
    user's, and overwriting it would be a surprise.
    """
    if codex_mod.catalog_is_ours(cfg):
        previous = codex_mod.existing_catalog_path(cfg)
        if previous:
            return Path(previous)
    return codex_mod.catalog_path()


def write_codex_catalog(cfg: Path, models: list[gateway.Model]) -> list[str]:
    """Generate the catalog JSON and point config.toml at it."""
    out()
    path = catalog_target(cfg)
    try:
        slugs = codex_mod.write_catalog(path, models)
    except ValueError as e:
        out(f"  ! {e}; leaving Codex's own model list in place")
        return []

    codex_mod.apply_catalog(cfg, path)

    out(f"  wrote {path}  (model catalog)")
    out(f"    {len(slugs)} model(s) in Codex's dropdown, and nothing else:")
    for s in slugs:
        out(f"      {s}")
    hidden = [m.id for m in models if not m.is_chat_capable]
    if hidden:
        out(f"    ({len(hidden)} non-chat endpoint(s) omitted: {', '.join(hidden)})")
    out(f"    {cfg.name}: {codex_mod.CATALOG_KEY} = {path}")
    return slugs


def strip_blocking_keys(cfg: Path) -> None:
    """Remove root-level keys that stop the catalog's reasoning from working.

    Two root-level keys in config.toml silently override the per-model reasoning
    levels we just wrote into the catalog:

      * `model_reasoning_effort` -- a global default that outranks every model's
        `default_reasoning_level`.  When it is set (often to ``"none"`` by a
        previous OpenAI-only config or a Codex UI toggle), the Reasoning submenu
        disappears for *every* model, not just the one the user intended.
      * `service_tier` -- an OpenAI-only concept that a private gateway may
        reject, and that has nothing to do with reasoning but was previously
        only warned about.

    Deleting them here means the catalog we just wrote actually takes effect,
    which is the whole point of refreshing it.  Both are re-added by Codex's own
    UI if the user genuinely wants them, so this is not destructive.
    """
    removed_reasoning = codex_mod.strip_reasoning_override(cfg)
    if removed_reasoning is not None:
        out()
        out(f'  removed root-level model_reasoning_effort = "{removed_reasoning}"')
        out("    it was masking every model's default_reasoning_level in the catalog,")
        out("    so the Reasoning submenu was hidden or stuck at one level for all models.")
        out("    Each model now uses the level its catalog entry declares.")

    removed_openai = codex_mod.strip_openai_only_keys(cfg)
    for k in removed_openai:
        out()
        out(f"  removed root-level {k} (OpenAI-only; a private gateway may reject it)")


def refresh_catalog(args, rep: detect.Report) -> int:
    """Codex's half of `--refresh-models`: rewrite only the catalog JSON."""
    rule("refresh Codex model catalog")
    cfg = rep.codex_config or detect.codex_config()
    claude_paths = [*rep.claude_editor_settings, *rep.claude_cli_settings]
    saved = _codex_saved_base(cfg) or claude_mod.existing_base_url(claude_paths)
    base, key = resolve_credentials(
        args, saved,
        codex_mod.saved_key(cfg) or claude_mod.existing_api_key(claude_paths))
    models = fetch_models(base, key, required=True)

    if not cfg.exists():
        out(f"  ! no {cfg} -- run --target codex first to create it")
        return 1
    snapshot([cfg])
    write_codex_catalog(cfg, models)
    strip_blocking_keys(cfg)
    out()
    out("  next: reload the VSCode window; the model dropdown picks this up.")
    return 0


def _codex_saved_base(cfg: Path) -> str | None:
    if not cfg.exists():
        return None
    from tomlpatch import TomlFile
    t = TomlFile(cfg)
    for header in ("private", "private-gateway"):
        v = t.get("base_url", table=codex_mod.provider_table(header))
        if v:
            return v.strip("'\"")
    return None


def _codex_saved_model(cfg: Path) -> str | None:
    if not cfg.exists():
        return None
    from tomlpatch import TomlFile
    v = TomlFile(cfg).get("model")
    return v.strip("'\"") if v else None


def restore_codex(cfg: Path) -> int:
    rule("restore Codex")
    # Read the catalog facts BEFORE restoring: restore_from_backup rewrites
    # config.toml, so afterwards the key is usually gone.
    owns_catalog = codex_mod.catalog_is_ours(cfg)
    catalog_file = codex_mod.existing_catalog_path(cfg)

    if codex_mod.restore_from_backup(cfg):
        out(f"  restored {cfg} from {cfg.name}.bak")
    else:
        out(f"  no backup for {cfg} -- leaving as is")

    # Redundant when the .bak predates the catalog, but it is the only thing that
    # cleans up a run that had no backup to restore from.
    if cfg.exists() and codex_mod.clear_catalog(cfg):
        out(f"  dropped {codex_mod.CATALOG_KEY} from {cfg}")
    if owns_catalog and catalog_file and codex_mod.remove_catalog_file(Path(catalog_file)):
        out(f"  removed generated catalog {catalog_file}")
    return 0


# --------------------------------------------------------------------------- #
# gateway-side model parameters  (reasoning_effort, client_metadata)
# --------------------------------------------------------------------------- #

def _gateway_credentials(args, rep: detect.Report) -> tuple[str, str]:
    """Base + key for the admin API, reusing whatever a previous run stored.

    `/model/info` and `/model/update` are master-key endpoints, so this is the
    same credential the rest of the tool already persists.
    """
    cfg = rep.codex_config or detect.codex_config()
    claude_paths = [*rep.claude_editor_settings, *rep.claude_cli_settings]
    saved = _codex_saved_base(cfg) or claude_mod.existing_base_url(claude_paths)
    return resolve_credentials(
        args, saved,
        codex_mod.saved_key(cfg) or claude_mod.existing_api_key(claude_paths))


def read_gateway_models(args, rep: detect.Report) -> tuple[str, str, litellm_admin.Report]:
    base, key = _gateway_credentials(args, rep)
    endpoint = gateway.normalize_base(base, keep_v1=False) + litellm_admin.INFO_PATH
    out(f"  model definitions: {endpoint}")
    entries = litellm_admin.fetch_model_info(base, key)
    out(f"  gateway OK: {len(entries)} model definition(s)")
    return base, key, litellm_admin.build_report(entries)


def warn_gateway_params(base: str, key: str) -> None:
    """Non-fatal heads-up when the gateway would 400 every Codex turn.

    Worth doing immediately after writing the config: otherwise the failure
    surfaces inside the editor as an opaque error, and the fix is a different
    command that nothing has pointed at yet.
    """
    try:
        report = litellm_admin.build_report(litellm_admin.fetch_model_info(base, key))
    except gateway.GatewayError:
        return  # never fatal here; --status reports this properly
    todo = report.todo
    if not todo:
        return
    out()
    out(f"  ! {len(todo)} private model(s) lack gateway parameters Codex needs, so")
    out("    turns against them fail (HTTP 400 on reasoning, HTTP 500 on metadata):")
    for p in todo:
        out(f"      {p.model_name:28} {litellm_admin.describe(p)}")
    out(f"    fix: python {self_cmd()} --emit-gateway-config")


def render_report(rep: litellm_admin.Report) -> None:
    if rep.ok:
        out()
        out(f"  already accept Codex's Responses fields ({len(rep.ok)}):")
        for p in rep.ok:
            out(f"      {p.model_name}")
    if rep.skipped:
        out()
        out(f"  not applicable ({len(rep.skipped)}):")
        for p in rep.skipped:
            out(f"      {p.model_name:28} {p.reason}")
    if rep.todo:
        out()
        out(f"  NEED THE FIX ({len(rep.todo)}) -- Codex turns fail against these:")
        for p in rep.todo:
            out(f"      {p.model_name:28} {litellm_admin.describe(p)}")


def run_gateway_config(args, rep: detect.Report, *, apply: bool) -> int:
    rule("gateway model parameters")
    out("  Codex only speaks /v1/responses, and every turn carries two fields a")
    out("  custom_openai model definition rejects by default:")
    out(f"    reasoning.effort  -> {litellm_admin.REASONING_PARAM}, refused as an")
    out("                         unsupported parameter -> HTTP 400")
    out(f"    {litellm_admin.CLIENT_METADATA_PARAM}   -> forwarded into the OpenAI SDK,")
    out("                         which has no such kwarg   -> HTTP 500")
    out()
    out(f"  Both are answered with litellm_params: allowed_openai_params and")
    out(f"  {litellm_admin.DROP_PARAMS_KEY}. Codex's binary cannot send either, so")
    out("  it has to live on the gateway.")
    out()

    try:
        base, key, report = read_gateway_models(args, rep)
    except gateway.GatewayError as e:
        raise SystemExit(f"  ! {e}")

    render_report(report)

    if not report.todo:
        out()
        out("  nothing to do -- every private model already accepts Codex's fields.")
        return 0

    if not apply:
        out()
        out("  Not changing anything. Run these where you can reach the gateway:")
        out()
        out(litellm_admin.emit_commands(base, report.todo))
        out()
        out("  ...or re-run with --apply-gateway-config to send them from here.")
        out("  (POST /model/update REPLACES litellm_params, so each command above")
        out("   resends the model's full parameter set with the one key added.)")
        return 0

    out()
    out(f"  applying to {len(report.todo)} model(s) ...")
    failures = 0
    for p in report.todo:
        try:
            litellm_admin.apply_plan(base, key, p)
        except gateway.GatewayError as e:
            failures += 1
            out(f"      ! {p.model_name}: {e}")
        else:
            out(f"      ok {p.model_name}")
    out()
    if failures:
        out(f"  {failures} model(s) could NOT be updated; see above.")
        return 1
    out("  done. Verify with: --emit-gateway-config (it should report nothing to do).")
    return 0


def run_fix_login(args, rep: detect.Report) -> int:
    rule("Codex sign-in")
    state, detail = codex_auth.status()
    out(f"  before: {state}  ({detail})")

    if args.restore:
        if codex_auth.restore_from_backup():
            out(f"  restored {codex_auth.auth_path()} from its .bak")
        else:
            out("  no auth.json.bak to restore -- nothing changed")
        return 0

    if state in ("API_KEY",) and not args.force:
        out("  already signed in with an API key; nothing to do.")
        out("  (re-run with --force to rewrite it, e.g. after rotating the key)")
        return 0
    if state == "CHATGPT" and not args.force:
        out("  a real ChatGPT sign-in is already present; leaving it alone.")
        out("  (re-run with --force to replace it with the gateway key)")
        return 0

    base, key = _gateway_credentials(args, rep)

    # Writing a key that the gateway rejects would be worse than writing nothing:
    # Codex would look signed in and then fail every turn. Prove it works first.
    out()
    out(f"  checking the key against {base} ...")
    try:
        models = gateway.list_models(base, key)
    except gateway.GatewayError as e:
        raise SystemExit(
            f"  ! the key was refused, so auth.json was NOT written:\n      {e}"
        )
    out(f"  key OK: the gateway serves {len(models)} model(s)")

    path, backup = codex_auth.write_api_key_auth(key)
    state, detail = codex_auth.status()

    out()
    out(f"  wrote {path}")
    out(f"    OPENAI_API_KEY = {_mask(key)}   (the gateway key, not an OpenAI one)")
    if backup:
        out(f"    previous file kept at {backup}")
    out(f"  after: {state}  ({detail})")
    out()
    out("  Codex no longer needs a ChatGPT account: the private provider table")
    out("  authenticates with this same key. Reopen VSCode for it to take effect.")
    return 0


def run_configure_reasoning(args, rep: detect.Report) -> int:
    rule("per-model thinking levels")

    if args.reasoning_clear:
        store = reasoning.load()
        targets = [args.reasoning_clear] if args.reasoning_clear != "all" else list(store)
        if not targets:
            out("  no per-model overrides stored; nothing to clear.")
            return 0
        for m in targets:
            out(f"  {'cleared ' + m if reasoning.clear(m) else 'no override for ' + m}")
        return 0

    models = fetch_models(*_gateway_credentials(args, rep), required=True)

    model = args.reasoning_model
    if not model:
        if not is_tty():
            raise SystemExit("--reasoning-model is required (or run it on a terminal)")
        model = pick_model(models, "which model's thinking levels?")

    if model not in {m.id for m in models}:
        out(f"  ! the gateway does not serve {model!r}; listing what it does:")
        for m in models:
            out(f"      {m.id}")
        return 1

    raw = next((m.raw or {} for m in models if m.id == model), {})
    current_levels, current_default = reasoning.levels_for(model, raw)
    out()
    out(f"  {model}")
    if reasoning.is_openai_official(model):
        out("  this is an OpenAI model -- its levels come from Codex's own catalog")
        out("  and are not user-configurable. Showing them anyway:")
        for lv in current_levels:
            out(f"      {lv['effort']:12} {lv['description']}")
        return 0
    out(f"  now: {', '.join(lv['effort'] for lv in current_levels) or '(none)'}"
        f"  default={current_default or '-'}")

    if args.reasoning_levels is not None:
        wanted = [x.strip() for x in args.reasoning_levels.split(",") if x.strip()]
    else:
        out()
        out(f"  available efforts: {', '.join(reasoning.KNOWN_EFFORTS)}")
        out("  (comma-separated; empty means no Reasoning submenu for this model)")
        wanted = [x.strip() for x in
                  ask("levels", default=",".join(lv["effort"] for lv in current_levels)
                      or "low,high,max").split(",") if x.strip()]

    unknown = [x for x in wanted if x not in reasoning.KNOWN_EFFORTS]
    if unknown:
        out(f"  ! not valid Codex reasoning levels: {', '.join(unknown)}")
        out(f"    valid: {', '.join(reasoning.KNOWN_EFFORTS)}")
        return 1

    default = args.reasoning_default
    if wanted and not default:
        if is_tty() and not args.yes:
            default = ask("default level", default=current_default or wanted[0])
        else:
            default = wanted[0]

    try:
        entry = reasoning.configure(model, wanted, default)
    except ValueError as e:
        out(f"  ! {e}")
        return 1

    levels = entry.get("levels") or []
    out()
    out(f"  wrote {reasoning.store_path()}")
    if not levels:
        out(f"    {model}: no Reasoning submenu")
    else:
        out(f"    {model}: {', '.join(lv['effort'] for lv in levels)}"
            f"  default={entry.get('default')}")
    out()
    out("  next: --target codex --refresh-models to regenerate the catalog.")
    return 0


def run_configure_modalities(args, rep: detect.Report) -> int:
    rule("per-model input types (images)")

    if args.modalities_clear:
        store = modalities.load()
        targets = [args.modalities_clear] if args.modalities_clear != "all" else list(store)
        if not targets:
            out("  no per-model overrides stored; nothing to clear.")
            return 0
        for m in targets:
            out(f"  {'cleared ' + m if modalities.clear(m) else 'no override for ' + m}")
        out("  (cleared models fall back to the measured table, not to text-only)")
        return 0

    models = fetch_models(*_gateway_credentials(args, rep), required=True)

    model = args.modalities_model
    if not model:
        if not is_tty():
            raise SystemExit("--modalities-model is required (or run it on a terminal)")
        model = pick_model(models, "which model's input types?")

    if model not in {m.id for m in models}:
        out(f"  ! the gateway does not serve {model!r}; listing what it does:")
        for m in models:
            out(f"      {m.id}")
        return 1

    raw = next((m.raw or {} for m in models if m.id == model), {})
    current = modalities.modalities_for(model, raw)
    out()
    out(f"  {model}")
    if modalities.is_openai_official(model):
        out("  this is an OpenAI model -- its input types come from Codex's own")
        out("  catalog and are not user-configurable. Showing them anyway:")
        out(f"      {', '.join(current)}")
        return 0
    out(f"  now: {', '.join(current)}")

    if args.modalities is not None:
        wanted = args.modalities
    else:
        out()
        out(f"  available: {', '.join(modalities.KNOWN_MODALITIES)}")
        out("  add 'image' only if the model really reads images -- accept-the-")
        out("  request is not enough; verify with --probe-modalities")
        wanted = ask("input types", default=",".join(current))

    try:
        chosen = modalities.configure(model, wanted)
    except ValueError as e:
        out(f"  ! {e}")
        return 1

    out()
    out(f"  wrote {modalities.store_path()}")
    out(f"    {model}: {', '.join(chosen)}")
    out()
    out("  next: --target codex --refresh-models to regenerate the catalog,")
    out("        then reload the VSCode window for the attach button to appear.")
    return 0


# Vision inference is slower than text, and these backends queue.
PROBE_TIMEOUT = 180.0


def run_probe_modalities(args, rep: detect.Report) -> int:
    rule("measure which models can actually see images")
    out("  Sends a solid red and a solid blue image and asks each model for the")
    out("  colour. Accepting the request proves nothing -- a model can return 200")
    out("  and describe a colour that is not there -- so the test is whether it")
    out("  tells the two apart.")
    out()

    base, key = _gateway_credentials(args, rep)
    models = fetch_models(base, key, required=True)
    names = args.probe_models.split(",") if args.probe_models else None
    targets = [m.id for m in models
               if m.is_chat_capable and (not names or m.id in names)]
    if not targets:
        out("  ! no chat models to probe")
        return 1

    images = modalities.probe_images()
    url = gateway.normalize_base(base, keep_v1=True) + "/chat/completions"

    verdicts: dict[str, bool] = {}
    for name in targets:
        answers: dict[str, str] = {}
        failure = ""
        for colour, b64 in images.items():
            body = {"model": name, "max_tokens": 300, "messages": [{"role": "user",
                    "content": [
                        {"type": "text", "text": modalities.PROBE_QUESTION},
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/png;base64,{b64}"}}]}]}
            try:
                status, raw = gateway._request(url, key, method="POST", body=body,
                                               timeout=PROBE_TIMEOUT)
                payload = json.loads(raw.decode("utf-8", "replace"))
            except Exception as e:  # noqa: BLE001
                failure = f"{type(e).__name__}: {e}"
                break
            if status != 200:
                message = ""
                if isinstance(payload, dict):
                    message = str((payload.get("error") or {}).get("message") or "")
                # The upstream's own words are the useful part; the gateway wraps
                # them in a paragraph of LiteLLM context.
                failure = f"HTTP {status} {message.split('Fallbacks=')[0].strip()[:120]}"
                break
            message = payload["choices"][0]["message"]
            answers[colour] = (message.get("content") or "").strip()

        if failure:
            verdicts[name] = False
            out(f"  {name:26} FAILED   {failure}")
            continue

        can_see, why = modalities.judge(answers)
        verdicts[name] = can_see
        said = " / ".join(f"{c}={answers.get(c, '')[:24]!r}" for c in ("red", "blue"))
        out(f"  {name:26} {'IMAGE' if can_see else 'TEXT ':6} {said}")
        out(f"  {'':26} {why}")

    out()
    can = [n for n, v in verdicts.items() if v]
    cannot = [n for n, v in verdicts.items() if not v]
    out(f"  can see images ({len(can)}): {', '.join(can) or '(none)'}")
    out(f"  cannot ({len(cannot)}): {', '.join(cannot) or '(none)'}")
    out()
    out("  Enable per model with:")
    for n in can:
        out(f"      python {self_cmd()} --configure-modalities "
            f"--modalities-model {n} --modalities text,image")
    out("  ...then --target codex --refresh-models and reload the VSCode window.")
    return 0


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #

def run_status(rep: detect.Report, args) -> int:
    out(rep.render())

    rule("Claude Code")
    from jsonc import read_jsonc
    for path in [*rep.claude_editor_settings, *rep.claude_cli_settings]:
        try:
            data = read_jsonc(path)
        except Exception as e:  # noqa: BLE001
            out(f"  {path}: unreadable ({e})")
            continue
        env = {}
        for e in data.get("claudeCode.environmentVariables") or []:
            if isinstance(e, dict) and "name" in e:
                env[e["name"]] = e.get("value")
        if isinstance(data.get("env"), dict):
            env.update(data["env"])
        if not env:
            out(f"  {path}: no gateway config")
            continue
        out(f"  {path}:")
        for k in claude_mod.MANAGED:
            if k in env:
                v = _mask(str(env[k])) if ("TOKEN" in k or "KEY" in k) else env[k]
                out(f"      {k} = {v}")

    rule("claude.exe")
    if not rep.claude_binaries:
        out("  no bundled binary found")
    for b in rep.claude_binaries:
        out(f"  {claude_patch.status(b):<10} {b}")
    alive = claude_patch.running_processes()
    out(f"  running now: {', '.join(alive) if alive else 'none'}")

    rule("/model picker")
    user_settings = user_claude_settings()
    picker = None
    if user_settings is not None:
        try:
            picker = read_jsonc(user_settings).get(claude_mod.PICKER_KEY)
        except Exception:  # noqa: BLE001
            picker = None
    if not isinstance(picker, dict):
        out("  not curated -- the picker lists Claude Code's own lineup")
        out("      (Opus/Sonnet/Haiku, which this gateway does not serve)")
        out("      fix: --target claude --refresh-models")
    else:
        rows = picker.get("options") or []
        out(f"  {len(rows)} row(s) in {user_settings}")
        for r in rows:
            if isinstance(r, dict):
                out(f"      {str(r.get('model', '?')):32} {r.get('description', '')}")
        if picker.get("replaceBuiltInOptions") is True:
            out("      built-in lineup: hidden")
        else:
            out("      built-in lineup: still shown, gateway rows appended after it")

    rule("gateway model cache")
    cache_path = claude_mod.gateway_cache_path()
    cache = claude_mod.read_gateway_cache(cache_path)
    if cache is None:
        out(f"  none at {cache_path}  (discovery will re-fetch)")
    else:
        models = [str(m.get("id", "")) for m in (cache.get("models") or [])]
        out(f"  {cache_path}")
        out(f"      {len(models)} cached: {', '.join(models) or '(empty)'}")
        stale = claude_mod.cache_looks_filtered(cache)
        out(f"      written by: {cache.get('baseUrl', '?')}  "
            f"{'<- looks like a PRE-PATCH (filtered) result' if stale else ''}")
        if stale:
            out("      clear it with: --target claude --clear-model-cache")

    rule("Codex")
    if rep.codex_config and rep.codex_config.exists():
        from tomlpatch import TomlFile
        t = TomlFile(rep.codex_config)
        out(f"  {rep.codex_config}")
        out(f"      model          = {t.get('model')}")
        out(f"      model_provider = {t.get('model_provider')}")
        for header in ("private", "private-gateway"):
            tbl = codex_mod.provider_table(header)
            if t.get("base_url", table=tbl):
                out(f"      [{tbl}]")
                for k in ("name", "base_url", "wire_api", "env_key", "requires_openai_auth",
                          "experimental_bearer_token"):
                    v = t.get(k, table=tbl)
                    if v is None:
                        continue
                    if k == "experimental_bearer_token":
                        v = _mask(v.strip("'\""))
                    out(f"          {k} = {v}")

        cat = codex_mod.existing_catalog_path(rep.codex_config)
        out(f"      {codex_mod.CATALOG_KEY} = {cat}")
        if not cat:
            out("      -> Codex's model dropdown lists OpenAI's own lineup")
            out("         (GPT-5.6 Sol/Terra/Luna, GPT-5.5, ...), none of which this")
            out("         gateway serves. fix: --target codex --refresh-models")
        elif not Path(cat).exists():
            out(f"      ! {cat} does not exist -- Codex will fail to start")
        else:
            try:
                entries = json.loads(Path(cat).read_text(encoding="utf-8"))["models"]
            except Exception as e:  # noqa: BLE001
                out(f"      ! unreadable ({e})")
            else:
                slugs = [str(e.get("slug", "?")) for e in entries]
                out(f"      {len(slugs)} model(s), and nothing else:")
                for s in slugs:
                    out(f"          {s}")
                if not codex_mod.catalog_is_ours(rep.codex_config):
                    out("      (this file was not generated by this tool; left alone)")
    else:
        out("  no config.toml")

    rule("Codex sign-in")
    state, detail = codex_auth.status()
    out(f"  {state}  ({detail})")
    if state in ("MISSING", "EMPTY", "UNREADABLE"):
        out("      Codex shows a login prompt it cannot complete against a private")
        out("      gateway. fix: --fix-login")

    rule("per-model thinking levels")
    store = reasoning.load()
    if not store:
        out(f"  no overrides in {reasoning.store_path()}")
        out(f"      private models default to: "
            f"{', '.join(x['effort'] for x in reasoning._levels(reasoning.DEFAULT_EFFORTS))}")
        out("      fix: --configure-reasoning --reasoning-model <id> "
            "--reasoning-levels low,high,xhigh")
    else:
        out(f"  {reasoning.store_path()}")
        for model_id, entry in sorted(store.items()):
            lv = [x["effort"] for x in (entry.get("levels") or [])]
            out(f"      {model_id:28} {', '.join(lv) or '(no submenu)'}"
                f"  default={entry.get('default', '-')}")

    rule("per-model input types")
    mod_store = modalities.load()
    if mod_store:
        out(f"  overrides in {modalities.store_path()}:")
        for model_id, entry in sorted(mod_store.items()):
            chosen = modalities.normalize_entries(
                (entry or {}).get("input_modalities") if isinstance(entry, dict) else None)
            out(f"      {model_id:28} {', '.join(chosen) or '(none)'}")
    out(f"  measured on this deployment ({len(modalities.MEASURED_MODALITIES)} models):")
    for model_id, types in sorted(modalities.MEASURED_MODALITIES.items()):
        out(f"      {model_id:28} {', '.join(types)}")
    if not any("image" in t for t in modalities.MEASURED_MODALITIES.values()):
        out("      (none accept images; re-measure with --probe-modalities)")

    rule("gateway model parameters")
    # `--status` is a read-only report; it must not start interrogating the user
    # for credentials (resolve_credentials prints the whole examples block when
    # there is nothing saved). No known gateway, nothing to report.
    known_base = args.api_base or _codex_saved_base(rep.codex_config or detect.codex_config()) \
        or claude_mod.existing_base_url([*rep.claude_editor_settings, *rep.claude_cli_settings])
    if not known_base:
        out("  no gateway configured yet -- run --target codex first")
    else:
        try:
            base, key, gw = read_gateway_models(args, rep)
        except (SystemExit, gateway.GatewayError) as e:
            out(f"  could not read /model/info: {e}")
            out("      (needs the LiteLLM master key; pass --api-base/--api-key)")
            gw = None
        if gw is not None:
            todo = gw.todo
            if not todo:
                out("  all private models accept Codex's Responses fields")
            else:
                out(f"  ! {len(todo)} model(s) are missing parameters Codex needs;")
                out("    every turn against them fails (HTTP 400 / HTTP 500).")
                for p in todo:
                    out(f"      {p.model_name:28} {litellm_admin.describe(p)}")
                out("      fix: --emit-gateway-config  (then --apply-gateway-config)")
    return 0


# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EXAMPLES,
    )
    ap.add_argument("--target", "-t", choices=["claude", "codex", "both"],
                    help="which extension to configure")
    ap.add_argument("--api-base", help="gateway base URL, e.g. http://10.18.219.156:4000")
    ap.add_argument("--api-key", help="gateway API key, e.g. sk-XXXXXXXX")
    ap.add_argument("--model", help="model id to pin (see --list-models)")
    ap.add_argument("--list-models", action="store_true",
                    help="print every model the gateway serves and exit")
    ap.add_argument("--switch-model", action="store_true",
                    help="codex: pick a model interactively and write it to config.toml")
    ap.add_argument("--detect", action="store_true", help="show config file locations and exit")
    ap.add_argument("--status", action="store_true", help="show current configuration and exit")
    ap.add_argument("--restore", action="store_true", help="put the .bak files back")
    ap.add_argument("--yes", "-y", action="store_true", help="never prompt; fail instead")
    ap.add_argument("--claude-binary", help="explicit path to claude.exe")
    ap.add_argument("--clear-model-cache", action="store_true",
                    help="claude: drop ~/.claude/cache/gateway-models.json so the "
                         "/model picker re-fetches from the gateway")
    ap.add_argument("--refresh-models", action="store_true",
                    help="re-fetch the gateway model list and rewrite only the model "
                         "list: Claude Code's /model picker rows, and/or Codex's "
                         "model catalog (no env vars, no binary patch). Defaults to "
                         "claude; use --target codex|both for the rest")
    ap.add_argument("--keep-builtin-models", action="store_true",
                    help="claude: leave Claude Code's built-in Opus/Sonnet/Haiku rows "
                         "in the /model picker instead of showing only gateway models")

    g = ap.add_mutually_exclusive_group()
    g.add_argument("--no-patch", action="store_true",
                   help="claude: configure settings but leave claude.exe alone")
    g.add_argument("--patch-only", action="store_true",
                   help="claude: only (re)apply the model-filter patch")

    ap.add_argument("--no-prompt-model", action="store_true",
                    help="claude: never offer to pin a default model")
    ap.add_argument("--inline-key", action="store_true",
                    help="codex: write the key into config.toml instead of using an env var")
    ap.add_argument("--env-key", help="codex: name of the env var holding the key")
    ap.add_argument("--provider-id", default=codex_mod.DEFAULT_PROVIDER_ID,
                    help="codex: provider table id (default: private)")
    ap.add_argument("--profile", help="codex: write a [profiles.<name>] instead of the root config")
    ap.add_argument("--skip-probe", action="store_true",
                    help="codex: skip the /v1/responses reachability probe")
    ap.add_argument("--strip-openai-keys", action="store_true",
                    help="codex: delete OpenAI-only keys such as service_tier from config.toml")

    ap.add_argument("--emit-gateway-config", action="store_true",
                    help="print the /model/update calls that let private models accept "
                         "Codex's reasoning_effort and drop client_metadata, without "
                         "changing the gateway")
    ap.add_argument("--apply-gateway-config", action="store_true",
                    help="same, but actually send them (needs the LiteLLM master key)")
    ap.add_argument("--fix-login", action="store_true",
                    help="codex: write ~/.codex/auth.json so Codex stops asking for a "
                         "ChatGPT login (use with --restore to undo, --force to rewrite)")
    ap.add_argument("--force", action="store_true",
                    help="codex logins: rewrite auth.json even if a sign-in already exists")
    ap.add_argument("--configure-reasoning", action="store_true",
                    help="set which thinking levels a private model offers in Codex")
    ap.add_argument("--reasoning-model", help="model id to configure thinking levels for")
    ap.add_argument("--reasoning-levels",
                    help="comma-separated efforts, e.g. low,high,max; empty string means "
                         "no Reasoning submenu for that model")
    ap.add_argument("--reasoning-default", help="default effort (must be one of --reasoning-levels)")
    ap.add_argument("--reasoning-clear", metavar="MODEL",
                    help="drop the override for MODEL, or `all` for every model")
    ap.add_argument("--configure-modalities", action="store_true",
                    help="set which input types (text/image) a private model accepts in Codex")
    ap.add_argument("--modalities-model", help="model id to configure input types for")
    ap.add_argument("--modalities",
                    help="comma-separated types, e.g. text,image; `text` alone blocks "
                         "attachments for that model")
    ap.add_argument("--modalities-clear", metavar="MODEL",
                    help="drop the override for MODEL, or `all` for every model")
    ap.add_argument("--probe-modalities", action="store_true",
                    help="measure which gateway models can really read an image, by "
                         "asking them to tell red from blue")
    ap.add_argument("--probe-models",
                    help="comma-separated model ids to probe (default: every chat model)")

    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    rep = detect.discover()

    if args.detect:
        out(rep.render())
        return 0
    if args.status:
        return run_status(rep, args)
    if args.strip_openai_keys:
        cfg = rep.codex_config or detect.codex_config()
        removed = codex_mod.strip_openai_only_keys(cfg)
        out(f"  removed from {cfg}: {', '.join(removed) if removed else '(nothing)'}")
        return 0
    if args.clear_model_cache:
        rule("gateway model cache")
        report_and_clear_cache(force=True)
        return 0
    # Gateway-side and reasoning commands stand alone: they touch no client file,
    # so they must not drag in --target or the editor-config machinery.
    if args.emit_gateway_config or args.apply_gateway_config:
        return run_gateway_config(args, rep, apply=args.apply_gateway_config)
    if args.fix_login:
        return run_fix_login(args, rep)
    if args.configure_reasoning or args.reasoning_clear:
        return run_configure_reasoning(args, rep)
    if args.configure_modalities or args.modalities_clear:
        return run_configure_modalities(args, rep)
    if args.probe_modalities:
        return run_probe_modalities(args, rep)

    target = args.target
    if args.patch_only:
        target = "claude"
    # --refresh-models was Claude-only before Codex grew a catalog; defaulting to
    # claude keeps every existing invocation meaning exactly what it used to.
    if args.refresh_models and not target:
        target = "claude"
    if args.refresh_models:
        rc = 0
        if target in ("claude", "both"):
            rc |= refresh_models(args, rep)
        if target in ("codex", "both"):
            rc |= refresh_catalog(args, rep)
        return rc
    if not target:
        if not is_tty():
            raise SystemExit("--target is required (claude | codex | both)")
        out("Which plugin should be pointed at the private gateway?")
        out("  1. Claude Code   2. Codex   3. both")
        target = {"1": "claude", "2": "codex", "3": "both"}.get(
            ask("choice", default="3"), "both"
        )
        if not args.api_base or not args.api_key:
            out()
            out(EXAMPLES)

    if args.patch_only:
        patch_claude_binary(args, rep)
        return 0

    rc = 0
    if target in ("claude", "both"):
        rc |= run_claude(args, rep)
    if target in ("codex", "both"):
        rc |= run_codex(args, rep)

    if target == "both":
        rule("done")
        out("  Reopen VSCode so the extension picks up the new settings and env vars.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
