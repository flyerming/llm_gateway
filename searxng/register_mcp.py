#!/usr/bin/env python3
"""把 searxng-mcp 注册成 LiteLLM 网关的一个 MCP server。

为什么要有这一步
----------------
`searxng-mcp` 原本是独立发布到局域网的（`ports: 8090:8090`），LiteLLM 不认识它，
所以它只能自己鉴权 —— 于是每个客户端都要额外拿到 `MCP_SEARXNG_TOKEN`，用户手里
就有了两把 key，而且这一把是全局共享、无法按用户吊销的。

注册到 LiteLLM 之后，客户端改成连网关的 `/searxng/mcp`，用**它本来就有的 LiteLLM
virtual key** 鉴权。`MCP_SEARXNG_TOKEN` 降级成服务端内部凭据：只写在 LiteLLM 的
这份注册记录里，用来让网关去连 `searxng-mcp`，用户永远看不到它 —— 和
`CLIPROXY_API_KEY` 之于 litellm↔cli-proxy-api 是同一个角色。

于是「每用户一把 key」成立，8090 也可以不再发布。

注册记录存在 Postgres 里（compose 里 `STORE_MODEL_IN_DB: True`），重启不丢，
所以这是**一次性管理员动作**，不需要进 compose 的启动流程。

幂等
----
先 `GET /v1/mcp/server` 找 `server_name == searxng`：找得到就 `PUT` 更新，
找不到才 `POST` 新建。重复执行没有副作用，改完 token 重跑一次即可。

⚠️ 不要「GET 到的记录改几个字段再 PUT 回去」
------------------------------------------
`GET /v1/mcp/server` **从不回显 `credentials`**（永远是 `None` —— 密钥不回传是对的），
所以「读-改-写」会把存着的 token 一起抹掉。实测后果很隐蔽：

    改动生效了 → 但网关连不上 searxng-mcp → tools/list 全空
    → /health 从 healthy 变 unknown

**PUT 必须发完整请求体**，凭据一律从 `.env` 的 `MCP_SEARXNG_TOKEN` 重新取，
而不是从 GET 的结果里带。本脚本就是这么做的（`build_body` 每次重建整个 body）。

⚠️ url 必须是**容器内**地址
--------------------------
`http://searxng-mcp:8090/mcp` 是 LiteLLM 容器去连的地址，走 compose 网络。
不要写 `10.18.219.156:8090` —— 那是给局域网客户端用的，网关在自己的网络命名
空间里解析不到，而且 searxng-mcp 的 `MCP_HTTP_ALLOWED_HOSTS` 也要能接受
`searxng-mcp:8090` 这个 Host 头（它连端口精确比对，不匹配就是 403）。

用**服务名**而不是 IP，是为了让这一步不含任何环境相关的配置：换机器、换 IP、
换环境都不用改这里。服务名由 docker-compose.yml 的服务键决定。
本脚本每次都会去 `docker-compose.yml`（和 `.env` 的覆盖值）核对白名单里有没有
这个 host:port，对不上就往 stderr 报警 —— 它**不自动推导**，只保证漂移不静默。

用法
----
    python searxng/register_mcp.py --dry-run      # 只看要发什么，token 打码
    python searxng/register_mcp.py                # 注册/更新
    python searxng/register_mcp.py --check        # 只看当前注册状态和健康
    python searxng/register_mcp.py --unregister   # 撤销，回到「没注册」的状态

在服务器上跑时 `--base` 默认 `http://127.0.0.1:4000` 就够。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

SERVER_NAME = "searxng"

# 网关容器去连的地址，走 compose 网络。不是给局域网客户端用的那个。
#
# ★ 这里用 compose 的**服务名**，不是 IP。服务名由 docker-compose.yml 的服务键
#   决定，在任何环境下都一模一样 —— 换机器、换 IP、换环境都不用改。用 IP 就会
#   变成环境相关的配置，而且失败只在真调用时才暴露（/health 照样是绿的）。
INTERNAL_HOSTPORT = "searxng-mcp:8090"
INTERNAL_URL = f"http://{INTERNAL_HOSTPORT}/mcp"

DEFAULT_BASE = "http://127.0.0.1:4000"

# 只放行真正需要的四个：web_url_read 能抓任意 URL，多开一个就多一分暴露面。
#
# ⚠️ 这里的名字是**上游的原始名**。网关对客户端暴露的是加了 `{server_name}-`
#    前缀的版本（`web_url_read` → `searxng-web_url_read`）—— 实测确认，且
#    `tool_name_to_display_name` **改不掉它**（那个字段只影响显示层）。
#    所以客户端侧任何按名字做的校验都必须同时接受两种拼法。
ALLOWED_TOOLS = [
    "searxng_web_search",
    "web_url_read",
    "searxng_instance_info",
    "searxng_search_suggestions",
]


def load_env(path: Path) -> dict[str, str]:
    """极简 .env 解析：KEY=VALUE，支持引号，跳过注释和空行。

    只取需要的两个变量，绝不回显。
    """
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip("'\"")
    return env


def mask(secret: str | None) -> str:
    """只用于打印。任何要写进 stdout 的 secret 都必须先过这里。"""
    if not secret:
        return "(none)"
    return secret[:4] + "..." + secret[-4:] if len(secret) > 10 else "***"


def api(method: str, path: str, base: str, key: str, body: dict | None = None,
        timeout: float = 30.0):
    """调 LiteLLM 的管理接口。返回 (status, 解析后的 body 或原始文本)。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    except urllib.error.URLError as exc:
        return None, f"无法连接 {base}：{exc.reason}"
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


