#!/usr/bin/env python3
r"""把 Codex CLI 指向私有网关：免登录，`/model` 实时列出网关模型。

    python3 private_api.py                              # 交互式向导
    python3 private_api.py --api-base http://10.18.219.156:4000 --api-key sk-xxx
    python3 private_api.py --list-models                # 查看网关提供哪些模型
    python3 private_api.py --switch-model               # 换一个要固定的模型
    python3 private_api.py --sync                       # 刷新模型目录（包装脚本用）
    python3 private_api.py --status                     # 查看当前配置
    python3 private_api.py --install-wrapper            # 每次启动自动刷新
    python3 private_api.py --restore                    # 撤销

会改动哪些文件

  脚本旁边（工具包目录）：

  * `model-config.jsonc` -> 每个模型的能力配置：图片支持、思考档位、上下文长度。
                      首次运行时由 `private-api/model-config.seed.jsonc` 与网关实时
                      模型列表交叉生成，之后归你所有——重复运行只会追加没见过的
                      模型，绝不改写你已经写好的内容。在这里编辑，然后运行
                      `--apply-model-config` 把改动同步到实际环境。它放在脚本旁边
                      而不是隐藏目录里：用户找不到的配置文件等于没人会去改。

  `$CODEX_HOME` 下（默认 `~/.codex`）：

  * `config.toml`  -> `[model_providers.private]` + `model` + `model_catalog_json`
                      一个指向网关的 `[model_providers.<id>]` 配置表，并设置
                      `requires_openai_auth = false`，免得 Codex 去要它永远用不上的
                      ChatGPT 账号。
  * `gateway-models.json` -> 生成的模型目录。正是它让 `/model` 只列出网关的模型：
                      该文件会替换 Codex 内置的 OpenAI 模型清单（那些模型网关都
                      提供不了）。它是产物——每次运行都根据 `model-config.jsonc`
                      重新生成。
  * `auth.json`    -> 网关密钥，这样不会出现登录提示。无论 `model_provider` 怎么配，
                      Codex 都靠这个文件判断「已登录」——见 private-api/codex_auth.py。
  * `private-reasoning.json` / `private-modalities.json` -> 旧的按模型存储。
                      仍会读取（这样在 `model-config.jsonc` 出现之前配置过的机器能保留
                      原有选择），但不再写入；配置文件优先级更高。
  * `~/.local/bin/codex` -> 仅在 `--install-wrapper` 时安装。一个很短的 shell 包装
                      脚本，在真正执行 codex 之前刷新 `gateway-models.json`
                      （有节流，且出错也绝不阻断）。

网关本身（仅在 `--apply-gateway-config` 时）

  `custom_openai` 模型上的 `litellm_params.allowed_openai_params` 和
  `additional_drop_params`。Codex 只说 `/v1/responses`，而且每一轮都带
  `reasoning.effort`（LiteLLM 会映射成 `reasoning_effort`，而 `custom_openai`
  定义会拒绝 -> HTTP 400）和 `client_metadata`（LiteLLM 会原样转发给 OpenAI SDK，
  而 SDK 没有这个关键字参数 -> HTTP 500）。两者缺一，针对私有模型的每一轮请求
  都会失败。用 `--emit-gateway-config` 查看现状，它只打印；真正应用需要 LiteLLM
  主密钥，并且会改变所有使用该网关的人的配置。

写任何文件之前都会先备份成 `<file>.bak`。

目录结构
------
这是入口脚本，位于 `codexcli/` 顶层。它调用的模块在 `codexcli/private-api/`；
那里没有需要直接运行的东西。`codexcli/README.md` 是完整说明（中文）。
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB = HERE / "private-api"
sys.path.insert(0, str(LIB))

import codex as codex_mod
import codex_auth
import detect
import gateway
import litellm_admin
import modalities
import modelconfig
import reasoning
import search as search_mod
from tomlpatch import TomlFile

# --------------------------------------------------------------------------- #

EXAMPLES = """\
  --api-base 示例
      http://10.18.219.156:4000          局域网里的 LiteLLM 网关
      http://127.0.0.1:4000              本机部署，docker-compose 默认值
      https://llm.corp.example.com       走 ingress 的域名（结尾带 /v1 也可以）

  --api-key 示例
      sk-xxxxxxxxxxxxxxxxxxxxxxxx        LiteLLM 主密钥 / 虚拟密钥
      <你自己网关的 sk-... 密钥>

  完整命令
      python3 private_api.py \\
          --api-base http://10.18.219.156:4000 \\
          --api-key sk-XXXXXXXX
