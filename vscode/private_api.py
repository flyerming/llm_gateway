#!/usr/bin/env python3
"""把 Claude Code 与 Codex 的 VSCode 插件指向私有 API 网关。

    python private_api.py --target claude --api-base http://10.0.0.5:4000 --api-key sk-xxx
    python private_api.py --target codex  --api-base http://10.0.0.5:4000 --api-key sk-xxx
    python private_api.py --target both   --api-base http://10.0.0.5:4000 --api-key sk-xxx
    python private_api.py --detect          # 只显示各配置文件在哪里
    python private_api.py --status          # 查看当前配置
    python private_api.py --target codex --switch-model
    python private_api.py --target codex --refresh-models   # 重新生成模型目录
    python private_api.py --fix-login                       # 关掉 Codex 的登录提示
    python private_api.py --configure-reasoning --reasoning-model <id> \
        --reasoning-levels low,high,max --reasoning-default high
    python private_api.py --emit-gateway-config             # 打印网关侧修复命令
    python private_api.py --apply-gateway-config            # ……并真正发送
    python private_api.py --restore --target both

在终端里不带 `--target` 运行，会进入交互式向导。

会改动哪些文件
--------------
Claude Code：
  * `<editor>/User/settings.json`  -> `claudeCode.environmentVariables`
  * `~/.claude/settings.json`      -> `env` 以及 `modelPicker`；后者用网关自己的
                                      模型替换内置的 /model 列表（Opus/Sonnet/
                                      Haiku，私有网关并不提供）
  * 插件自带的 `claude.exe`         -> 硬编码的 `/(claude|anthropic)/i` 模型过滤
                                      规则（见 claude_patch.py）。已经在
                                      `modelPicker` 里整理好的模型不需要它；它只是
                                      为 `--keep-builtin-models` 保留，同时作为该
                                      设置被移除时的兜底。

Codex：
  * `~/.codex/config.toml`         -> `[model_providers.private]` + `model`
                                      + `model_catalog_json`
  * `~/.codex/gateway-models.json` -> 生成的模型目录。否则 Codex 的模型下拉框
                                      列的是 OpenAI 自家阵容（GPT-5.6 Sol/Terra/
                                      Luna、GPT-5.5……），网关一个都不提供；有了
                                      目录，就会用网关自己的模型整体替换该列表
  * `~/.codex/auth.json`           -> 仅在使用 `--fix-login` 时写入。无论
                                      `model_provider` 怎么配，Codex 都靠这个文件
                                      判断「已登录」；没有它，私有网关仍会弹出一
                                      个它无法完成的登录提示。里面存的是网关密钥，
                                      不是 OpenAI 的 —— 见 codex_auth.py
  * `model-config.jsonc` -> 模型能力配置：输入类型（文本/图片）、思考档位、
                                      上下文长度。首次 `--refresh-models` 时生成在
                                      本脚本旁边（不在 `~/.codex` 里），由实测种子
                                      与网关实时模型列表交叉得出，之后归你编辑。
                                      `--apply-model-config` 会把你的改动同步进
                                      模型目录。见 modelconfig.py
  * `~/.codex/private-reasoning.json` / `private-modalities.json` -> 旧的按模型
                                      存储，由 `--configure-reasoning` /
                                      `--configure-modalities` 写入。仍会读取，但
                                      `model-config.jsonc` 优先级更高；主流程已
                                      不再写入它们
  * `PRIVATE_API_KEY` 用户环境变量（或内联密钥）

网关本身：
  * 私有模型上的 `litellm_params.allowed_openai_params` 和
    `litellm_params.additional_drop_params` -> 仅在使用 `--apply-gateway-config`
    时改动。Codex 每一轮都会发送 `reasoning.effort`（LiteLLM 映射成
    `reasoning_effort`，而 `custom_openai` provider 会拒绝 -> HTTP 400）和
    `client_metadata`（LiteLLM 原样转发给 OpenAI SDK，而 SDK 没有这个关键字
    参数 -> HTTP 500）。两者缺一，针对私有模型的每一轮 Codex 请求都会失败。
    默认的 `--emit-gateway-config` 只打印命令，不发送。

写任何文件之前都会先备份成 `<file>.bak`。

目录结构
--------
这是入口脚本，位于 `vscode/` 顶层。它调用的模块在 `vscode/private-api/`；
那里没有需要直接运行的东西。`vscode/README.md` 是完整说明。
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
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
import modelconfig
import reasoning
import search as search_mod

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


def pick_model(models: list[gateway.Model], title: str = "选择一个模型") -> str:
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
        raw = ask("序号，或用于过滤的子串，或完整模型 id").strip()
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

def resolve_credentials(args, saved_base: str | None,
                        saved_key: str | None = None) -> tuple[str, str]:
    """Fill in --api-base / --api-key, prompting or showing examples as needed."""
    base, key = args.api_base, args.api_key

    # The gateway in the settings file is one this tool wrote on an earlier run,
    # so reusing it is the obvious default -- asking every time is just noise, and
    # it made `--refresh-models` unusable from anything but a terminal.
    if not base and saved_base:
        base = saved_base
        out(f"  在配置里找到了已有网关：{saved_base}")

    # The key for that gateway is usually already in the settings file this tool
    # wrote. Reusing it is what makes `--refresh-models` a one-liner -- but only
    # when the base is the same one, or it would silently point an old key at a
    # different gateway.
    if (not key and saved_key and saved_base and base
            and gateway.normalize_base(base, keep_v1=False)
            == gateway.normalize_base(saved_base, keep_v1=False)):
        key = saved_key
        out(f"  复用配置里已有的 API 密钥（{_mask(key)}）")

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


def fetch_models(base: str, key: str, *, required: bool) -> list[gateway.Model]:
    try:
        models = gateway.list_models(base, key)
    except gateway.GatewayError as e:
        if required:
            raise SystemExit(f"无法获取模型列表：{e}")
        out(f"  ! 无法获取模型列表（{e}）")
        out("    继续执行 —— 仍然可以把客户端指向该网关。")
        return []
    out(f"  网关连接正常：共提供 {len(models)} 个模型")
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
        out(f"  未打二进制补丁时 Claude Code 能看到的模型：{len(visible)}")
        for m in visible:
            out(f"      {m}")
        if hidden:
            out(f"  它悄悄丢弃的模型：{len(hidden)}")
            for m in hidden:
                out(f"      {m}")
            out("  -> 下面的二进制补丁能把它们找回来。")

    model = args.model
    if not model and models and is_tty() and not args.yes and not args.no_prompt_model:
        if confirm("  现在也固定一个默认模型吗？", False):
            model = pick_model(models, "选择 Claude Code 的默认模型")

    if not rep.claude_editor_settings and not rep.claude_cli_settings:
        out("  ! 找不到 Claude Code 配置文件；插件装了吗？")
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
        out(f"  已写入 {path}")
        for k in claude_mod.MANAGED:
            if k in after:
                mark = " " if before.get(k) == after[k] else "*"
                shown = after[k] if "TOKEN" not in k and "KEY" not in k else _mask(after[k])
                out(f"    {mark} {k} = {shown}")
        out(f"    （同时设置 claudeCode.disableLoginPrompt = true）")

    for path in cli_paths:
        before, after = claude_mod.apply_cli_settings(path, base, key, model)
        written.append(path)
        out()
        out(f"  已写入 {path}  （env 段，供终端里的 `claude` 使用）")

    if models:
        write_model_picker(args, models)

    warn_workspace_override(rep)

    if not args.no_patch:
        patch_claude_binary(args, rep)

    out()
    out("  下一步：重新打开 VSCode，然后 Claude Code 里的 /model 应列出网关的模型。")
    return 0


def _mask(secret: str) -> str:
    return secret[:6] + "..." + secret[-4:] if len(secret) > 12 else "***"


def _patch_state(state: str) -> str:
    """把 claude_patch 的英文状态值译成中文，仅供显示用（比较仍用原值）。"""
    if state == "ORIGINAL":
        return "原始未打补丁"
    if state == "PATCHED":
        return "已打补丁"
    if state.startswith("UNKNOWN"):
        detail = state[len("UNKNOWN ("):].rstrip(")")
        detail = detail.replace("unpatched filters found:", "未打补丁的匹配数：")
        detail = detail.replace("expected 2", "期望为 2")
        return "未知（" + detail + "）"
    return state


def _picker_description(text: str) -> str:
    """兼容旧 settings.json 里已写入的英文灰字描述，只影响展示。"""
    if not text:
        return text
    return (text.replace("From gateway", "来自网关")
                .replace("context", "上下文"))


def _auth_state(state: str) -> str:
    """把 codex_auth 的英文状态码译成中文，仅供显示用。"""
    return {
        "MISSING": "缺失",
        "EMPTY": "空文件",
        "UNREADABLE": "无法读取",
        "API_KEY": "API 密钥已登录",
        "CHATGPT": "ChatGPT 账号已登录",
    }.get(state, state)


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
    rule("刷新 /model 选择列表")
    paths = [*rep.claude_editor_settings, *rep.claude_cli_settings]
    base, key = resolve_credentials(args, claude_mod.existing_base_url(paths),
                                    claude_mod.existing_api_key(paths))
    models = fetch_models(base, key, required=True)
    snapshot([user_claude_settings()])
    write_model_picker(args, models)
    report_and_clear_cache()
    out()
    out("  下一步：重新加载 VSCode 窗口，选择列表就会读到它。")
    return 0


def write_model_picker(args, models: list[gateway.Model]) -> None:
    """Replace the built-in /model lineup with the gateway's live model list."""
    out()
    path = user_claude_settings()
    rows, _ = claude_mod.apply_model_picker(
        path, models, replace_builtin=not args.keep_builtin_models)

    out(f"  已写入 {path}  （modelPicker）")
    if args.keep_builtin_models:
        out(f"    + 在内置列表后面追加了 {len(rows)} 条网关模型")
        return

    out(f"    {len(rows)} 条；Claude Code 的内置列表已被隐藏：")
    for r in rows:
        out(f"      {r['model']:32} {r['description']}")

    hidden = [m.id for m in models if not m.is_chat_capable]
    if hidden:
        out(f"    （已省略 {len(hidden)} 个非对话端点：{', '.join(hidden)}）")
    out("    仍保留 Default 一行 —— 它会解析为本文件里的 `model`。")


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
            out("    也定义了 claudeCode.environmentVariables。工作区配置会整体")
            out("    替换用户级数组，所以那里的网关配置会覆盖本文件的设置。")
            out("    请删除它，或针对该文件重新运行本脚本。")