def find_server(base: str, key: str):
    """返回 (server_id, entry) 或 (None, None)。"""
    status, body = api("GET", "/v1/mcp/server", base, key)
    if status != 200 or not isinstance(body, list):
        raise SystemExit(f"GET /v1/mcp/server 失败：HTTP {status} {str(body)[:200]}")
    for entry in body:
        if entry.get("server_name") == SERVER_NAME:
            return entry.get("server_id"), entry
    return None, None


def build_body(token: str, url: str = INTERNAL_URL) -> dict:
    return {
        "server_name": SERVER_NAME,
        "transport": "http",
        "url": url,
        "auth_type": "bearer_token",
        # 网关拿它去连 searxng-mcp。这是这把 token 唯一该待的地方。
        "credentials": {"auth_value": token},
        # 任何 LiteLLM virtual key 都能调用 —— 「每用户一把 key」就靠这一行。
        # 想收紧到特定用户组，改用 mcp_access_groups 并把这行设 false。
        "allow_all_keys": True,
        # ⚠️ 这个字段的**默认值是 true**，不显式设 false 就等于把它挂到公网。
        "available_on_public_internet": False,
        "allowed_tools": ALLOWED_TOOLS,
    }


def check_compose(path: Path, env: dict[str, str] | None = None) -> list[str]:
    """核对 docker-compose.yml 里 searxng-mcp 的白名单默认值是否含 INTERNAL_HOSTPORT。

    为什么要做这一步（而不是「自动从 compose 推导 URL」）：推导要么引入 pyyaml
    依赖、要么起 subprocess、要么写脆弱正则，每种都自带新的失败模式。而真正的
    危害不是「两边不一致」，是**不一致之后静默** —— 网关连过去 403，而
    searxng-mcp 的 /health 照样返回 200 绿的，症状指向 token 而不是 Host 头。

    所以这里不推导、只核对：一致就闭嘴，不一致就大声报警。

    `.env` 会覆盖 compose 里的默认值，所以有覆盖值时核对的是覆盖值 —— 只看默认值
    会漏掉「.env 里还留着旧 IP」这种情况，而那正是最可能发生的漂移。

    返回问题列表，空列表表示没问题。文件不存在时也返回空（可能是在客户端机器上跑）。
    """
    problems: list[str] = []
    if not path.exists():
        return problems

    override = (env or {}).get("MCP_SEARXNG_ALLOWED_HOSTS")
    if override:
        hosts = [h.strip() for h in override.split(",") if h.strip()]
        if INTERNAL_HOSTPORT not in hosts:
            problems.append(
                f".env 里 MCP_SEARXNG_ALLOWED_HOSTS={hosts} 覆盖了 compose 的默认值，"
                f"里面没有 {INTERNAL_HOSTPORT!r} —— 网关会用这个 Host 头去连，"
                f"不匹配就是全程 403（而 /health 照样绿）")
        return problems

    text = path.read_text(encoding="utf-8", errors="replace")

    if f"{SERVER_NAME}-mcp:" not in text:
        problems.append(f"{path.name} 里没有服务 '{SERVER_NAME}-mcp' —— "
                        f"服务名改了，URL 和 Host 白名单都要跟着改")
        return problems

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("MCP_HTTP_ALLOWED_HOSTS:"):
            continue
        # 形如 MCP_HTTP_ALLOWED_HOSTS: ${VAR:-a:1,b:2}  —— 取 :- 之后的默认值
        value = stripped.split(":", 1)[1].strip()
        if value.startswith("${") and ":-" in value:
            value = value[value.index(":-") + 2:]
            value = value.rstrip("}")
        hosts = [h.strip() for h in value.split(",") if h.strip()]
        if INTERNAL_HOSTPORT not in hosts:
            problems.append(
                f"MCP_HTTP_ALLOWED_HOSTS 的默认值 {hosts} 里没有 {INTERNAL_HOSTPORT!r} "
                f"—— 网关会用这个 Host 头去连，不匹配就是全程 403（而 /health 照样绿）")
        return problems

    problems.append(f"{path.name} 里找不到 MCP_HTTP_ALLOWED_HOSTS —— 无法核对")
    return problems