"""

# How long --sync waits for the gateway before giving up and letting codex start.
# It runs on the critical path of every `codex` launch, so it is deliberately
# impatient: a slow gateway costs the user a stale model list, not a hang.
SYNC_TIMEOUT = 8.0


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


def note(msg: str) -> None:
    """A line that --sync/--quiet suppresses. Errors never go through this."""
    if not QUIET:
        out(msg)


# Set from --quiet in main(). A module global rather than a parameter because it
# is read by helpers several call layers down (fetch_models, warn_gateway_params).
QUIET = False


def _mask(secret: str) -> str:
    return f"{secret[:5]}...{secret[-4:]}" if len(secret) > 12 else "***"


def _auth_state(state: str) -> str:
    """把 codex_auth 的英文状态码译成中文，仅供显示用。"""
    return {
        "MISSING": "缺失",
        "EMPTY": "空文件",
        "UNREADABLE": "无法读取",
        "API_KEY": "API 密钥已登录",
        "CHATGPT": "ChatGPT 账号已登录",
    }.get(state, state)


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
            raise SystemExit("已中止")
        val = val.strip() or (default or "")
        if val:
            return val
        out("  （必填 —— 按 Ctrl+C 中止）")


def confirm(prompt: str, default: bool = True) -> bool:
    if not is_tty():
        return default
    d = "Y/n" if default else "y/N"
    try:
        ans = input(f"{prompt} [{d}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        out()
        raise SystemExit("已中止")
    if not ans:
        return default
    return ans in ("y", "yes")


def pick_model(models: list[gateway.Model],
               title: str = "选择一个模型") -> str:
    """Numbered picker over the live gateway list. Returns the chosen id."""
    chat = [m for m in models if m.is_chat_capable]
    other = [m for m in models if not m.is_chat_capable]

    out()
    out(f"{title}（来自网关，共提供 {len(models)} 个）：")
    for i, m in enumerate(chat, 1):
        out(f"  {i:>3}. {m.id}")
    for i, m in enumerate(other, len(chat) + 1):
        out(f"  {i:>3}. {m.id}   <- 不是对话模型，无法使用")

    # Deliberately no isatty() gate: piped input (`printf '3\n' | ...`) is a
    # legitimate way to script this, and `ask()` turns EOF into a clear abort.
    ordered = chat + other
    while True:
        raw = ask("序号，或用于筛选的片段，或完整的模型 id").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(ordered):
            return ordered[int(raw) - 1].id
        matches = [m for m in models if raw.lower() in m.id.lower()]
        if len(matches) == 1:
            return matches[0].id
        if len(matches) > 1:
            out("  有多个匹配：")
            for m in matches:
                out(f"    {m.id}")
            continue
        if any(m.id == raw for m in models):
            return raw
        out("  没有模型使用这个 id，请重试")


# --------------------------------------------------------------------------- #
# shared setup
# --------------------------------------------------------------------------- #

def snapshot(paths: list[Path]) -> None:
    """Copy each existing file to `<name>.bak` once, before we touch it."""
    for p in paths:
        if not p.exists():
            continue
        bak = p.with_suffix(p.suffix + ".bak")
        if not bak.exists():  # only ever snapshot the ORIGINAL
            bak.write_bytes(p.read_bytes())


def resolve_credentials(args, saved_base: str | None,
                        saved_key: str | None = None) -> tuple[str, str]:
    """Fill in --api-base / --api-key, prompting or showing examples as needed."""
    base, key = args.api_base, args.api_key

    # The gateway in config.toml is one this tool wrote on an earlier run, so
    # reusing it is the obvious default -- asking every time is just noise.
    if not base and saved_base:
        base = saved_base
        note(f"  在 config.toml 里找到了已有网关：{saved_base}")

    # The key for that gateway is usually already there too. Reusing it is what
    # makes `--refresh-models` a one-liner -- but only when the base is the same
    # one, or it would silently point an old key at a different gateway.
    if (not key and saved_key and saved_base and base
            and gateway.normalize_base(base, keep_v1=False)
            == gateway.normalize_base(saved_base, keep_v1=False)):
        key = saved_key
        note(f"  复用 config.toml 里已有的 API 密钥（{_mask(key)}）")

    if not base:
        out()
        out("没有提供 --api-base。")
        out(EXAMPLES)
        if not is_tty() or args.yes:
            raise SystemExit("必须提供 --api-base（见上面的示例）")
        base = ask("网关地址（API base URL）", default=saved_base)

    if not key:
        out()
        out("没有提供 --api-key。")
        out(EXAMPLES)
        if not is_tty() or args.yes:
            raise SystemExit("必须提供 --api-key（见上面的示例）")
        key = ask("API 密钥", secret=True)

    return gateway.normalize_base(base, keep_v1=False), key


def fetch_models(base: str, key: str, *, required: bool,
                 timeout: float = 30.0) -> list[gateway.Model]:
    try:
        models = gateway.list_models(base, key, timeout=timeout)
    except gateway.GatewayError as e:
        if required:
            raise SystemExit(f"无法获取模型列表：{e}")
        note(f"  ! 无法获取模型列表（{e}）")
        note("    继续执行——仍然可以把 Codex 指向该网关。")
        return []
    note(f"  网关连接正常：共提供 {len(models)} 个模型")
    return models


def gateway_credentials(args) -> tuple[str, str]:
    """Base + key, reusing whatever a previous run stored.

    `/model/info` and `/model/update` are master-key endpoints, so this is the
    same credential the rest of the tool already persists.
    """
    cfg = detect.codex_config()
    return resolve_credentials(args, codex_mod.saved_base(cfg),
                               codex_mod.saved_key(cfg))


# --------------------------------------------------------------------------- #
# the main setup flow
# --------------------------------------------------------------------------- #

def report_version(rep: detect.Report, *, quiet_ok: bool = False) -> None:
    """Print which codex this toolkit is talking to, and whether it fits.

    The catalog format is version-sensitive -- the 0.145 rename of
    `supports_reasoning_summaries` is what broke a 0.144.1 box -- so the version
    is part of every run's output rather than something to dig out later.
    """
    # NB: `text`, not `note` -- `note` is the module-level printer, and binding
    # the verdict to that name turned the too-old path into a TypeError.
    ok, text = codex_mod.version_note(rep.codex_version)
    if not rep.codex_version:
        if not quiet_ok:
            note(f"  ! {text}")
        return
    if ok and quiet_ok:
        return
    out(f"  {'' if ok else '! '}codex: {text}")


def run_setup(args, rep: detect.Report) -> int:
    cfg = rep.codex_config or detect.codex_config()

    rule("Codex CLI")
    report_version(rep)

    base, key = resolve_credentials(args, codex_mod.saved_base(cfg),
                                    codex_mod.saved_key(cfg))
    base_v1 = gateway.normalize_base(base, keep_v1=True)

    models = fetch_models(base, key, required=args.list_models or args.switch_model)

    if args.list_models:
        for m in models:
            flag = "" if m.is_chat_capable else "   <- 不是对话模型"
            out(f"    {m.id}{flag}")
        if not args.switch_model:
            return 0

    current = codex_mod.saved_model(cfg)
    if current:
        out(f"  config.toml 里当前使用的模型：{current}")

    model = codex_mod.clean_model_id(args.model) if args.model else None
    if not model and models:
        if args.switch_model or (is_tty() and not args.yes):
            model = pick_model(models, "选择 Codex 要使用的模型")
        else:
            # No terminal and no --model: keep whatever is pinned, else take the
            # first model the gateway serves. A wizard that fails because it
            # could not ask a question is worse than one that picks sensibly and
            # says so.
            model = current or next((m.id for m in models if m.is_chat_capable), None)
            if model:
                note(f"  没有提供 --model；自动使用 {model!r}")
    if not model:
        out("  ! 没有选择模型；请传 --model <id> 或运行 --switch-model")
        return 1

    if models and model not in {m.id for m in models}:
        out(f"  ! 网关不提供 {model!r}；以下是它提供的模型：")
        for m in models:
            out(f"      {m.id}")
        return 1

    # Codex 0.150 only speaks /responses. Verify the gateway serves it before
    # writing a config that would fail on the first prompt.
    if not args.skip_probe:
        out(f"  正在用 {model} 探测 /v1/responses ...")
        try:
            result = gateway.probe_wire_api(base_v1, key, model)
        except gateway.GatewayError as e:
            out(f"  ! 探测失败：{e}")
        else:
            if result.get("responses"):
                out("  /v1/responses  OK")
            else:
                out("  ! 该网关不提供 /v1/responses")
                if result.get("chat"):
                    out("    /v1/chat/completions 可用，但 Codex 0.150 已经取消了")
                    out('    wire_api = "chat" —— 在网关代理 /responses 之前，')
                    out("    Codex 无法驱动该网关（LiteLLM 可以代理）。")
                return 1

    detect.ensure_home()
    inline_key = None if args.use_env_key else key
    snapshot([cfg])
    summary = codex_mod.apply_config(
        cfg, base_v1, model,
        pid=args.provider_id, env_key=args.env_key, inline_key=inline_key,
        profile=args.profile,
    )

    out()
    out(f"  已写入 {cfg}")
    out(f"    [{summary['provider_table']}]")
    out(f"      base_url = {base_v1}")
    out(f'      wire_api = "{codex_mod.WIRE_API}"')
    out("      requires_openai_auth = false")
    if inline_key:
        out(f"      experimental_bearer_token = {_mask(key)}   （内联）")
    else:
        name = args.env_key or codex_mod.env_key_name(args.provider_id)
        out(f"      env_key  = {name}")
    if summary.get("profile"):
        out(f"    [profiles.{summary['profile']}]  "
            f"-> 用 `codex --profile {summary['profile']}` 生效")
    elif model:
        out(f'    model = "{model}"')
        out(f'    model_provider = "{args.provider_id}"')

    if not inline_key:
        name = args.env_key or codex_mod.env_key_name(args.provider_id)
        where = codex_mod.set_env_var(name, key)
        out()
        out(f"  API 密钥：{where}")

    if models:
        write_catalog(cfg, models)
        strip_blocking_keys(cfg)
        warn_gateway_params(base, key)

    # The login gate is independent of everything above: without auth.json Codex
    # asks for a ChatGPT account no matter what the provider table says.
    if not args.skip_login:
        # A real ChatGPT sign-in already satisfies that gate, and a private
        # provider never reads the tokens it holds -- so overwriting it would
        # throw away a working account for nothing. Everything else (missing,
        # empty, or an API key an earlier run of this tool left behind) is ours
        # to write.
        state, _ = codex_auth.status()
        write_login(key, quiet_ok=True, force=state != "CHATGPT")

    if args.install_wrapper:
        install_wrapper(args)

    write_helpers()

    # Search LAST, and non-fatally. Last because it reuses the key the steps
    # above just wrote -- run first, it would have nothing to copy. Non-fatal
    # because being unable to add a search tool must not fail a run whose actual
    # job (pointing Codex at the gateway) succeeded. This is what makes the whole
    # thing one command instead of one command plus a follow-up.
    if not args.no_search:
        run_configure_search(args, rep, optional=True)

    out()
    out("  下一步：运行 `codex`，用 `/model` 在网关模型之间切换。")
    return 0


def write_helpers() -> list[Path]:
    """Write `codexcli/bin/codex-model`, the "switch model later" shortcut.

    Best-effort: a read-only checkout must not fail the setup that already
    succeeded, so a write error is reported and swallowed.
    """
    try:
        paths = codex_mod.write_switch_helpers(HERE)
    except OSError as e:
        note(f"  ! 无法写入辅助脚本（{e}）")
        return []
    for p in paths:
        note(f"  已写入 {p}")
    return paths


def generate_model_config(models: list[gateway.Model]) -> Path | None:
    """Create or top up `model-config.jsonc`, beside this script.

    The file is where a model's capability now lives -- image support, thinking
    rungs, context length -- so it is written BEFORE the catalog is built, and the
    catalog is then resolved from it. That is what makes "install and it works"
    true without a second command, and what makes the second command a refinement
    rather than a prerequisite.

    Only ever ADDS: a slug already in the file is left byte-for-byte alone,
    comments and all. `--refresh-models` is cheap enough that people run it often,
    and silently reverting an edit would be indistinguishable from a broken tool.
    OpenAI's own models are skipped -- their capability is OpenAI's to declare and
    is copied from `models_cache.json`; see modelconfig.py.
    """
    entries: dict[str, dict[str, object]] = {}
    for m in models:
        if not m.is_chat_capable:
            continue
        if reasoning.is_openai_official(m.id):
            continue
        raw = m.raw or {}
        entries[m.id] = modelconfig.suggested(
            m.id, modelconfig.TOOLKIT_CODEX,
            advertised_context=raw.get("max_input_tokens"),
        )
    if not entries:
        return None

    try:
        path, added, kept = modelconfig.ensure(entries, modelconfig.TOOLKIT_CODEX)
    except (OSError, ValueError) as e:
        # Never fatal: the catalog can still be built from the seed and defaults,
        # so a config file we cannot write must not cost the user their setup.
        out(f"  ! 无法写入模型配置文件（{e}）；改用种子里的默认值")
        return None

    if added and kept:
        out(f"  {path}  （新增 {len(added)} 个模型：{', '.join(added)}）")
        out("    —— 请编辑这个文件，补充/修正每个模型的图片支持、思考档位和上下文长度，")
        out(f"    然后运行 `python3 {self_cmd()} --apply-model-config` 让改动生效。")
    else:
        out(f"  已写入 {path}  （按模型的能力配置——归你编辑）")
        out("    —— 请编辑这个文件，补充/修正每个模型的图片支持、思考档位和上下文长度，")
        out(f"    然后运行 `python3 {self_cmd()} --apply-model-config` 让改动生效。")
    return path


def write_catalog(cfg: Path, models: list[gateway.Model]) -> list[str]:
    """Generate the catalog JSON and point config.toml at it."""
    out()
    generate_model_config(models)
    path = catalog_target(cfg)
    try:
        slugs = codex_mod.write_catalog(path, models)
    except ValueError as e:
        out(f"  ! {e}；保留 Codex 自己的模型列表")
        return []

    codex_mod.apply_catalog(cfg, path)
    codex_mod.mark_refreshed(models, time.time())

    out(f"  已写入 {path}  （模型目录——`/model` 列出的就是它）")
    out(f"    {len(slugs)} 个模型，且仅此而已：")
    for s in slugs:
        raw = next((m.raw or {} for m in models if m.id == s), {})
        levels, default = reasoning.levels_for(s, raw)
        shown = ",".join(lv["effort"] for lv in levels) or "无思考档位菜单"
        out(f"      {s:28} 思考档位: {shown}")
    hidden = [m.id for m in models if not m.is_chat_capable]
    if hidden:
        out(f"    （已省略 {len(hidden)} 个非对话端点：{', '.join(hidden)}）")
    return slugs


def catalog_target(cfg: Path) -> Path:
    """Where the catalog JSON goes.

    A catalog a previous run of this tool wrote is reused so the file stays where
    the user last saw it; otherwise we fall back to `$CODEX_HOME/gateway-models.json`.
    A `model_catalog_json` pointing anywhere ELSE is left alone -- that one is the
    user's, and overwriting it would be a surprise.
    """
    if codex_mod.catalog_is_ours(cfg):
        previous = codex_mod.existing_catalog_path(cfg)
        if previous:
            return Path(previous)
    return codex_mod.catalog_path()


def strip_blocking_keys(cfg: Path) -> None:
    """Remove root-level keys that stop the catalog's reasoning from working.

    Two root-level keys in config.toml silently override the per-model reasoning
    levels we just wrote into the catalog:

      * `model_reasoning_effort` -- a global default that outranks every model's
        `default_reasoning_level`. When it is set (often to ``"none"`` by an
        older OpenAI-only config), the reasoning menu disappears for *every*
        model, not just the one the user intended.
      * `service_tier` -- an OpenAI-only concept a private gateway may reject.

    Deleting them here means the catalog we just wrote actually takes effect.
    """
    removed_reasoning = codex_mod.strip_reasoning_override(cfg)
    if removed_reasoning is not None:
        out()
        out(f'  已删除根级配置 model_reasoning_effort = "{removed_reasoning}"')
        out("    它会覆盖目录里每个模型的 default_reasoning_level，")
        out("    导致所有模型的思考档位菜单被隐藏或卡在同一个档位。")

    for k in codex_mod.strip_openai_only_keys(cfg):
        out()
        out(f"  已删除根级配置 {k}（OpenAI 专有；私有网关可能拒绝它）")


def warn_gateway_params(base: str, key: str) -> None:
    """Non-fatal heads-up when the gateway would 400 every Codex turn.

    Worth doing immediately after writing the config: otherwise the failure
    surfaces as an opaque error inside codex, and the fix is a different command
    that nothing has pointed at yet.
    """
    try:
        report = litellm_admin.build_report(litellm_admin.fetch_model_info(base, key))
    except gateway.GatewayError:
        return  # never fatal here; --status reports this properly
    todo = report.todo
    if not todo:
        return
    out()
    out(f"  ! {len(todo)} 个私有模型缺少 Codex 需要的网关参数，")
    out("    对它们的请求会失败（思考参数报 HTTP 400，元数据报 HTTP 500）：")
    for p in todo:
        out(f"      {p.model_name:28} {litellm_admin.describe(p)}")
    out(f"    修复：python3 {self_cmd()} --emit-gateway-config")


# --------------------------------------------------------------------------- #
# login (auth.json)
# --------------------------------------------------------------------------- #

def write_login(key: str, *, quiet_ok: bool = False, force: bool = True) -> bool:
    """Write `auth.json` so Codex stops asking for a ChatGPT account.

    The value is the GATEWAY's key: it is what the private provider table already
    authenticates with, so this introduces no new secret -- it moves one that is
    already on this machine (in config.toml) into the file Codex actually
    consults for "am I signed in".
    """
    state, detail = codex_auth.status()
    if state == "API_KEY" and not force:
        note(f"  登录：已经是 API 密钥（{detail}）")
        return False
    if state == "CHATGPT" and not force:
        note(f"  登录：已存在真实的 ChatGPT 登录，保持不动（{detail}）")
        return False

    path, backup = codex_auth.write_api_key_auth(key)
    if not quiet_ok:
        out()
    out(f"  已写入 {path}  （登录状态，Codex 不会再弹登录提示）")
    out(f"    OPENAI_API_KEY = {_mask(key)}   （网关密钥，不是 OpenAI 的密钥）")
    if backup:
        out(f"    原文件已备份到 {backup}")
    return True


def run_fix_login(args, rep: detect.Report) -> int:
    rule("Codex 登录状态")

    if args.restore:
        if codex_auth.restore_from_backup():
            out(f"  已从 .bak 恢复 {codex_auth.auth_path()}")
        else:
            out("  没有 auth.json.bak 可以恢复——未做任何改动")
        return 0

    state, detail = codex_auth.status()
    out(f"  当前状态：{_auth_state(state)}  （{detail}）")

    base, key = gateway_credentials(args)

    # Writing a key the gateway rejects would be worse than writing nothing:
    # Codex would look signed in and then fail every turn. Prove it works first.
    out()
    out(f"  正在用 {base} 校验密钥 ...")
    try:
        models = gateway.list_models(base, key)
    except gateway.GatewayError as e:
        raise SystemExit(
            f"  ! 密钥被拒绝，未写入 auth.json：\n      {e}"
        )
    out(f"  密钥有效：网关提供 {len(models)} 个模型")

    write_login(key, force=args.force)
    state, detail = codex_auth.status()
    out()
    out(f"  之后状态：{_auth_state(state)}  （{detail}）")
    return 0


# --------------------------------------------------------------------------- #
# the wrapper: refresh the catalog on every launch
# --------------------------------------------------------------------------- #

def install_wrapper(args) -> tuple[Path, Path] | None:
    """Write `~/.local/bin/codex`. Returns (wrapper, real binary), or None."""
    real = codex_mod.find_real_codex()
    out()
    if not real:
        out("  ! 找不到真正的 `codex` 可执行文件，未安装包装脚本。")
        out("    请先安装 codex（npm i -g @openai/codex），或者运行")
        out(f"    `python3 {self_cmd()} --install-wrapper --real-codex /path/to/codex`。")
        return None

    target = codex_mod.wrapper_path()
    if target.exists() and not codex_mod.wrapper_is_ours(target):
        out(f"  ! {target} 已存在，且不是本工具写的，拒绝覆盖。")
        out("    请把它移走，或用 --wrapper-dir 装到别处。")
        return None

    target = codex_mod.install_wrapper(real, ttl=args.refresh_ttl)
    out(f"  已写入 {target}")
    out(f"    实际执行 {real}")
    out(f"    最多每 {args.refresh_ttl}s 刷新一次模型目录，网关慢或挂掉时也不会")
    out("    阻塞 codex 启动")

    if not codex_mod.path_has_wrapper_dir():
        out()
        out(f"  ! {codex_mod.wrapper_dir()} 不在 PATH 里，`codex` 仍会跑到真正的")
        out("    可执行文件，模型目录不会自动刷新。请把它加进 PATH：")
        out(f'      echo \'export PATH="$HOME/.local/bin:$PATH"\' >> ~/.bashrc')
    return target, real


def run_install_wrapper(args, rep: detect.Report) -> int:
    rule("codex 包装脚本（自动刷新）")
    if args.uninstall_wrapper:
        if codex_mod.uninstall_wrapper():
            out(f"  已删除 {codex_mod.wrapper_path()}")
        else:
            out(f"  {codex_mod.wrapper_path()} 处没有本工具写的包装脚本")
        return 0

    if args.real_codex:
        real = Path(args.real_codex).expanduser()
        # `is_file()` is False for a symlink in some Windows toolchains and for
        # a script with no read bit; what actually matters is that exec works.
        if not os.access(real, os.X_OK):
            raise SystemExit(f"  ! {real} 不可执行")
        codex_mod.wrapper_path().parent.mkdir(parents=True, exist_ok=True)
        target = codex_mod.install_wrapper(real, ttl=args.refresh_ttl)
        out(f"  已写入 {target}")
        out(f"    实际执行 {real}")
        return 0

    result = install_wrapper(args)
    return 0 if result else 1


# --------------------------------------------------------------------------- #
# --sync: the fast path the wrapper calls
# --------------------------------------------------------------------------- #

def run_sync(args, rep: detect.Report) -> int:
    """Refresh the catalog if it is stale. Always returns 0, never raises.

    This runs before every `codex` launch, so the contract is: leave the machine
    in a usable state and get out of the way. A failure here means the user gets
    the catalog from last time, which is exactly what should happen -- there is
    no version of "the gateway is unreachable" that should stop codex starting.
    """
    cfg = rep.codex_config or detect.codex_config()
    base = codex_mod.saved_base(cfg)
    key = codex_mod.saved_key(cfg)
    if not base or not key:
        # Not configured yet -- `--setup` has not been run. Nothing to do, and
        # saying so on every launch would be noise.
        return 0

    age = codex_mod.last_refresh_age(time.time())
    ttl = 0 if args.force else args.refresh_ttl
    if age is not None and age < ttl:
        return 0

    try:
        models = gateway.list_models(base, key, timeout=SYNC_TIMEOUT)
    except gateway.GatewayError as e:
        note(f"  ! 模型目录未刷新（{e}）；继续使用上一次的目录")
        return 0

    if not models:
        return 0

    path = catalog_target(cfg)
    try:
        slugs = codex_mod.write_catalog(path, models)
    except (ValueError, OSError) as e:
        note(f"  ! 无法写入模型目录：{e}")
        return 0

    codex_mod.apply_catalog(cfg, path)
    codex_mod.mark_refreshed(models, time.time())

    # A pinned model that the gateway no longer serves would fail on the first
    # prompt with no explanation. Repoint it at something that exists.
    current = codex_mod.saved_model(cfg)
    if current and current not in slugs:
        fallback = next((s for s in slugs), None)
        note(f"  ! {current!r} 网关已不再提供；改用 {fallback!r}")
        if fallback:
            codex_mod.apply_config(cfg, base, fallback)

    note(f"  模型目录已刷新：{len(slugs)} 个模型")

    # Only reached when a refresh was actually due (the wrapper short-circuits
    # the throttled case before we are ever exec'd), so paying for one
    # `codex --version` here is cheap. A binary older than the supported range
    # writes a catalog its own parser will reject, and the user would otherwise
    # meet that as an unexplained startup failure.
    ok, verdict = codex_mod.version_note(detect.codex_version())
    if not ok:
        note(f"  ! {verdict}")
    return 0


# --------------------------------------------------------------------------- #
# --status
# --------------------------------------------------------------------------- #

def run_status(rep: detect.Report, args) -> int:
    out(rep.render())
    cfg = rep.codex_config or detect.codex_config()

    rule("config.toml")
    if not cfg.exists():
        out(f"  {cfg} 不存在 —— 请先跑一遍向导")
        return 0
    out(f"  {cfg}")
    table = codex_mod.provider_table()
    doc = TomlFile(cfg)
    for key in ("model", "model_provider", codex_mod.CATALOG_KEY):
        value = doc.get(key)
        out(f"    {key:22} {value if value is not None else '(未设置)'}")
    for key in ("name", "base_url", "wire_api", "requires_openai_auth",
                "env_key", "experimental_bearer_token"):
        value = doc.get(key, table=table)
        if value is None:
            continue
        if key == "experimental_bearer_token":
            value = _mask(value.strip("'\""))
        out(f"    [{table}] {key} = {value}")

    override = doc.get(codex_mod.REASONING_OVERRIDE_KEY)
    if override is not None:
        out()
        out(f"  ! 根级配置 {codex_mod.REASONING_OVERRIDE_KEY} = {override}")
        out("    它会覆盖目录里每个模型的 default_reasoning_level。")

    # NOT `return 0` on the unset case. Every section below is a fact about a
    # different file, so a missing catalog must not suppress the report -- and the
    # ordering workaround above (print search early) is the evidence: it was added
    # when an EARLIER section got swallowed here, and it did not stop the next one
    # from being swallowed too.
    rule("通过 MCP 搜索")
    url = search_mod.codex_configured(cfg)
    if not url:
        out("  未配置 —— Codex 自带的 web search 是托管工具，本网关无法执行，")
        out("  所以模型没有搜索能力。见 --configure-search。")
    else:
        token = search_mod.codex_saved_token(cfg)
        out(f"  {search_mod.DEFAULT_NAME:8} {url}")
        out(f"           Authorization {'Bearer ' + _mask(token) if token else '(缺失！)'}")

    rule("模型目录")
    cat = codex_mod.existing_catalog_path(cfg)
    if not cat:
        out("  model_catalog_json 未设置 —— `/model` 会列出 OpenAI 的模型，")
        out("  而本网关一个都不提供。")
        out("      修复：--refresh-models")
    else:
        slugs = codex_mod.read_catalog(Path(cat))
        out(f"  {cat}")
        out(f"    {len(slugs)} 个模型：{', '.join(slugs) if slugs else '(空)'}")
        age = codex_mod.last_refresh_age(time.time())
        out(f"    上次刷新："
            f"{f'{int(age)} 秒前' if age is not None else '本工具从未刷新过'}")

    rule("登录状态")
    state, detail = codex_auth.status()
    out(f"  {_auth_state(state)}  （{detail}）")

    rule("模型能力配置")
    # The file beside the script, not the catalog: the catalog is derived from it,
    # so showing where the user EDITS matters more than showing the output.
    store = modelconfig.store_path()
    if not store.exists():
        out(f"  {store}  缺失 —— 运行 --refresh-models 重新生成")
    else:
        out(f"  {store}")
        for slug in sorted(modelconfig.slugs()):
            e = modelconfig.entry(slug)
            mods_txt = ", ".join(str(m) for m in (e.get(modelconfig.FIELD_MODALITIES) or [])) or "-"
            lv = e.get(modelconfig.FIELD_LEVELS)
            lv_txt = ", ".join(str(x) for x in lv) if isinstance(lv, list) else "-"
            ctx = e.get(modelconfig.FIELD_CONTEXT)
            out(f"    {slug:28} {mods_txt:14} 上下文={ctx if ctx is not None else '-':<8}"
                f" 思考档位=[{lv_txt}] 默认={e.get(modelconfig.FIELD_DEFAULT_LEVEL) or '-'}")
        probs = model_config_problems()
        for p in probs:
            out(f"  有问题  {p}")
        out(f"  （编辑它，然后运行 --apply-model-config，把改动同步到"
            f" {modelconfig.catalog_hint()}）")

    rule("按模型覆盖（旧存储）")
    levels = reasoning.load()
    mods = modalities.load()
    if not levels and not mods:
        out("  无 —— 取值来自 model-config.jsonc 与内置种子")
    # Say which of these are actually IN EFFECT. The config file outranks them, so
    # listing a value without that check can state the opposite of the truth: an
    # old-store `low` next to a config-file `low,high,max` reads as "this model
    # offers one rung" when it offers three.
    for m, entry in sorted(levels.items()):
        names = ", ".join(lv["effort"] for lv in (entry.get("levels") or [])) or "（无）"
        shadowed = modelconfig.configured(m, modelconfig.FIELD_LEVELS) is not None
        out(f"  思考档位  {m:28} {names}  默认={entry.get('default') or '-'}"
            f"{'   <- 已忽略，model-config.jsonc 里配了这个模型' if shadowed else ''}")
    for m, entry in sorted(mods.items()):
        shadowed = modelconfig.configured(m, modelconfig.FIELD_MODALITIES) is not None
        out(f"  输入类型  {m:28} "
            f"{', '.join(modalities.normalize_entries(entry.get('input_modalities')))}"
            f"{'   <- 已忽略，model-config.jsonc 里配了这个模型' if shadowed else ''}")

    rule("codex 包装脚本")
    w = codex_mod.wrapper_path()
    if codex_mod.wrapper_is_ours(w):
        out(f"  {w}  （已安装）")
        out(f"  在 PATH 里：{'是' if codex_mod.path_has_wrapper_dir() else '否 —— 见 --install-wrapper'}")
    else:
        out("  未安装 —— 只有手动运行 --sync 时模型目录才会更新")
    return 0


def run_restore(rep: detect.Report) -> int:
    rule("恢复")
    cfg = rep.codex_config or detect.codex_config()

    # Read the catalog facts BEFORE restoring: restore_from_backup rewrites
    # config.toml, so afterwards the key is usually gone.
    owns_catalog = codex_mod.catalog_is_ours(cfg)
    catalog_file = codex_mod.existing_catalog_path(cfg)

    if codex_mod.restore_from_backup(cfg):
        out(f"  已从 {cfg.name}.bak 恢复 {cfg}")
    else:
        out(f"  {cfg} 没有备份 —— 保持原样")

    if cfg.exists() and codex_mod.clear_catalog(cfg):
        out(f"  已从 {cfg} 删除 {codex_mod.CATALOG_KEY}")
    if owns_catalog and catalog_file and codex_mod.remove_catalog_file(Path(catalog_file)):
        out(f"  已删除生成的模型目录 {catalog_file}")

    if codex_auth.restore_from_backup():
        out(f"  已从 .bak 恢复 {codex_auth.auth_path()}")

    if codex_mod.uninstall_wrapper():
        out(f"  已删除包装脚本 {codex_mod.wrapper_path()}")

    out("  （按模型的 reasoning/modalities 覆盖仍保留 —— 需要清空请用")
    out("   --reasoning-clear all / --modalities-clear all）")
    return 0


# --------------------------------------------------------------------------- #
# gateway-side model parameters
# --------------------------------------------------------------------------- #

def render_report(rep: litellm_admin.Report) -> None:
    if rep.ok:
        out()
        out(f"  已接受 Codex 的 Responses 字段（{len(rep.ok)} 个）：")
        for p in rep.ok:
            out(f"      {p.model_name}")
    if rep.skipped:
        out()
        out(f"  不适用（{len(rep.skipped)} 个）：")
        for p in rep.skipped:
            out(f"      {p.model_name:28} {p.reason}")
    if rep.todo:
        out()
        out(f"  需要修复（{len(rep.todo)} 个）—— Codex 的请求打到这些模型会失败：")
        for p in rep.todo:
            out(f"      {p.model_name:28} {litellm_admin.describe(p)}")


def run_gateway_config(args, rep: detect.Report, *, apply: bool) -> int:
    rule("网关侧模型参数")
    out("  Codex 只说 /v1/responses，且每一轮都会带两个 custom_openai 模型定义")
    out("  默认拒绝的字段：")
    out(f"    reasoning.effort  -> {litellm_admin.REASONING_PARAM}，被当成")
    out("                         不支持的参数拒绝 -> HTTP 400")
    out(f"    {litellm_admin.CLIENT_METADATA_PARAM}   -> 被转发进 OpenAI SDK，")
    out("                         而 SDK 没有这个关键字参数 -> HTTP 500")
    out()
    out("  这两个问题都要靠 litellm_params 里的 allowed_openai_params 和")
    out(f"  {litellm_admin.DROP_PARAMS_KEY} 解决。Codex 二进制本身无法发送它们，")
    out("  所以只能配置在网关上。")
    out()

    base, key = gateway_credentials(args)
    endpoint = gateway.normalize_base(base, keep_v1=False) + litellm_admin.INFO_PATH
    out(f"  模型定义端点：{endpoint}")
    try:
        entries = litellm_admin.fetch_model_info(base, key)
    except gateway.GatewayError as e:
        raise SystemExit(f"  ! {e}")
    report = litellm_admin.build_report(entries)
    out(f"  网关连接正常：{len(entries)} 个模型定义")
    render_report(report)

    if not report.todo:
        out()
        out("  无需处理 —— 所有私有模型都已接受 Codex 的字段。")
        return 0

    if not apply:
        out()
        out("  未做任何改动。请在能访问网关的地方运行这些命令：")
        out()
        out(litellm_admin.emit_commands(base, report.todo))
        out()
        out("  ……或者加 --apply-gateway-config 重新运行，从这里直接发送。")
        out("  （POST /model/update 会整体替换 litellm_params，所以上面每条命令都会")
        out("   重发该模型的完整参数集，只多加了那一个键。）")
        out()
        out("  注意：这是共享网关 —— 应用后会改变所有用户的请求行为，")
        out("  不只影响你自己。")
        return 0

    if not args.yes and not confirm(
            f"  要更新 {base} 上的 {len(report.todo)} 个模型定义吗？", default=False):
        out("  已中止 —— 未发送任何内容")
        return 1

    out()
    out(f"  正在应用到 {len(report.todo)} 个模型 ...")
    failures = 0
    for p in report.todo:
        try:
            litellm_admin.apply_plan(base, key, p)
        except gateway.GatewayError as e:
            failures += 1
            out(f"      ! {p.model_name}: {e}")
        else:
            out(f"      成功 {p.model_name}")
    out()
    if failures:
        out(f"  {failures} 个模型更新失败；见上方输出。")
        return 1
    out("  完成。可用 --emit-gateway-config 验证（应当报告无需处理）。")
    return 0


# --------------------------------------------------------------------------- #
# reasoning levels
# --------------------------------------------------------------------------- #

def _configured_models(args, what: str) -> tuple[list[gateway.Model], str] | None:
    """Fetch the model list and resolve which model the user means."""
    models = fetch_models(*gateway_credentials(args), required=True)
    model = args.reasoning_model if what == "reasoning" else args.modalities_model
    if not model:
        if not is_tty():
            raise SystemExit(f"必须提供 --{what}-model（或在终端里交互运行）")
        label = "思考档位" if what == "reasoning" else "输入类型"
        model = pick_model(models, f"要配置哪个模型的{label}？")
    if model not in {m.id for m in models}:
        out(f"  ! 网关不提供 {model!r}；以下是它提供的模型：")
        for m in models:
            out(f"      {m.id}")
        return None
    return models, model


def run_configure_reasoning(args, rep: detect.Report) -> int:
    rule("按模型设置思考档位")

    if args.reasoning_clear:
        store = reasoning.load()
        if args.reasoning_clear == "all":
            n = reasoning.clear_all()
            out(f"  已清除 {n} 条覆盖")
            return 0
        m = args.reasoning_clear
        out(f"  {'已清除 ' + m if reasoning.clear(m) else m + ' 没有覆盖配置'}")
        return 0

    picked = _configured_models(args, "reasoning")
    if not picked:
        return 1
    models, model = picked

    raw = next((m.raw or {} for m in models if m.id == model), {})
    current_levels, current_default = reasoning.levels_for(model, raw)
    out()
    out(f"  {model}")
    if reasoning.is_openai_official(model):
        out("  这是 OpenAI 官方模型 —— 它的档位来自 Codex 内置目录，")
        out("  不能由用户配置。这里仍然展示一下：")
        for lv in current_levels:
            out(f"      {lv['effort']:12} {lv['description']}")
        return 0
    out(f"  当前：{', '.join(lv['effort'] for lv in current_levels) or '(无)'}"
        f"  默认={current_default or '-'}")
    out(f"  简写预设：private={','.join(reasoning.PROFILES['private'])}"
        f"（默认）  xhigh={','.join(reasoning.PROFILES['xhigh'])}  none=（无档位菜单）")

    if args.reasoning_levels is not None:
        wanted = list(reasoning.resolve_profile(args.reasoning_levels))
    else:
        out()
        out(f"  可选档位：{', '.join(reasoning.KNOWN_EFFORTS)}")
        out("  （逗号分隔；填 `none` 或直接回车表示没有思考档位菜单）")
        answer = ask("档位",
                     default=",".join(lv["effort"] for lv in current_levels)
                     or ",".join(reasoning.DEFAULT_EFFORTS))
        wanted = list(reasoning.resolve_profile(answer))

    unknown = [x for x in wanted if x not in reasoning.KNOWN_EFFORTS]
    if unknown:
        out(f"  ! 不是 Codex 认的思考档位：{', '.join(unknown)}")
        out(f"    可选值：{', '.join(reasoning.KNOWN_EFFORTS)}")
        return 1

    default = args.reasoning_default
    if wanted and not default:
        if is_tty() and not args.yes:
            default = ask("默认档位", default=current_default or reasoning.DEFAULT_EFFORT
                          if (current_default in wanted or reasoning.DEFAULT_EFFORT in wanted)
                          else wanted[0])
        else:
            default = reasoning.DEFAULT_EFFORT if reasoning.DEFAULT_EFFORT in wanted else wanted[0]

    try:
        entry = reasoning.configure(model, wanted, default)
    except ValueError as e:
        out(f"  ! {e}")
        return 1

    levels = entry.get("levels") or []
    out()
    out(f"  已写入 {reasoning.store_path()}")
    if not levels:
        out(f"    {model}: 无思考档位菜单")
    else:
        out(f"    {model}: {', '.join(lv['effort'] for lv in levels)}"
            f"  默认={entry.get('default')}")
    out()
    out("  下一步：运行 --refresh-models 重新生成目录（或者直接运行 codex，")
    out("        装好 --install-wrapper 后它会自动帮你刷新）。")
    return 0


# --------------------------------------------------------------------------- #
# input modalities
# --------------------------------------------------------------------------- #

def run_configure_modalities(args, rep: detect.Report) -> int:
    rule("按模型设置输入类型（图片）")

    if args.modalities_clear:
        if args.modalities_clear == "all":
            store = modalities.load()
            for m in list(store):
                modalities.clear(m)
            out(f"  已清除 {len(store)} 条覆盖")
            out("  （被清除的模型会依次回退到 model-config.jsonc、再用内置种子 ——")
            out("    不会自动变成只支持文本，除非种子就是这么写的）")
            return 0
        m = args.modalities_clear
        out(f"  {'已清除 ' + m if modalities.clear(m) else m + ' 没有覆盖配置'}")
        out("  （被清除的模型会依次回退到 model-config.jsonc、再用内置种子 ——")
        out("    不会自动变成只支持文本，除非种子就是这么写的）")
        return 0

    picked = _configured_models(args, "modalities")
    if not picked:
        return 1
    models, model = picked

    raw = next((m.raw or {} for m in models if m.id == model), {})
    current = modalities.modalities_for(model, raw)
    out()
    out(f"  {model}")
    if modalities.is_openai_official(model):
        out("  这是 OpenAI 官方模型 —— 它的输入类型来自 Codex 内置目录，")
        out("  不能由用户配置。这里仍然展示一下：")
        out(f"      {', '.join(current)}")
        return 0
    out(f"  当前：{', '.join(current)}")

    if args.modalities is not None:
        wanted = args.modalities
    else:
        out()
        out(f"  可选类型：{', '.join(modalities.KNOWN_MODALITIES)}")
        out("  只有模型真的能看图才加 'image' —— 接口不报错并不代表能看懂，")
        out("  请用 --probe-modalities 实测")
        wanted = ask("输入类型", default=",".join(current))

    try:
        chosen = modalities.configure(model, wanted)
    except ValueError as e:
        out(f"  ! {e}")
        return 1

    out()
    out(f"  已写入 {modalities.store_path()}")
    out(f"    {model}: {', '.join(chosen)}")
    out()
    out("  下一步：运行 --refresh-models 重新生成目录。")
    return 0


# Vision inference is slower than text, and these backends queue.
PROBE_TIMEOUT = 180.0


def run_probe_modalities(args, rep: detect.Report) -> int:
    rule("实测哪些模型真的能看图")
    out("  分别发送纯红、纯蓝两张图片，让每个模型说出颜色。接口不报错并不能")
    out("  说明问题 —— 模型可能返回 200 却描述一个根本不存在的颜色 —— 所以")
    out("  真正的判据是它能不能把两张图区分开。")
    out()

    base, key = gateway_credentials(args)
    models = fetch_models(base, key, required=True)
    names = args.probe_models.split(",") if args.probe_models else None
    targets = [m.id for m in models
               if m.is_chat_capable and (not names or m.id in names)]
    if not targets:
        out("  ! 没有可探测的对话模型")
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
            out(f"  {name:26} 失败   {failure}")
            continue

        can_see, why = modalities.judge(answers)
        verdicts[name] = can_see
        said = " / ".join(f"{c}={answers.get(c, '')[:24]!r}" for c in ("red", "blue"))
        out(f"  {name:26} {'图片' if can_see else '文本':6} {said}")
        out(f"  {'':26} {why}")

    out()
    can = [n for n, v in verdicts.items() if v]
    cannot = [n for n, v in verdicts.items() if not v]
    out(f"  能看图（{len(can)}）：{', '.join(can) or '(无)'}")
    out(f"  不能（{len(cannot)}）：{', '.join(cannot) or '(无)'}")
    if can:
        # Paste-ready, not written back. The verdict is worth having as text:
        # writing it ourselves would mean re-encoding a nested entry through
        # JsoncFile, which drops every comment in it. The user owns that file.
        out()
        out(f"  要启用，请把下面这些条目粘贴到 {modelconfig.store_path()}")
        out(f"  （或直接修改已有条目），然后运行："
            f" python3 {self_cmd()} --apply-model-config")
        out()
        for n in can:
            out(f'      "{n}": {{ "input_modalities": ["text", "image"] }},')
    return 0


# --------------------------------------------------------------------------- #
# search (SearXNG over MCP)
# --------------------------------------------------------------------------- #

def search_endpoint(args, rep) -> tuple[str, str | None]:
    """(url, token) for the MCP server, derived from the gateway config.

    The MCP server is served BY the gateway, so both halves come from the same
    place the model config does: the URL is the gateway base plus the fixed MCP
    path, and the token IS the LiteLLM key. That is what removes the second key,
    and it is also why nothing here is deployment-specific -- the address is
    whatever `--api-base` resolved to, not a constant.

    Deliberately does NOT call `resolve_credentials`: that one raises SystemExit
    when it cannot find a base, which is right for the model config (nothing
    works without it) and wrong here. Search is an add-on, so a missing key must
    not fail an otherwise successful run.
    """
    cfg = rep.codex_config or detect.codex_config()

    # Saved config first: in the main flow the wizard has just written it, so
    # this picks up exactly the key in use. `args` is the last resort for the
    # standalone `--configure-search` case on a machine with no config yet.
    base = codex_mod.saved_base(cfg) or args.api_base
    key = codex_mod.saved_key(cfg) or args.api_key

    # `mcp_url` strips a trailing `/v1` itself -- the saved base is the model
    # endpoint, the MCP route is not under `/v1`.
    url = args.search_url or (search_mod.mcp_url(base) if base
                              else search_mod.DEFAULT_URL)

    # Precedence matters: --search-token is the explicit escape hatch for a
    # backend reached directly (the pre-gateway layout), and $MCP_SEARXNG_TOKEN
    # deliberately comes AFTER the gateway key so a stale variable left exported
    # in someone's shell cannot silently shadow the normal path.
    token = args.search_token or key or os.environ.get(search_mod.TOKEN_ENV) or None
    return url, token


def render_probe(p: search_mod.Probe) -> None:
    if not p.ok:
        out(f"  无法连接 —— {p.detail}")
        return
    out(f"  {p.server} {p.version}  （协议 {p.protocol}）")
    if p.detail:
        out(f"  ! {p.detail}")
    for name, desc in p.tools:
        out(f"    {name:24} {desc}")
    missing = search_mod.missing_expected(p.tools)
    if missing:
        out(f"  ! 服务端没有提供：{', '.join(missing)}")
        out("    模型将拿不到该工具 —— 请检查 MCP_SEARXNG_IMAGE")


def run_configure_search(args, rep, *, optional: bool = False) -> int:
    """Point the Codex CLI at the gateway's search endpoint.

    `optional` is how the main flow calls it: search is an add-on, so not being
    able to write it must not turn an otherwise successful run into a failure.
    The standalone `--configure-search` keeps `optional=False` so the exit code
    still reports the problem when that is the only thing the user asked for.
    """
    rule("通过 MCP 搜索")
    cfg = rep.codex_config or detect.codex_config()
    if not cfg.exists():
        if optional:
            out(f"  已跳过 —— 还没有 {cfg}")
            return 0
        out(f"  ! 没有 {cfg} —— 请先跑一遍向导把它创建出来")
        return 1

    url, token = search_endpoint(args, rep)
    if not token:
        if optional:
            out("  已跳过 —— 没有可复用的网关密钥。请先完成模型配置，")
            out("  或者对直连后端传 --search-token。")
            return 0
        if not is_tty():
            out("  ! 没有可写入的 token —— LiteLLM 密钥就是搜索 token，")
            out("    请传 --api-key（直连后端则传 --search-token）。")
            return 1
        token = ask("LiteLLM API 密钥（同时作为 MCP bearer token）",
                    secret=True)

    snapshot([cfg])
    search_mod.codex_write(cfg, url, token)
    out(f"  已写入 [mcp_servers.{search_mod.DEFAULT_NAME}] -> {url}")
    out()
    out("  Codex 启动时读取这个配置，请重启它。之后 `/mcp` 应当列出")
    out(f"  {search_mod.DEFAULT_NAME}，模型也就获得了 "
        f"{', '.join(search_mod.EXPECTED_TOOLS)}.")
    out()
    out("  正在校验服务端本身：")
    render_probe(search_mod.probe(url, token))
    return 0


def run_clear_search(rep) -> int:
    rule("通过 MCP 搜索")
    cfg = rep.codex_config or detect.codex_config()
    removed = search_mod.codex_clear(cfg) if cfg.exists() else []
    if not removed:
        out(f"  {cfg} 里没有需要移除的内容")
        return 0
    out(f"  已从 {cfg} 移除：{', '.join(removed)}")
    out("  重启 Codex 后生效。")
    return 0


def run_check_search(args, rep) -> int:
    rule("通过 MCP 搜索")
    cfg = rep.codex_config or detect.codex_config()
    configured = search_mod.codex_configured(cfg)
    derived_url, token = search_endpoint(args, rep)
    # Prefer what is on disk -- that is what Codex will send, and a mismatch with
    # the derived value is exactly the drift worth seeing.
    url = args.search_url or configured or derived_url
    if args.search_url is None and configured is None:
        out(f"  ! {cfg} 里未配置 —— 仍然检查默认地址")
    # A stale entry survives a gateway move and then shows up as a connection
    # error rather than as a config error.
    if args.search_url is None and configured and derived_url != configured:
        out(f"  ! 配置的 URL 与当前网关端点不同（{derived_url}）")
        out("    重新运行 --configure-search 可以迁移过去")
    out(f"  {url}")
    out(f"  Authorization   {'Bearer ' + _mask(token) if token else '(无)'}")
    render_probe(search_mod.probe(url, token))
    return 0


# --------------------------------------------------------------------------- #
# refresh / switch
# --------------------------------------------------------------------------- #

def run_refresh(args, rep: detect.Report) -> int:
    """Re-fetch the gateway list and rewrite only the catalog JSON."""
    rule("刷新模型目录")
    cfg = rep.codex_config or detect.codex_config()
    base, key = gateway_credentials(args)
    models = fetch_models(base, key, required=True)

    if not cfg.exists():
        out(f"  ! 没有 {cfg} —— 请先跑一遍向导把它创建出来")
        return 1
    snapshot([cfg])
    write_catalog(cfg, models)
    strip_blocking_keys(cfg)
    out()
    out("  下一步：运行 `codex` —— `/model` 会读到这个模型目录。")
    return 0


# --------------------------------------------------------------------------- #
# model-config.jsonc: the second step
# --------------------------------------------------------------------------- #

def model_config_problems() -> list[str]:
    """Everything wrong with the user's `model-config.jsonc`, in plain words.

    Two kinds, deliberately reported together: shape problems (from modelconfig,
    which owns the schema) and values outside the enums Codex accepts (known only
    here, because the enums live in `modalities` / `reasoning` and modelconfig must
    not import them -- they import it).

    Reported, never fatal. A value outside the enum is DROPPED by the resolver and
    falls back down the chain, so the catalog stays parseable no matter what the
    user typed. Refusing to proceed would be worse than degrading: the fallback is
    safe, and a hard stop leaves them with no model list at all.
    """
    problems = list(modelconfig.structural_problems())
    known_mods = set(modalities.KNOWN_MODALITIES)
    known_efforts = set(reasoning.KNOWN_EFFORTS)

    for slug, entry in modelconfig.load().items():
        if modelconfig.is_reserved(str(slug)) or not isinstance(entry, dict):
            continue
        mods = entry.get(modelconfig.FIELD_MODALITIES)
        if isinstance(mods, list):
            for m in mods:
                if str(m) not in known_mods:
                    problems.append(
                        f"{slug}.{modelconfig.FIELD_MODALITIES}: {m!r} 不是 Codex 认的值"
                        f"（只有 {'/'.join(modalities.KNOWN_MODALITIES)}）"
                    )
        levels = entry.get(modelconfig.FIELD_LEVELS)
        if isinstance(levels, list):
            for lv in levels:
                if str(lv) not in known_efforts:
                    problems.append(
                        f"{slug}.{modelconfig.FIELD_LEVELS}: {lv!r} 不是 Codex 认的档位"
                        f"（见 reasoning.KNOWN_EFFORTS）"
                    )
    return problems


def run_apply_model_config(args, rep: detect.Report) -> int:
    """Apply an edited `model-config.jsonc`: validate, re-resolve, rewrite the catalog.

    This is the second half of the two-step flow. Step one (install or
    `--refresh-models`) generated the file and applied its own suggestions, so the
    system already works; this is for the edits the user made afterwards.
    """
    rule("应用模型能力配置")
    path = modelconfig.store_path()
    if not path.exists():
        out(f"  ! {path} 不存在")
        out(f"    它由安装流程生成：python3 {self_cmd()} "
            f"--refresh-models")
        return 1
    out(f"  {path}")

    problems = model_config_problems()
    if problems:
        out(f"  ! 有 {len(problems)} 个问题 —— 非法值会被忽略，其余配置")
        out("    仍会生效（Codex 不认的值不能写进目录：它会让整个文件无法解析，")
        out("    Codex 直接启动不了）")
        for p in problems:
            out(f"      {p}")
    else:
        listed = [s for s in modelconfig.slugs()]
        out(f"  已配置 {len(listed)} 个模型：{', '.join(listed) or '(无)'}")

    cfg = rep.codex_config or detect.codex_config()
    if not cfg.exists():
        out(f"  ! 没有 {cfg} —— 请先跑一遍向导把它创建出来")
        return 1
    base, key = gateway_credentials(args)
    models = fetch_models(base, key, required=True)

    snapshot([cfg])
    # Top up first: a model the gateway gained since the file was generated should
    # get an entry rather than silently resolving from defaults forever.
    write_catalog(cfg, models)
    strip_blocking_keys(cfg)

    out()
    out("  模型目录现在已反映该文件的内容。如果运行时没生效，那是个 bug ——")
    out("  `--status` 会显示每个模型最终生效的值。")
    out("  下一步：运行 `codex`，然后打开 `/model`。")
    return 0


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    global QUIET

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
        epilog=EXAMPLES
        + f"\n适配版本\n    codex-cli {codex_mod.target_version()}"
          f"  （模型目录仍兼容 {'.'.join(map(str, codex_mod.MIN_SUPPORTED_VERSION))}"
          f"+；更新的版本可能需要重新适配）\n",
    )
    ap.add_argument("-h", "--help", action="help", help="显示本帮助后退出")
    ap._optionals.title = "选项"
    # 让 argparse 自带文案也显示为中文（usage / 缺参 / 无效选项等）
    _argparse_zh = {
        "usage: ": "用法： ",
        "the following arguments are required: %s": "缺少必需参数：%s",
        "unrecognized arguments: %s": "无法识别的参数：%s",
        "invalid choice: %(value)r (choose from %(choices)s)":
            "无效选项：%(value)r（可选值：%(choices)s）",
        "argument %(argument_name)s: %(message)s": "参数 %(argument_name)s：%(message)s",
        "expected one argument": "需要一个值",
        "expected at most one argument": "最多只能有一个值",
        "expected at least one argument": "至少需要一个值",
        "not allowed with argument %s": "不能与参数 %s 同时使用",
    }
    argparse._ = lambda s: _argparse_zh.get(s, s)
    ap.add_argument("--api-base", help="网关地址（API base URL），如 http://10.18.219.156:4000")
    ap.add_argument("--api-key", help="网关 API 密钥，如 sk-XXXXXXXX")
    ap.add_argument("--model", help="要固定的模型 id（见 --list-models）")
    ap.add_argument("--list-models", action="store_true",
                    help="打印网关提供的所有模型后退出")
    ap.add_argument("--switch-model", action="store_true",
                    help="交互式选择一个模型并写入 config.toml")
    ap.add_argument("--refresh-models", action="store_true",
                    help="重新拉取网关模型列表，只重写模型目录")
    ap.add_argument("--apply-model-config", action="store_true",
                    help=f"重新读取你编辑过的 {modelconfig.STORE_FILENAME} "
                         f"并同步到实际生效的目录；隐含 --refresh-models")
    ap.add_argument("--sync", action="store_true",
                    help="目录过期时静默刷新；永不失败"
                         "（安装的 codex 包装脚本就是跑这个）")
    ap.add_argument("--detect", action="store_true",
                    help="显示各配置文件位置后退出")
    ap.add_argument("--status", action="store_true",
                    help="显示当前配置后退出")
    ap.add_argument("--restore", action="store_true",
                    help="还原 .bak 文件、删除模型目录、移除包装脚本")
    ap.add_argument("--yes", "-y", action="store_true", help="不提问，直接用默认值")
    ap.add_argument("--quiet", "-q", action="store_true",
                    help="抑制进度输出（错误仍会打印）")

    ap.add_argument("--use-env-key", action="store_true",
                    help="把密钥放进环境变量，而不是内联写在 config.toml 里。"
                         "这里默认内联：非交互式 shell（docker exec、cron）不会加载 "
                         "~/.bashrc，根本看不到环境变量")
    ap.add_argument("--env-key", help="保存密钥的环境变量名")
    ap.add_argument("--provider-id", default=codex_mod.DEFAULT_PROVIDER_ID,
                    help="provider 配置表 id（默认：private）")
    ap.add_argument("--profile",
                    help="写入 [profiles.<name>]，而不是根配置")
    ap.add_argument("--skip-probe", action="store_true",
                    help="跳过 /v1/responses 连通性探测")
    ap.add_argument("--skip-login", action="store_true",
                    help="不写 auth.json（Codex 会要求登录）")
    ap.add_argument("--strip-openai-keys", action="store_true",
                    help="从 config.toml 删除 service_tier 等 OpenAI 专有键")

    ap.add_argument("--fix-login", action="store_true",
                    help="写入 auth.json，让 Codex 不再弹登录提示"
                         "（配 --restore 可撤销，配 --force 可强制重写）")
    ap.add_argument("--force", action="store_true",
                    help="即使已存在登录信息也重写 auth.json；同时让 "
                         "--sync 忽略节流")

    ap.add_argument("--install-wrapper", action="store_true",
                    help="安装 ~/.local/bin/codex 包装脚本，每次启动前刷新模型目录")
    ap.add_argument("--uninstall-wrapper", action="store_true",
                    help="移除 --install-wrapper 安装的包装脚本")
    ap.add_argument("--real-codex",
                    help="包装脚本应执行的 codex 可执行文件（自动找不到时指定）")
    ap.add_argument("--refresh-ttl", type=int, default=codex_mod.REFRESH_TTL_SECONDS,
                    help=f"刷新后的模型目录可信任多少秒"
                         f"（默认：{codex_mod.REFRESH_TTL_SECONDS}）")

    ap.add_argument("--emit-gateway-config", action="store_true",
                    help="打印 /model/update 调用，让私有模型接受 "
                         "Codex 的 reasoning_effort 并丢弃 client_metadata")
    ap.add_argument("--apply-gateway-config", action="store_true",
                    help="同上，但真正发送（需要 LiteLLM 主密钥）")

    ap.add_argument("--configure-reasoning", action="store_true",
                    help="设置某个私有模型提供哪些思考档位")
    ap.add_argument("--reasoning-model", help="要配置思考档位的模型 id")
    ap.add_argument("--reasoning-levels",
                    help="逗号分隔的档位（如 low,high,max），或简写："
                         "private / three / xhigh / none；空字符串表示没有思考档位菜单")
    ap.add_argument("--reasoning-default",
                    help="默认档位（必须是 --reasoning-levels 之一）")
    ap.add_argument("--reasoning-clear", metavar="MODEL",
                    help="删除 MODEL 的覆盖配置，`all` 表示所有模型")

    ap.add_argument("--configure-modalities", action="store_true",
                    help="设置某个私有模型接受哪些输入类型（文本/图片）")
    ap.add_argument("--modalities-model", help="要配置输入类型的模型 id")
    ap.add_argument("--modalities",
                    help="逗号分隔的类型，如 text,image；只写 `text` 会禁止该模型接收附件")
    ap.add_argument("--modalities-clear", metavar="MODEL",
                    help="删除 MODEL 的覆盖配置，`all` 表示所有模型")
    ap.add_argument("--probe-modalities", action="store_true",
                    help="实测哪些网关模型真的能看图：让它们分辨红色和蓝色")
    ap.add_argument("--probe-models",
                    help="要探测的模型 id，逗号分隔（默认：所有对话模型）")

    ap.add_argument("--configure-search", action="store_true",
                    help="单独把搜索 MCP 服务写进 config.toml 后退出。"
                         "正常安装流程已经会做，这里只用于单独重跑或修复")
    ap.add_argument("--no-search", action="store_true",
                    help="跳过正常安装流程本会执行的搜索配置步骤")
    ap.add_argument("--search-url",
                    help="覆盖 MCP 端点（默认：网关地址 + /searxng/mcp）")
    ap.add_argument("--search-token",
                    help="覆盖 MCP bearer token（默认：本次配置使用的 LiteLLM "
                         f"密钥；直连后端时 ${search_mod.TOKEN_ENV} 是最后的兜底）")
    ap.add_argument("--search-clear", action="store_true",
                    help="再次删除 MCP 搜索配置块")
    ap.add_argument("--check-search", action="store_true",
                    help="与 MCP 服务握手并列出模型能获得的工具；会解释 401/403/404")

    args = ap.parse_args(argv)
    # --sync is what the `codex` wrapper runs, so it is silent by default -- but
    # `--sync --force` is how a human debugs it, and that one should say what it
    # did.
    QUIET = args.quiet or (args.sync and not args.force)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    rep = detect.discover(probe_version=not args.sync)

    # --sync is the only command that must never raise: it is on the critical
    # path of `codex` starting. Everything else is allowed to fail loudly.
    if args.sync:
        try:
            return run_sync(args, rep)
        except Exception as e:  # noqa: BLE001
            note(f"  ! 已跳过目录同步（{type(e).__name__}: {e}）")
            return 0

    if args.detect:
        out(rep.render())
        return 0
    if args.status:
        return run_status(rep, args)
    if args.restore:
        return run_restore(rep)
    if args.strip_openai_keys:
        cfg = rep.codex_config or detect.codex_config()
        removed = codex_mod.strip_openai_only_keys(cfg)
        out(f"  已从 {cfg} 删除：{', '.join(removed) if removed else '（无）'}")
        return 0
    if args.install_wrapper or args.uninstall_wrapper:
        return run_install_wrapper(args, rep)

    # These touch no client file, so they must not drag in the wizard.
    if args.emit_gateway_config or args.apply_gateway_config:
        return run_gateway_config(args, rep, apply=args.apply_gateway_config)
    if args.fix_login:
        return run_fix_login(args, rep)
    if args.configure_search:
        return run_configure_search(args, rep)
    if args.search_clear:
        return run_clear_search(rep)
    if args.check_search:
        return run_check_search(args, rep)

    if args.configure_reasoning or args.reasoning_clear:
        return run_configure_reasoning(args, rep)
    if args.configure_modalities or args.modalities_clear:
        return run_configure_modalities(args, rep)
    if args.probe_modalities:
        return run_probe_modalities(args, rep)
    if args.apply_model_config:
        return run_apply_model_config(args, rep)
    if args.refresh_models:
        return run_refresh(args, rep)

    return run_setup(args, rep)


if __name__ == "__main__":
    sys.exit(main())