def patch_claude_binary(args, rep: detect.Report) -> None:
    rule("claude.exe 模型过滤补丁")
    target = Path(args.claude_binary) if args.claude_binary else (
        rep.claude_binaries[0] if rep.claude_binaries else None
    )
    if not target or not target.exists():
        out("  ! 找不到插件自带的 claude.exe —— 跳过过滤规则补丁。")
        out("    （如果你知道它在哪，用 --claude-binary <路径> 指定）")
        return

    state = claude_patch.status(target)
    out(f"  二进制：{target}")
    out(f"  状态  ：{_patch_state(state)}")

    if state == "PATCHED":
        out("  已打过补丁，二进制是最新的。")
        report_and_clear_cache()
        return
    if not state.startswith("ORIGINAL"):
        out("  ! 二进制内容不符合预期；拒绝打补丁。请重装插件。")
        return

    patched, hits, changed = claude_patch.build_patched(target)
    out(f"  已生成：{patched.name}  （停用 {hits} 条过滤规则，改动 {changed} 字节）")
    swap, restore_bat = claude_patch.write_swap_scripts(target)

    alive = claude_patch.running_processes()
    if alive:
        out()
        out(f"  ! 仍在运行：{', '.join(alive)}")
        out("    请完全退出 VSCode，然后二选一：")
        out(f'      双击 {swap}')
        out(f"      或重新运行：python {self_cmd()} --target claude --patch-only")
        return

    claude_patch.install(target, patched)
    out(f"  已安装 -> {_patch_state(claude_patch.status(target))}")
    out(f"  备份：{target.with_suffix(target.suffix + '.bak')}")
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
            out(f"  模型缓存：{path} 处没有")
        return

    models = [str(m.get("id", "")) for m in (cache.get("models") or [])]
    out()
    out(f"  模型缓存：{path}")
    out(f"      已缓存 {len(models)} 个模型：{', '.join(models) or '(空)'}")
    if not models and not force:
        return

    if claude_mod.cache_looks_filtered(cache):
        out("      ! 缓存的每个名字都含 claude/anthropic —— 这是打补丁之前抓到的")
        out("        列表，而选择列表优先读它、不重新拉取。清掉它才能强制重新发现。")

    out(f"      缓存来源：{cache.get('baseUrl', '?')}")
    bak = claude_mod.clear_gateway_cache(path)
    out(f"      已清除（备份：{bak.name if bak else '无'}）—— 重启 VSCode 后重新拉取")