def report_health(base: str, key: str) -> None:
    status, body = api("GET", "/v1/mcp/server/health", base, key)
    print(f"  /v1/mcp/server/health -> HTTP {status}")
    if status == 200 and body is not None:
        print("   ", json.dumps(body, ensure_ascii=False)[:400])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--base", default=DEFAULT_BASE,
                    help=f"LiteLLM 地址（默认 {DEFAULT_BASE}，在服务器上跑就用默认值）")
    ap.add_argument("--env-file", default=str(Path(__file__).resolve().parent.parent / ".env"),
                    help=".env 路径（默认仓库根目录的 .env）")
    ap.add_argument("--compose-file",
                    default=str(Path(__file__).resolve().parent.parent / "docker-compose.yml"),
                    help="docker-compose.yml 路径；用来核对 Host 白名单和这里的 URL 是否同源")
    ap.add_argument("--master-key",
                    help="LITELLM_MASTER_KEY；不给就读 .env / 环境变量")
    ap.add_argument("--token",
                    help="MCP_SEARXNG_TOKEN；不给就读 .env / 环境变量")
    ap.add_argument("--url", default=INTERNAL_URL,
                    help=f"网关去连的地址（默认 {INTERNAL_URL}）。"
                         "★ 只有当 Host 头在白名单里时网关才连得上 —— "
                         "用宿主 IP 形式可以绕过白名单做连通性验证，但生产该用内网名。")
    ap.add_argument("--dry-run", action="store_true", help="只打印将发送的内容，不写")
    ap.add_argument("--check", action="store_true", help="只看当前注册状态和健康")
    ap.add_argument("--unregister", action="store_true", help="删除注册，回到未注册状态")
    args = ap.parse_args(argv)

    # Windows 控制台默认是 GBK，中文提示会乱码。和 toolkit 里的做法一致。
    # stderr 也要重配 —— 漂移告警是往 stderr 走的，只改 stdout 它会照样乱码。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    env = load_env(Path(args.env_file))
    master = args.master_key or env.get("LITELLM_MASTER_KEY") or None
    token = args.token or env.get("MCP_SEARXNG_TOKEN") or None

    if not master:
        raise SystemExit("缺少 LITELLM_MASTER_KEY：用 --master-key 给，或确保 "
                         f"{args.env_file} 里有这一项")
    if not token and not args.check and not args.unregister:
        raise SystemExit("缺少 MCP_SEARXNG_TOKEN：用 --token 给，或确保 "
                         f"{args.env_file} 里有这一项")

    base = args.base

    # ---- 同源核对：不推导，只报警 ----
    # 只在用默认内网地址时才有意义：--url 传了宿主 IP 形式就是故意绕开白名单的。
    if args.url == INTERNAL_URL:
        for problem in check_compose(Path(args.compose_file), env):
            print(f"  ! 配置漂移：{problem}", file=sys.stderr)

    # ---- 只看状态 ----
    if args.check:
        sid, entry = find_server(base, master)
        if not entry:
            print(f"  '{SERVER_NAME}' 尚未注册")
        else:
            print(f"  server_id     {sid}")
            print(f"  url           {entry.get('url')}")
            print(f"  transport     {entry.get('transport')}")
            print(f"  auth_type     {entry.get('auth_type')}")
            print(f"  allow_all_keys {entry.get('allow_all_keys')}")
            print(f"  公网可达      {entry.get('available_on_public_internet')}")
            print(f"  allowed_tools {entry.get('allowed_tools')}")
            # 正常应该是 None。实测这个映射**只改显示名，不改可调用名** ——
            # 加了它之后 tools/list 返回的仍然是 `searxng-web_url_read` 这种
            # 带前缀的名字。所以它解决不了「工具名被加前缀」的问题，别指望它。
            print(f"  显示名映射    {entry.get('tool_name_to_display_name')}")
        report_health(base, master)
        return 0

    # ---- 撤销 ----
    if args.unregister:
        sid, entry = find_server(base, master)
        if not entry:
            print(f"  '{SERVER_NAME}' 本来就没注册，无需操作")
            return 0
        status, body = api("DELETE", f"/v1/mcp/server/{sid}", base, master)
        print(f"  DELETE /v1/mcp/server/{sid} -> HTTP {status}")
        if status not in (200, 204):
            print(f"  ! {str(body)[:300]}")
            return 1
        print("  已删除。客户端会立刻 404 —— 记得同步改客户端配置。")
        return 0

    # ---- 注册 / 更新 ----
    body = build_body(token, args.url)

    if args.dry_run:
        safe = json.loads(json.dumps(body))
        safe["credentials"]["auth_value"] = mask(token)
        print("  将发送的请求体（token 已打码）：")
        print(json.dumps(safe, ensure_ascii=False, indent=2))
        print()
        print(f"  ★ 网关是用 URL 里的主机名当 Host 头去连的，而 MCP_HTTP_ALLOWED_HOSTS")
        print(f"    是连端口【精确比对】。所以上面这个 url 的 host:port 必须在白名单里，")
        print(f"    否则网关连过去会被 403，而 searxng-mcp 的 /health 照样是绿的。")
        return 0

    sid, entry = find_server(base, master)
    if entry:
        body["server_id"] = sid
        status, resp = api("PUT", "/v1/mcp/server", base, master, body)
        action = "更新"
    else:
        status, resp = api("POST", "/v1/mcp/server", base, master, body)
        action = "新建"

    # PUT 成功返回的是 **202 Accepted**（不是 200），POST 新建是 201。
    # 只认 200/201 会把「更新成功」误报成失败 —— 实测踩过。
    if status not in (200, 201, 202):
        print(f"  {action}失败：HTTP {status}")
        print(f"  {str(resp)[:400]}")
        return 1

    print(f"  已{action} '{SERVER_NAME}'  ->  {args.url}")
    if args.url != INTERNAL_URL:
        print(f"  ! 注意：这不是生产该用的地址（那个是 {INTERNAL_URL}）")
        print(f"    只有在做连通性验证时才该用宿主 IP 形式。")
    print(f"  token {mask(token)}（服务端内部凭据，客户端不再需要它）")
    print(f"  allow_all_keys=True  -> 任何 LiteLLM virtual key 都能调用")
    print()
    print("  下一步，用**普通用户的 key**（不是 master key）验一次：")
    print()
    print(f"    curl -sS -X POST {base}/{SERVER_NAME}/mcp \\")
    print("      -H \"Authorization: Bearer $USER_KEY\" \\")
    print("      -H 'Content-Type: application/json' \\")
    print("      -H 'Accept: application/json, text/event-stream' \\")
    print("      -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",")
    print("           \"params\":{\"protocolVersion\":\"2025-06-18\",\"capabilities\":{},")
    print("                     \"clientInfo\":{\"name\":\"curl\",\"version\":\"1\"}}}'")
    print()
    report_health(base, master)
    return 0


if __name__ == "__main__":
    sys.exit(main())