def restore_claude(rep: detect.Report) -> int:
    rule("恢复 Claude Code")
    # The user-level file is listed even when it is not on disk any more -- its
    # backup is the zero-byte "did not exist" record and still needs acting on.
    settings_files = [*rep.claude_editor_settings, *rep.claude_cli_settings]
    if user_claude_settings() not in settings_files:
        settings_files.append(user_claude_settings())
    for path in settings_files:
        if claude_mod.restore_from_backup(path):
            out(f"  已从 {path.name}.bak 恢复 {path}")
        else:
            out(f"  {path} 没有备份 —— 保持原样")
    for b in rep.claude_binaries:
        if claude_patch.status(b) == "PATCHED" and not claude_patch.running():
            claude_patch.restore(b)
            out(f"  已恢复原始的 {b.name}")
        elif claude_patch.status(b) == "PATCHED":
            out(f"  ! {b.name} 已打补丁，但 VSCode 正在运行；请关闭后重试")
    # A cache written by the patched binary would keep showing models the original
    # binary is once again filtering out.
    if claude_mod.clear_gateway_cache():
        out(f"  已清除网关模型缓存 {claude_mod.gateway_cache_path().name}")
    # Restoring from .bak already removed the modelPicker block, but only if a
    # backup was there to restore from -- belt and braces for the other case.
    user_settings = user_claude_settings()
    if claude_mod.clear_model_picker(user_settings):
        out(f"  已从 {user_settings} 删除 modelPicker")
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
            flag = "" if m.is_chat_capable else "   <- 不是对话模型"
            out(f"    {m.id}{flag}")
        if not args.switch_model:
            return 0

    current = _codex_saved_model(cfg)
    if current:
        out(f"  config.toml 里当前使用的模型：{current}")

    model = args.model
    if not model and models:
        if args.switch_model or (is_tty() and not args.yes):
            model = pick_model(models, "选择 Codex 要使用的模型")
    if not model and not args.model and not current:
        out("  ! 没有选择模型；请传 --model <id> 或运行 --switch-model")
        return 1

    # Codex 0.150 only speaks /responses. Verify the gateway serves it before
    # writing a config that would fail on the first prompt.
    if model and not args.skip_probe:
        out(f"  正在用 {model} 探测 /v1/responses ...")
        try:
            result = gateway.probe_wire_api(base_v1, key, model)
        except gateway.GatewayError as e:
            out(f"  ! 探测失败：{e}")
        else:
            if result.get("responses"):
                out("  /v1/responses  正常")
            else:
                out("  ! 该网关不提供 /v1/responses")
                if result.get("chat"):
                    out("    /v1/chat/completions 可用，但 Codex 0.150 已经取消了")
                    out('    wire_api = "chat" —— 在网关代理 /responses 之前，')
                    out("    Codex 无法驱动该网关（LiteLLM 可以代理）。")
                return 1

    inline_key = key if args.inline_key else None
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
    if inline_key:
        out(f"      experimental_bearer_token = {_mask(key)}   （内联，无需重启）")
    else:
        out(f"      env_key  = {codex_mod.env_key_name(args.provider_id)}")
    if summary.get("profile"):
        out(f"    [profiles.{summary['profile']}]  -> 用 `codex --profile {summary['profile']}` 生效")
    elif model:
        out(f'    model = "{model}"')
        out(f'    model_provider = "{args.provider_id}"')

    if not inline_key:
        name = codex_mod.env_key_name(args.provider_id)
        note = codex_mod.set_env_var(name, key)
        out()
        out(f"  API 密钥：{note}")

    if models:
        write_codex_catalog(cfg, models)
        strip_blocking_keys(cfg)
        warn_gateway_params(base, key)

    helpers = codex_mod.write_switch_helpers(HERE)
    out()
    out("  以后要切换模型可以用：")
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


def generate_model_config(models: list[gateway.Model]) -> Path | None:
    """Create or top up `model-config.jsonc`, beside this script.

    The file is where a model's capability now lives -- image support, thinking
    rungs, context length -- so it is written BEFORE the catalog is built, and the
    catalog is then resolved from it. That is what makes "patch and it works" true
    without a second command, and what makes the second command a refinement
    rather than a prerequisite.

    Only ever ADDS: a slug already in the file is left byte-for-byte alone,
    comments and all. `--refresh-models` is cheap enough that people run it often,
    and silently reverting an edit would be indistinguishable from a broken tool.
    OpenAI's own models are skipped -- their capability is OpenAI's to declare and
    is copied from `models_cache.json`; see modelconfig.py.

    `TOOLKIT_VSCODE` is what makes `_toolkits.vscode` apply, so this file's
    reasoning rungs stop at `xhigh` -- the webview cannot draw a `max` row and
    drops it silently.
    """
    entries: dict[str, dict[str, object]] = {}
    for m in models:
        if not m.is_chat_capable:
            continue
        if reasoning.is_openai_official(m.id):
            continue
        raw = m.raw or {}
        entries[m.id] = modelconfig.suggested(
            m.id, modelconfig.TOOLKIT_VSCODE,
            advertised_context=raw.get("max_input_tokens"),
        )
    if not entries:
        return None

    try:
        path, added, kept = modelconfig.ensure(entries, modelconfig.TOOLKIT_VSCODE)
    except (OSError, ValueError) as e:
        # Never fatal: the catalog can still be built from the seed and defaults,
        # so a config file we cannot write must not cost the user their setup.
        out(f"  ! 无法写入模型配置文件（{e}）；改用种子里的默认值")
        return None

    if added and kept:
        out(f"  {path}  （新增 {len(added)} 个模型：{', '.join(added)}）")
        out("    —— 请编辑这个文件，补充/修正每个模型的图片支持、思考档位和上下文长度，")
        out(f"    然后运行 `python {self_cmd()} --apply-model-config` 让改动生效。")
    else:
        out(f"  已写入 {path}  （按模型的能力配置 —— 归你编辑）")
        out("    —— 请编辑这个文件，补充/修正每个模型的图片支持、思考档位和上下文长度，")
        out(f"    然后运行 `python {self_cmd()} --apply-model-config` 让改动生效。")
    return path


def write_codex_catalog(cfg: Path, models: list[gateway.Model]) -> list[str]:
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

    out(f"  已写入 {path}  （模型目录）")
    out(f"    Codex 下拉框里只有这 {len(slugs)} 个模型，没有别的：")
    for s in slugs:
        out(f"      {s}")
    hidden = [m.id for m in models if not m.is_chat_capable]
    if hidden:
        out(f"    （已省略 {len(hidden)} 个非对话端点：{', '.join(hidden)}）")
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
        out(f'  已删除根级配置 model_reasoning_effort = "{removed_reasoning}"')
        out("    它会覆盖目录里每个模型的 default_reasoning_level，")
        out("    导致所有模型的 Reasoning 子菜单被隐藏或卡在同一个档位。")
        out("    现在每个模型都使用自己目录条目声明的档位。")

    removed_openai = codex_mod.strip_openai_only_keys(cfg)
    for k in removed_openai:
        out()
        out(f"  已删除根级配置 {k}（OpenAI 专有；私有网关可能拒绝它）")


def refresh_catalog(args, rep: detect.Report) -> int:
    """Codex's half of `--refresh-models`: rewrite only the catalog JSON."""
    rule("刷新 Codex 模型目录")
    cfg = rep.codex_config or detect.codex_config()
    claude_paths = [*rep.claude_editor_settings, *rep.claude_cli_settings]
    saved = _codex_saved_base(cfg) or claude_mod.existing_base_url(claude_paths)
    base, key = resolve_credentials(
        args, saved,
        codex_mod.saved_key(cfg) or claude_mod.existing_api_key(claude_paths))
    models = fetch_models(base, key, required=True)

    if not cfg.exists():
        out(f"  ! 没有 {cfg} —— 请先用 --target codex 创建它")
        return 1
    snapshot([cfg])
    write_codex_catalog(cfg, models)
    strip_blocking_keys(cfg)
    out()
    out("  下一步：重新加载 VSCode 窗口，模型下拉框就会读到它。")
    return 0


# --------------------------------------------------------------------------- #
# model-config.jsonc: the second step
# --------------------------------------------------------------------------- #

def model_config_problems() -> list[str]:
    """Everything wrong with the user's `model-config.jsonc`, in plain words.

    Two kinds, deliberately reported together: shape problems (from modelconfig,
    which owns the schema, plus an unparseable file) and values outside the enums
    Codex accepts (known only here, because the enums live in `modalities` /
    `reasoning` and modelconfig must not import them -- they import it).

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

    This is the second half of the two-step flow. Step one (the patch, or
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
        out(f"  ! 没有 {cfg} —— 请先用 --target codex 创建它")
        return 1
    base, key = _gateway_credentials(args, rep)
    models = fetch_models(base, key, required=True)

    snapshot([cfg])
    # Top up first: a model the gateway gained since the file was generated should
    # get an entry rather than silently resolving from defaults forever.
    write_codex_catalog(cfg, models)
    strip_blocking_keys(cfg)

    out()
    out("  模型目录现在已反映该文件的内容。如果运行时没生效，那是个 bug ——")
    out("  `--status` 会显示每个模型最终生效的值。")
    out("  下一步：重新加载 VSCode 窗口，模型下拉框就会读到它。")
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
    rule("恢复 Codex")
    # Read the catalog facts BEFORE restoring: restore_from_backup rewrites
    # config.toml, so afterwards the key is usually gone.
    owns_catalog = codex_mod.catalog_is_ours(cfg)
    catalog_file = codex_mod.existing_catalog_path(cfg)

    if codex_mod.restore_from_backup(cfg):
        out(f"  已从 {cfg.name}.bak 恢复 {cfg}")
    else:
        out(f"  {cfg} 没有备份 —— 保持原样")

    # Redundant when the .bak predates the catalog, but it is the only thing that
    # cleans up a run that had no backup to restore from.
    if cfg.exists() and codex_mod.clear_catalog(cfg):
        out(f"  已从 {cfg} 删除 {codex_mod.CATALOG_KEY}")
    if owns_catalog and catalog_file and codex_mod.remove_catalog_file(Path(catalog_file)):
        out(f"  已删除生成的模型目录 {catalog_file}")
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
    out(f"  模型定义端点：{endpoint}")
    entries = litellm_admin.fetch_model_info(base, key)
    out(f"  网关连接正常：{len(entries)} 个模型定义")
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
    out(f"  ! {len(todo)} 个私有模型缺少 Codex 需要的网关参数，")
    out("    对它们的请求会失败（思考参数报 HTTP 400，元数据报 HTTP 500）：")
    for p in todo:
        out(f"      {p.model_name:28} {litellm_admin.describe(p)}")
    out(f"    修复：python {self_cmd()} --emit-gateway-config")


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

    try:
        base, key, report = read_gateway_models(args, rep)
    except gateway.GatewayError as e:
        raise SystemExit(f"  ! {e}")

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
        return 0

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


def run_fix_login(args, rep: detect.Report) -> int:
    rule("Codex 登录状态")
    state, detail = codex_auth.status()
    out(f"  当前状态：{_auth_state(state)}  （{detail}）")

    if args.restore:
        if codex_auth.restore_from_backup():
            out(f"  已从 .bak 恢复 {codex_auth.auth_path()}")
        else:
            out("  没有 auth.json.bak 可以恢复 —— 未做任何改动")
        return 0

    if state in ("API_KEY",) and not args.force:
        out("  已经是 API 密钥登录，无需处理。")
        out("  （需要重写可加 --force，比如密钥轮换之后）")
        return 0
    if state == "CHATGPT" and not args.force:
        out("  已存在真实的 ChatGPT 登录，保持不动。")
        out("  （需要替换成网关密钥可加 --force）")
        return 0

    base, key = _gateway_credentials(args, rep)

    # Writing a key that the gateway rejects would be worse than writing nothing:
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

    path, backup = codex_auth.write_api_key_auth(key)
    state, detail = codex_auth.status()

    out()
    out(f"  已写入 {path}")
    out(f"    OPENAI_API_KEY = {_mask(key)}   （网关密钥，不是 OpenAI 的）")
    if backup:
        out(f"    原文件已备份到 {backup}")
    out(f"  之后状态：{_auth_state(state)}  （{detail}）")
    out()
    out("  Codex 不再需要 ChatGPT 账号：私有 provider 配置表就是用它校验的。")
    out("  重新打开 VSCode 后生效。")
    return 0


def run_configure_reasoning(args, rep: detect.Report) -> int:
    rule("按模型设置思考档位")

    if args.reasoning_clear:
        store = reasoning.load()
        targets = [args.reasoning_clear] if args.reasoning_clear != "all" else list(store)
        if not targets:
            out("  没有已保存的按模型覆盖配置，无需清除。")
            return 0
        for m in targets:
            out(f"  {'已清除 ' + m if reasoning.clear(m) else m + ' 没有覆盖配置'}")
        return 0

    models = fetch_models(*_gateway_credentials(args, rep), required=True)

    model = args.reasoning_model
    if not model:
        if not is_tty():
            raise SystemExit("需要指定 --reasoning-model（或在终端里交互运行）")
        model = pick_model(models, "要设置哪个模型的思考档位？")

    if model not in {m.id for m in models}:
        out(f"  ! 网关没有提供 {model!r}；下面是它实际提供的模型：")
        for m in models:
            out(f"      {m.id}")
        return 1

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

    if args.reasoning_levels is not None:
        wanted = [x.strip() for x in args.reasoning_levels.split(",") if x.strip()]
    else:
        out()
        out(f"  可选档位：{', '.join(reasoning.KNOWN_EFFORTS)}")
        out("  （逗号分隔；留空表示该模型没有思考档位菜单）")
        wanted = [x.strip() for x in
                  ask("档位", default=",".join(lv["effort"] for lv in current_levels)
                      or "low,high,max").split(",") if x.strip()]

    unknown = [x for x in wanted if x not in reasoning.KNOWN_EFFORTS]
    if unknown:
        out(f"  ! 不是 Codex 认的思考档位：{', '.join(unknown)}")
        out(f"    可选值：{', '.join(reasoning.KNOWN_EFFORTS)}")
        return 1

    default = args.reasoning_default
    if wanted and not default:
        if is_tty() and not args.yes:
            default = ask("默认档位", default=current_default or wanted[0])
        else:
            default = wanted[0]

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
    out("  下一步：运行 --target codex --refresh-models 重新生成目录。")
    return 0


def run_configure_modalities(args, rep: detect.Report) -> int:
    rule("按模型设置输入类型（图片）")

    if args.modalities_clear:
        store = modalities.load()
        targets = [args.modalities_clear] if args.modalities_clear != "all" else list(store)
        if not targets:
            out("  没有已保存的按模型覆盖配置，无需清除。")
            return 0
        for m in targets:
            out(f"  {'已清除 ' + m if modalities.clear(m) else m + ' 没有覆盖配置'}")
        out("  （被清除的模型会依次回退到 model-config.jsonc、再用内置种子 ——")
        out("    不会自动变成只支持文本，除非种子就是这么写的）")
        return 0

    models = fetch_models(*_gateway_credentials(args, rep), required=True)

    model = args.modalities_model
    if not model:
        if not is_tty():
            raise SystemExit("需要指定 --modalities-model（或在终端里交互运行）")
        model = pick_model(models, "要设置哪个模型的输入类型？")

    if model not in {m.id for m in models}:
        out(f"  ! 网关没有提供 {model!r}；下面是它实际提供的模型：")
        for m in models:
            out(f"      {m.id}")
        return 1

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
    out("  下一步：运行 --target codex --refresh-models 重新生成目录，")
    out("        然后重新打开 VSCode 窗口，附件按钮才会出现。")
    return 0


# Vision inference is slower than text, and these backends queue.
PROBE_TIMEOUT = 180.0


def run_probe_modalities(args, rep: detect.Report) -> int:
    rule("实测哪些模型真的能看图")
    out("  分别发送纯红、纯蓝两张图片，让每个模型说出颜色。接口不报错并不能")
    out("  说明问题 —— 模型可能返回 200 却描述一个根本不存在的颜色 —— 所以")
    out("  真正的判据是它能不能把两张图区分开。")
    out()

    base, key = _gateway_credentials(args, rep)
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
    out()
    if can:
        # Paste-ready, not written back. The verdict is worth having as text:
        # writing it ourselves would mean re-encoding a nested entry through
        # JsoncFile, which drops every comment in it. The user owns that file.
        out(f"  要启用，请把下面这些条目粘贴到 {modelconfig.store_path()}")
        out(f"  （或直接修改已有条目），然后运行："
            f" python {self_cmd()} --apply-model-config")
        out()
        for n in can:
            out(f'      "{n}": {{ "input_modalities": ["text", "image"] }},')
        out()
        out("  ...然后重新打开 VSCode 窗口。")
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
            out(f"  {path}: 读取失败（{e}）")
            continue
        env = {}
        for e in data.get("claudeCode.environmentVariables") or []:
            if isinstance(e, dict) and "name" in e:
                env[e["name"]] = e.get("value")
        if isinstance(data.get("env"), dict):
            env.update(data["env"])
        if not env:
            out(f"  {path}: 没有网关配置")
            continue
        out(f"  {path}:")
        for k in claude_mod.MANAGED:
            if k in env:
                v = _mask(str(env[k])) if ("TOKEN" in k or "KEY" in k) else env[k]
                out(f"      {k} = {v}")

    rule("claude.exe")
    if not rep.claude_binaries:
        out("  没有找到插件自带的二进制")
    for b in rep.claude_binaries:
        out(f"  {_patch_state(claude_patch.status(b)):<12} {b}")
    alive = claude_patch.running_processes()
    out(f"  当前正在运行：{', '.join(alive) if alive else '无'}")

    rule("/model 选择器")
    user_settings = user_claude_settings()
    picker = None
    if user_settings is not None:
        try:
            picker = read_jsonc(user_settings).get(claude_mod.PICKER_KEY)
        except Exception:  # noqa: BLE001
            picker = None
    if not isinstance(picker, dict):
        out("  未定制 —— 选择器会列出 Claude Code 自带的模型")
        out("      （Opus/Sonnet/Haiku，本网关一个都不提供）")
        out("      修复：--target claude --refresh-models")
    else:
        rows = picker.get("options") or []
        out(f"  {len(rows)} 项，位于 {user_settings}")
        for r in rows:
            if isinstance(r, dict):
                out(f"      {str(r.get('model', '?')):32} "
                    f"{_picker_description(str(r.get('description', '')))}")
        if picker.get("replaceBuiltInOptions") is True:
            out("      内置模型列表：已隐藏")
        else:
            out("      内置模型列表：仍然显示，网关模型追加在后面")

    rule("网关模型缓存")
    cache_path = claude_mod.gateway_cache_path()
    cache = claude_mod.read_gateway_cache(cache_path)
    if cache is None:
        out(f"  {cache_path} 不存在（发现模型时会重新拉取）")
    else:
        models = [str(m.get("id", "")) for m in (cache.get("models") or [])]
        out(f"  {cache_path}")
        out(f"      已缓存 {len(models)} 个：{', '.join(models) or '(空)'}")
        stale = claude_mod.cache_looks_filtered(cache)
        out(f"      写入来源：{cache.get('baseUrl', '?')}  "
            f"{'<- 看起来是打补丁前的（被过滤过的）结果' if stale else ''}")
        if stale:
            out("      用这个清除：--target claude --clear-model-cache")

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
            out("      -> Codex 的模型下拉框会列出 OpenAI 自带的模型")
            out("         （GPT-5.6 Sol/Terra/Luna、GPT-5.5 ...），本网关一个都不提供。")
            out("         修复：--target codex --refresh-models")
        elif not Path(cat).exists():
            out(f"      ! {cat} 不存在 —— Codex 会启动失败")
        else:
            try:
                entries = json.loads(Path(cat).read_text(encoding="utf-8"))["models"]
            except Exception as e:  # noqa: BLE001
                out(f"      ! 读取失败（{e}）")
            else:
                slugs = [str(e.get("slug", "?")) for e in entries]
                out(f"      {len(slugs)} 个模型，仅此而已：")
                for s in slugs:
                    out(f"          {s}")
                if not codex_mod.catalog_is_ours(rep.codex_config):
                    out("      （这个文件不是本工具生成的，已保持原样）")
    else:
        out("  没有 config.toml")

    rule("Codex 登录状态")
    state, detail = codex_auth.status()
    out(f"  {_auth_state(state)}  （{detail}）")
    if state in ("MISSING", "EMPTY", "UNREADABLE"):
        out("      Codex 会弹出登录提示，而私有网关无法完成这个登录。")
        out("      修复：--fix-login")

    rule("按模型设置思考档位")
    store = reasoning.load()
    if not store:
        out(f"  {reasoning.store_path()} 中没有覆盖配置")
        out(f"      私有模型默认为："
            f"{', '.join(x['effort'] for x in reasoning._levels(reasoning.DEFAULT_EFFORTS))}")
        out("      修复：--configure-reasoning --reasoning-model <id> "
            "--reasoning-levels low,high,xhigh")
    else:
        out(f"  {reasoning.store_path()}")
        for model_id, entry in sorted(store.items()):
            lv = [x["effort"] for x in (entry.get("levels") or [])]
            out(f"      {model_id:28} {', '.join(lv) or '(无档位菜单)'}"
                f"  默认={entry.get('default', '-')}")

    rule("模型能力配置")
    # The file beside the script, not the catalog: the catalog is derived from it,
    # so showing where the user EDITS matters more than showing the output.
    cap_store = modelconfig.store_path()
    if not cap_store.exists():
        out(f"  {cap_store}  缺失 —— 运行 --target codex --refresh-models 生成它")
    else:
        out(f"  {cap_store}")
        for slug in sorted(modelconfig.slugs()):
            e = modelconfig.entry(slug)
            mods_txt = ", ".join(str(m) for m in (e.get(modelconfig.FIELD_MODALITIES) or [])) or "-"
            lv = e.get(modelconfig.FIELD_LEVELS)
            lv_txt = ", ".join(str(x) for x in lv) if isinstance(lv, list) else "-"
            ctx = e.get(modelconfig.FIELD_CONTEXT)
            out(f"      {slug:28} {mods_txt:14} 上下文={ctx if ctx is not None else '-':<8}"
                f" 思考档位=[{lv_txt}] 默认={e.get(modelconfig.FIELD_DEFAULT_LEVEL) or '-'}")
        for p in model_config_problems():
            out(f"  有问题  {p}")
        out(f"  （编辑它，然后运行 --apply-model-config，把改动同步到"
            f" {codex_mod.catalog_path()}）")
    # Say which of these are actually IN EFFECT. The config file outranks them, so
    # listing a value without that check can state the opposite of the truth: an
    # old-store `low` next to a config-file `low,high,xhigh` reads as "this model
    # offers one rung" when it offers three.
    old_levels = reasoning.load()
    old_mods = modalities.load()
    if old_levels or old_mods:
        out("  按模型覆盖（旧存储，仍会读取，但 model-config.jsonc 优先级更高）：")
        for m, entry in sorted(old_levels.items()):
            names = ", ".join(lv["effort"] for lv in (entry.get("levels") or [])) or "(无)"
            shadowed = modelconfig.configured(m, modelconfig.FIELD_LEVELS) is not None
            out(f"      思考档位  {m:28} {names}  默认={entry.get('default') or '-'}"
                f"{'   <- 已忽略，model-config.jsonc 里配了这个模型' if shadowed else ''}")
        for m, entry in sorted(old_mods.items()):
            shadowed = modelconfig.configured(m, modelconfig.FIELD_MODALITIES) is not None
            out(f"      输入类型  {m:28} "
                f"{', '.join(modalities.normalize_entries(entry.get('input_modalities')))}"
                f"{'   <- 已忽略，model-config.jsonc 里配了这个模型' if shadowed else ''}")

    rule("网关侧模型参数")
    # `--status` is a read-only report; it must not start interrogating the user
    # for credentials (resolve_credentials prints the whole examples block when
    # there is nothing saved). No known gateway, nothing to report.
    known_base = args.api_base or _codex_saved_base(rep.codex_config or detect.codex_config()) \
        or claude_mod.existing_base_url([*rep.claude_editor_settings, *rep.claude_cli_settings])
    if not known_base:
        out("  还没有配置网关 —— 请先运行 --target codex")
    else:
        try:
            base, key, gw = read_gateway_models(args, rep)
        except (SystemExit, gateway.GatewayError) as e:
            out(f"  无法读取 /model/info：{e}")
            out("      （需要 LiteLLM 主密钥；请传入 --api-base/--api-key）")
            gw = None
        if gw is not None:
            todo = gw.todo
            if not todo:
                out("  所有私有模型都能接受 Codex 的 Responses 字段")
            else:
                out(f"  ! {len(todo)} 个模型缺少 Codex 需要的参数；")
                out("    打到这些模型的每一轮请求都会失败（HTTP 400 / HTTP 500）。")
                for p in todo:
                    out(f"      {p.model_name:28} {litellm_admin.describe(p)}")
                out("      修复：--emit-gateway-config（然后 --apply-gateway-config）")

    rule("通过 MCP 搜索")
    # Claude Code's built-in WebSearch and the Codex plugin's web_search are both
    # hosted tools: they reach the vendor's servers, not this gateway, so neither
    # works here. An MCP server is the only thing that puts search back.
    codex_url = search_mod.codex_configured(rep.codex_config or detect.codex_config())
    claude_url = search_mod.claude_configured(search_mod.claude_global_config())
    if not codex_url and not claude_url:
        out("  两个扩展都没有配置 —— 模型没有联网搜索能力。")
        out("      修复：重新运行常规命令；它会在最后一步接入搜索，")
        out("           并复用同一个 LiteLLM 密钥。（无需额外的密钥。）")
    else:
        out(f"  codex            {codex_url or '未配置'}")
        out(f"  claude           {claude_url or '未配置'}")
        if codex_url and claude_url and codex_url != claude_url:
            out("      ! 两者不一致 —— 重新运行 --configure-search")
        out("      用这个校验服务器：--check-search")
    return 0


# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# search (SearXNG over MCP)
# --------------------------------------------------------------------------- #

def search_targets(args) -> list[str]:
    """Which clients `--configure-search` writes.

    One MCP endpoint serves both editor clients, and unlike `--refresh-models`
    there is no older behaviour to preserve -- so with no `--target` we write
    both rather than prompting. Codex is included even though only Claude Code's
    built-in WebSearch was the visible loss: the Codex plugin needs it just as
    much, because its own web_search is a hosted tool this gateway cannot run.
    """
    return {"claude": ["claude"], "codex": ["codex"],
            "both": ["claude", "codex"]}[args.target or "both"]


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
    claude_paths = [*rep.claude_editor_settings, *rep.claude_cli_settings]

    # Saved config first: in the main flow the model steps have just written it,
    # so this picks up exactly the key in use. Either file will do -- both
    # clients are pointed at the same gateway -- and `args` is the last resort
    # for the standalone `--configure-search` case on a machine with no config.
    base = (_codex_saved_base(cfg)
            or claude_mod.existing_base_url(claude_paths)
            or args.api_base)
    key = (codex_mod.saved_key(cfg)
           or claude_mod.existing_api_key(claude_paths)
           or args.api_key)

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
        out(f"  无法访问 —— {p.detail}")
        return
    out(f"  {p.server} {p.version}  （协议 {p.protocol}）")
    if p.detail:
        out(f"  ! {p.detail}")
    for name, desc in p.tools:
        out(f"    {name:24} {desc}")
    missing = search_mod.missing_expected(p.tools)
    if missing:
        out(f"  ! 服务器没有提供这些工具：{', '.join(missing)}")
        out("    模型将没有该工具 —— 请检查 MCP_SEARXNG_IMAGE")


def run_configure_search(args, rep, *, optional: bool = False) -> int:
    """Point both MCP clients at the gateway's search endpoint.

    `optional` is how the main flow calls it: search is an add-on, so not being
    able to write it must not turn an otherwise successful run into a failure.
    The standalone `--configure-search` keeps `optional=False` so the exit code
    still reports the problem when that is the only thing the user asked for.
    """
    rule("通过 MCP 搜索")
    cfg = rep.codex_config or detect.codex_config()
    claude_json = search_mod.claude_global_config()
    targets = search_targets(args)

    url, token = search_endpoint(args, rep)
    if not token:
        if optional:
            out("  已跳过 —— 没有可复用的网关密钥。请先运行模型配置，")
            out("  或者对直连后端传入 --search-token。")
            return 0
        if not is_tty():
            out("  ! 没有可写入的令牌 —— LiteLLM 密钥就是搜索令牌，所以")
            out("    请传入 --api-key（或对直连后端传入 --search-token）。")
            return 1
        token = ask("LiteLLM API 密钥（同时用作 MCP 的 Bearer 令牌）",
                    secret=True)

    if "codex" in targets:
        snapshot([cfg])
        search_mod.codex_write(cfg, url, token)
        out(f"  codex    mcp_servers.{search_mod.DEFAULT_NAME} -> {url}")
        out(f"           {cfg}")
    if "claude" in targets:
        snapshot([claude_json])
        search_mod.claude_write(claude_json, url, token)
        out(f"  claude   mcpServers.{search_mod.DEFAULT_NAME} -> {url}")
        out(f"           {claude_json}")

    out()
    out("  两个扩展都在启动时读取这段配置，所以请重新打开窗口。之后模型")
    out(f"  就能使用 {', '.join(search_mod.EXPECTED_TOOLS)}。")
    out()
    out("  正在校验服务器本身：")
    render_probe(search_mod.probe(url, token))
    return 0


def run_clear_search(args, rep) -> int:
    rule("通过 MCP 搜索")
    cfg = rep.codex_config or detect.codex_config()
    claude_json = search_mod.claude_global_config()
    targets = search_targets(args)

    touched: list[str] = []
    if "codex" in targets and cfg.exists() and search_mod.codex_clear(cfg):
        touched.append(f"codex ({cfg})")
    if "claude" in targets and search_mod.claude_clear(claude_json):
        touched.append(f"claude ({claude_json})")

    if not touched:
        out("  没有需要移除的内容")
        return 0
    out(f"  已从以下位置移除：{', '.join(touched)}")
    out("  重新打开窗口后生效。")
    return 0


def run_check_search(args, rep) -> int:
    rule("通过 MCP 搜索")
    cfg = rep.codex_config or detect.codex_config()
    claude_json = search_mod.claude_global_config()

    codex_url = search_mod.codex_configured(cfg)
    claude_url = search_mod.claude_configured(claude_json)
    derived_url, token = search_endpoint(args, rep)
    # Prefer what is actually on disk -- that is what the clients will send, and
    # a mismatch with the derived value is exactly the drift worth seeing.
    url = args.search_url or codex_url or claude_url or derived_url

    out(f"  {url}")
    out(f"  Authorization   {'Bearer ' + _mask(token) if token else '(无)'}")
    out(f"  codex            {codex_url or '未配置'}")
    out(f"  claude           {claude_url or '未配置'}")
    # Drift here is silent and confusing: one editor works and the other 401s for
    # what looks like the same configuration.
    if codex_url and claude_url and codex_url != claude_url:
        out("  ! 两份配置指向了不同的服务器 —— 重新运行 "
            "--configure-search 让它们保持一致")
    # Same idea one level up: the written URL versus the gateway this machine is
    # configured against. A stale entry here survives a gateway move and shows up
    # as a connection error, not as a config error.
    if args.search_url is None and codex_url and derived_url != codex_url:
        out(f"  ! 已配置的 URL 与本网关的地址不同 "
            f"({derived_url})")
        out("    重新运行 --configure-search 把它改过来")
    if args.search_url is None and not (codex_url or claude_url):
        out("  ! 两个文件里都没有配置 —— 仍然尝试检查默认地址")
    render_probe(search_mod.probe(url, token))
    return 0


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
        epilog=EXAMPLES,
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
    ap.add_argument("--target", "-t", choices=["claude", "codex", "both"],
                    help="要配置哪个插件（claude | codex | both）")
    ap.add_argument("--api-base", help="网关地址（API base URL），如 http://10.18.219.156:4000")
    ap.add_argument("--api-key", help="网关 API 密钥，如 sk-XXXXXXXX")
    ap.add_argument("--model", help="要固定的模型 id（见 --list-models）")
    ap.add_argument("--list-models", action="store_true",
                    help="打印网关提供的所有模型后退出")
    ap.add_argument("--switch-model", action="store_true",
                    help="codex：交互式选择模型并写入 config.toml")
    ap.add_argument("--detect", action="store_true", help="显示各配置文件位置后退出")
    ap.add_argument("--status", action="store_true", help="显示当前配置后退出")
    ap.add_argument("--restore", action="store_true", help="还原 .bak 文件")
    ap.add_argument("--yes", "-y", action="store_true", help="不提问，直接用默认值")
    ap.add_argument("--claude-binary", help="显式指定 claude.exe 路径")
    ap.add_argument("--clear-model-cache", action="store_true",
                    help="claude：删除 ~/.claude/cache/gateway-models.json，"
                         "让 /model 选择器重新从网关拉取")
    ap.add_argument("--refresh-models", action="store_true",
                    help="重新拉取网关模型列表，只重写模型列表：Claude Code 的 "
                         "/model 选择器条目，和/或 Codex 的模型目录"
                         "（不改环境变量，不打二进制补丁）。默认仅 claude；"
                         "其余情况用 --target codex|both")
    ap.add_argument("--apply-model-config", action="store_true",
                    help=f"重新读取你编辑过的 {modelconfig.STORE_FILENAME} "
                         f"并同步到实际生效的 Codex 模型目录；隐含 "
                         f"--target codex --refresh-models")
    ap.add_argument("--keep-builtin-models", action="store_true",
                    help="claude：在 /model 选择器里保留 Claude Code 自带的 "
                         "Opus/Sonnet/Haiku，而不是只显示网关模型")

    g = ap.add_mutually_exclusive_group()
    g.add_argument("--no-patch", action="store_true",
                   help="claude：只写配置，不动 claude.exe")
    g.add_argument("--patch-only", action="store_true",
                   help="claude：只（重新）打模型过滤补丁")

    ap.add_argument("--no-prompt-model", action="store_true",
                    help="claude：不提示固定默认模型")
    ap.add_argument("--inline-key", action="store_true",
                    help="codex：把密钥内联写入 config.toml，而不是用环境变量")
    ap.add_argument("--env-key", help="codex：保存密钥的环境变量名")
    ap.add_argument("--provider-id", default=codex_mod.DEFAULT_PROVIDER_ID,
                    help="codex：provider 配置表 id（默认：private）")
    ap.add_argument("--profile", help="codex：写入 [profiles.<name>]，而不是根配置")
    ap.add_argument("--skip-probe", action="store_true",
                    help="codex：跳过 /v1/responses 连通性探测")
    ap.add_argument("--strip-openai-keys", action="store_true",
                    help="codex：从 config.toml 删除 service_tier 等 OpenAI 专有键")

    ap.add_argument("--emit-gateway-config", action="store_true",
                    help="打印 /model/update 调用，让私有模型接受 Codex 的 "
                         "reasoning_effort 并丢弃 client_metadata，但不改动网关")
    ap.add_argument("--apply-gateway-config", action="store_true",
                    help="同上，但真正发送（需要 LiteLLM 主密钥）")
    ap.add_argument("--fix-login", action="store_true",
                    help="codex：写入 ~/.codex/auth.json，让 Codex 不再弹 "
                         "ChatGPT 登录提示（配 --restore 可撤销，配 --force 可强制重写）")
    ap.add_argument("--force", action="store_true",
                    help="codex 登录：即使已存在登录信息也重写 auth.json")
    ap.add_argument("--configure-reasoning", action="store_true",
                    help="设置某个私有模型在 Codex 里提供哪些思考档位")
    ap.add_argument("--reasoning-model", help="要配置思考档位的模型 id")
    ap.add_argument("--reasoning-levels",
                    help="逗号分隔的档位（如 low,high,max）；空字符串表示"
                         "该模型没有思考档位菜单")
    ap.add_argument("--reasoning-default", help="默认档位（必须是 --reasoning-levels 之一）")
    ap.add_argument("--reasoning-clear", metavar="MODEL",
                    help="删除 MODEL 的覆盖配置，`all` 表示所有模型")
    ap.add_argument("--configure-modalities", action="store_true",
                    help="设置某个私有模型在 Codex 里接受哪些输入类型（文本/图片）")
    ap.add_argument("--modalities-model", help="要配置输入类型的模型 id")
    ap.add_argument("--modalities",
                    help="逗号分隔的类型，如 text,image；只写 `text` 会禁止"
                         "该模型接收附件")
    ap.add_argument("--modalities-clear", metavar="MODEL",
                    help="删除 MODEL 的覆盖配置，`all` 表示所有模型")
    ap.add_argument("--probe-modalities", action="store_true",
                    help="实测哪些网关模型真的能看图：让它们分辨红色和蓝色")
    ap.add_argument("--probe-models",
                    help="要探测的模型 id，逗号分隔（默认：所有对话模型）")

    ap.add_argument("--configure-search", action="store_true",
                    help="单独接入搜索 MCP 服务后退出。正常安装流程已经会做，"
                         "这里只用于单独重跑或修复")
    ap.add_argument("--no-search", action="store_true",
                    help="跳过正常安装流程本会执行的搜索配置步骤")
    ap.add_argument("--search-url",
                    help="覆盖 MCP 端点（默认：网关地址 + /searxng/mcp）")
    ap.add_argument("--search-token",
                    help="覆盖 MCP bearer token（默认：本次配置使用的 LiteLLM "
                         f"密钥；直连后端时 ${search_mod.TOKEN_ENV} 是最后的兜底）")
    ap.add_argument("--search-clear", action="store_true",
                    help="再次删除 MCP 搜索配置块（默认：--target both）")
    ap.add_argument("--check-search", action="store_true",
                    help="与 MCP 服务握手并列出模型能获得的工具；会解释 "
                         "401/403/404，并提示两份配置不一致")

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
        out(f"  已从 {cfg} 删除：{', '.join(removed) if removed else '(无)'}")
        return 0
    if args.clear_model_cache:
        rule("网关模型缓存")
        report_and_clear_cache(force=True)
        return 0
    # Gateway-side and reasoning commands stand alone: they touch no client file,
    # so they must not drag in --target or the editor-config machinery.
    if args.emit_gateway_config or args.apply_gateway_config:
        return run_gateway_config(args, rep, apply=args.apply_gateway_config)
    if args.fix_login:
        return run_fix_login(args, rep)
    if args.configure_search:
        return run_configure_search(args, rep)
    if args.search_clear:
        return run_clear_search(args, rep)
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
            raise SystemExit("需要指定 --target（claude | codex | both）")
        out("要把哪个插件指向私有网关？")
        out("  1. Claude Code   2. Codex   3. 两者都要")
        target = {"1": "claude", "2": "codex", "3": "both"}.get(
            ask("选择", default="3"), "both"
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

    # Search LAST, and non-fatally. Last because it reuses the key the steps
    # above just wrote -- run first, it would have nothing to copy. Non-fatal
    # because being unable to add a search tool must not fail a run whose actual
    # job (pointing the plugin at the gateway) succeeded. This is what makes the
    # whole thing one command instead of one command plus a follow-up.
    if not args.no_search:
        rc |= run_configure_search(args, rep, optional=True)

    if target == "both":
        rule("完成")
        out("  请重新打开 VSCode，让插件读取新的设置和环境变量。")
    return rc


if __name__ == "__main__":
    sys.exit(main())
