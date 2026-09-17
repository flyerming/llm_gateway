# codexcli —— 让 Codex CLI 免登录直连私有化网关，`/model` 直接列出网关模型

一条命令，把 Linux 上的 Codex CLI 从官方 API 切到你们自己的 LiteLLM 网关：

```bash
python3 private_api.py \
    --api-base http://10.18.219.156:4000 \
    --api-key  sk-XXXXXXXX
```

跑完之后 `codex` 打开就是**网关的模型**，**不弹登录**，`/model` 里能直接切换。

不带 `--api-base` 在终端里跑，会进入交互式向导。

只用 Python 标准库，**不需要 `pip install`**（`from __future__ import annotations` 已开，Python 3.8+ 都能跑）。
入口脚本是 `codexcli/private_api.py`，下面所有命令都假设你在 `codexcli/` 目录里执行。

> 这套工具**只在 Linux 上跑**（你的 codex 装在 Linux）。Windows 上那份对应的实现是 [`vscode/`](../vscode/README.md)，两者共用同一套 `private-reasoning.json` / `private-modalities.json` 覆盖文件，同一台机器两套都配过也不会打架。

---

## 目录

- [做了什么](#做了什么)
- [自动检索哪些位置](#自动检索哪些位置)
- [四个部件，缺一不可](#四个部件缺一不可)
  - [1. `config.toml`：指向网关](#1-configtoml指向网关)
  - [2. `model_catalog_json`：`/model` 列表的来源](#2-model_catalog_jsonmodel-列表的来源)
    - [版本兼容：codex 的版本敏感字段](#版本兼容codex-的版本敏感字段)
  - [3. `auth.json`：不写它就一直弹登录](#3-authjson不写它就一直弹登录)
  - [4. 自动刷新：`codex` 包装脚本](#4-自动刷新codex-包装脚本)
- [思考档位（reasoning）](#思考档位reasoning)
- [上下文窗口（context_window）](#上下文窗口context_window)
- [能不能贴图（input_modalities）](#能不能贴图input_modalities)
- [网关（LiteLLM）侧改动](#网关litellm侧改动)
- [命令速查](#命令速查)
- [常见问题](#常见问题)
- [目录结构](#目录结构)

---

## 做了什么

| 改的东西 | 说明 |
|---|---|
| `$CODEX_HOME/config.toml` | 写 `[model_providers.private]` + `model` + `model_provider` |
| `$CODEX_HOME/config.toml` 的 `model_catalog_json` | **把 `/model` 列表换成网关模型**，不再显示 GPT-5.6 Sol/Terra/Luna 这些网关上没有的（见下） |
| `$CODEX_HOME/gateway-models.json` | 生成的模型目录，就是上一行指向的文件 |
| `$CODEX_HOME/auth.json` | **不写这个 Codex 永远弹登录**（见下） |
| `$CODEX_HOME/private-reasoning.json` | 仅 `--configure-reasoning`。每个私有模型暴露哪些思考档位 |
| `$CODEX_HOME/private-modalities.json` | 仅 `--configure-modalities`。哪些模型能贴图；默认取实测表，只在覆盖时才产生该文件 |
| `$CODEX_HOME/.gateway-models.stamp` | 上次刷新目录的时间戳，包装脚本靠它做节流 |
| `~/.local/bin/codex` | 仅 `--install-wrapper`。两行 shell 包装脚本，启动前刷新目录 |
| `codexcli/bin/codex-model` | 切模型的小脚本，跑配置向导时自动生成 |
| 网关 | 仅 `--apply-gateway-config`。私有模型的 `litellm_params`，**不改这个 Codex 每轮请求都 400/500** |

**每个文件在写入前都会备份成 `<文件名>.bak`**，`--restore` 可以一键还原。
`.bak` 里存的**永远是最初的原始版本**：反复跑脚本不会把备份覆盖成上一次的输出，所以 `--restore` 一定回到你没动过的状态。还原后 `.bak` 会被删掉，下次再配置时重新快照。

`config.toml` 采用**"文本手术"**而不是「解析→重新序列化」（`private-api/tomlpatch.py`）：这个文件是你自己手工维护的，里面有几百行 `[projects.'e:\...']` 反斜杠字面量、`[mcp_servers.*]`、`[plugins.*]`，重新序列化会把它们全搞乱。脚本只动它该动的那几行，**其余字节原样复制**。

---

## 自动检索哪些位置

| 类型 | 位置 |
|---|---|
| 配置 | `$CODEX_HOME/config.toml`，默认 `~/.codex/config.toml` |
| 登录态 | `$CODEX_HOME/auth.json` |
| Codex 自己拉的模型缓存 | `$CODEX_HOME/models_cache.json` |
| 二进制 | `which codex` 优先，找不到再扫 `~/.local/bin`、`~/.npm-global/bin`、`~/.cargo/bin`、`/usr/local/bin`、linuxbrew 等 |

想先看一眼结果、不做任何改动：

```bash
python3 private_api.py --detect     # 只列出找到的文件和版本
python3 private_api.py --status     # 文件 + 当前配置状态（推荐先跑这个）
```

`--status` 会告诉你：`config.toml` 里现在 pin 的模型、目录里有几个模型、**是否已登录**、有没有 per-model 覆盖、包装脚本装没装。

---

## 四个部件，缺一不可

### 1. `config.toml`：指向网关

```toml
model          = "deepseek-v4.1-flash-test"
model_provider = "private"
model_catalog_json = '/home/you/.codex/gateway-models.json'

[model_providers.private]
name = "Private Gateway"
base_url = "http://10.18.219.156:4000/v1"
wire_api = "responses"
requires_openai_auth = false
experimental_bearer_token = "sk-XXXXXXXX"
```

几个关键点：

**`base_url` 要带 `/v1`。** Codex 拿到 `base_url` 后自己拼 `/responses`。`--api-base` 你写带不带 `/v1` 都行，脚本会归一化。

**`requires_openai_auth = false`。** 这个值的默认本来就是 `false`，但显式写出来，免得将来某次合并配置时被别的 profile 带成 `true`——一旦是 `true`，Codex 会去找 ChatGPT 账号，而私有网关永远给不了。

**`wire_api` 只能是 `"responses"`。** Codex 0.150 **删掉了** `wire_api = "chat"`，还留着会**直接硬报错**：

```
`wire_api = "chat"` is no longer supported.
How to fix: set `wire_api = "responses"` in your provider config.
```

也就是说**网关必须能代理 `POST /v1/responses`**（LiteLLM 可以）。脚本在写配置前会先探一下这个端点，不通就当场告诉你，而不是让你等到第一句话才炸。

**key 放哪。** 默认是**内联**写进 `experimental_bearer_token`。为什么不默认用环境变量：Linux 服务器上你多半是 `docker exec` 或 ssh 上来跑，**非交互 shell 不会 source `~/.bashrc`**，环境变量根本不存在，Codex 会报找不到 `env_key`。想要环境变量就加 `--use-env-key`（脚本会把 `export PRIVATE_API_KEY=...` 追加进 `~/.bashrc`/`~/.zshrc`/`~/.profile`）。

安全性上两种都一样是明文落盘——`auth.json` 里无论如何都要存一份。

### 2. `model_catalog_json`：`/model` 列表的来源

**这是整个工具存在的理由。**

Codex **不会**像 Claude Code 那样去拉 `${base_url}/models`。它的模型列表来自一张**内置目录**，默认是 OpenAI 自家那套（GPT-5.6 Sol/Terra/Luna、GPT-5.5…），网关上**一个都没有**，选中了第一句话就失败。

`model_catalog_json` 指向一个 JSON 文件，**整份替换**掉内置目录。脚本从 `GET /v1/models` 拉到网关的模型，生成这份文件，所以 `/model` 里出现并且只出现网关的模型。

生成的目录长这样（`$CODEX_HOME/gateway-models.json`）：

```json
{
  "models": [
    {
      "slug": "deepseek-v4.1-flash-test",
      "display_name": "deepseek-v4.1-flash-test",
      "description": "Private gateway model · 128K context",
      "priority": 1,
      "visibility": "list",
      "supported_reasoning_levels": [
        { "effort": "low",   "description": "Fast responses with lighter reasoning" },
        { "effort": "high",  "description": "Greater reasoning depth for complex problems" },
        { "effort": "max",   "description": "Maximum reasoning depth for the hardest problems" }
      ],
      "default_reasoning_level": "high",
      "shell_type": "unified_exec",
      "supported_in_api": true,
      "support_verbosity": false,
      "truncation_policy": { "mode": "tokens", "limit": 10000 },
      "experimental_supported_tools": [],
      "base_instructions": "You are Codex, a coding agent. ...",
      "supports_reasoning_summaries": false,
      "supports_reasoning_summary_parameter": false,
      "supports_parallel_tool_calls": false,
      "context_window": 128000,
      "max_context_window": 128000,
      "apply_patch_tool_type": "freeform",
      "supports_search_tool": false,
      "input_modalities": ["text", "image"]
    }
  ]
}
```

几个字段值得单独说：

| 字段 | 为什么这么填 |
|---|---|
| `slug` | 用网关的 model id **原样**。Codex 把它当 `model` 发出去，LiteLLM 就按这个字符串路由 |
| `visibility: "list"` | 只有 `list` 才进 `/model` 选择器。Codex 自家的 `gpt-reserve` 用的是 `hide` |
| `base_instructions` | 必填二选一（另一个是 `model_messages.instructions_template`）。填的是 Codex 的系统提示词，不填整个目录解析失败 |
| `apply_patch_tool_type: "freeform"` | **不填 Codex 就不提供 `apply_patch` 工具，agent 改不了文件** |
| `supports_reasoning_summaries`<br>`supports_reasoning_summary_parameter` | **同一个字段改名前后两种写法，两个都发**——见[版本兼容](#版本兼容codex-的版本敏感字段)。都填 `false`，理由见下 |
| `supports_parallel_tool_calls: false` | 0.143~0.154 必填。关掉是因为没有证据表明后端支持并行工具调用，填错只在任务中途才暴露 |
| `supports_search_tool: false` | 联网搜索会被代理回 OpenAI 后端，不是网关，所以关掉 |
| `context_window` | 网关照实报 `max_input_tokens` 时用它，没报就退回 **128K**。实测 `deepseek-v4.1-flash-test`、`glm-5.3-flash`、`qwen3-5-397b`、`xinghai-ultra` 这四个网关没报，都落在 128K |

> **两个 summary 开关为什么必须是 `false`**：它决定 Codex 会不会往请求里塞 `reasoning.summary`。2026-09-17 实测，网关对这个参数直接 **400**：
> ```
> litellm.BadRequestError: Custom_openaiException - Invalid OpenAI-compatible chat request:
> invalid type: map, expected a string
> ```
> 这和当初 `reasoning_effort` 那个坑是同一类。更要命的是 **0.145+ 这个字段的默认值是 `true`**——不显式写 `false`，新版本 codex 每轮都会带上 `reasoning.summary` 然后 400。

目录里**只放能聊天的模型**：`gpt-image-*` 那 4 个图像端点会被跳过——Codex 会愉快地接受这个 slug，然后在你第一句话时失败。

`base_instructions` 和这些字段是**必填的**：`serde` 缺字段会一行一行报 `missing field \`x\``，`models` 是空数组则报 `must contain at least one model`。

### 版本兼容：codex 的版本敏感字段

**目录格式在 0.14x 期间改过名，写错一边就会让 codex 起不来。** 这就是本工具要发三个"多余"字段的原因：

| 版本 | `supports_reasoning_summaries` | `supports_reasoning_summary_parameter` | `supports_parallel_tool_calls` |
|---|---|---|---|
| 0.143 ~ 0.144 | **必填** | —— | **必填** |
| 0.145 ~ 0.154 | （已改名，忽略） | 选填，**默认 `true`** | **必填** |
| 0.155+ | （已删除，忽略） | 选填，默认 `true` | （已删除，忽略） |

本工具**三个都发**。这是安全的：从 0.143 到 main，`ModelInfo` 都没有 `#[serde(deny_unknown_fields)]`，**不认识的 key 直接忽略**，所以同一份目录在所有版本上都能加载。

> **实际踩过的坑**：Linux 那台 `codex-cli 0.144.1` 启动时报
> `missing field \`supports_reasoning_summaries\` at line 40 column 5`
> ——0.144 要求这个字段，而当时的目录没发。修完它之后还会连着报 `supports_parallel_tool_calls`，因为 serde **一次只报一个**。

**本工具适配的版本范围**（`private-api/codex.py` 里的 `MIN_SUPPORTED_VERSION` / `TESTED_THROUGH_VERSION`）：

| | |
|---|---|
| 最低支持 | **0.143**（必填字段集来自该版本源码 + 其自带的 JSON 测试用例） |
| 实测通过 | **0.153.4**（`codex debug models` 干净退出，Windows 侧实测） |
| 已在用 | **0.144.1**（Linux 服务器，用户实际部署） |

**每次运行都会报版本**，`--setup` / `--status` / `--detect` 都会带一行：

```
  codex: codex-cli 0.144.1 is in the supported range (0.143 .. 0.153.4)
```

不在范围内时前缀 `!`，并且 `--sync` 只在**真正重写了目录**的那次才提示（节流跳过的那次不跑 `codex --version`，避免拖慢每次启动）：

```
  ! codex-cli 0.142.0 is OLDER than the oldest supported release (0.143). The catalog
    it writes may be rejected at startup -- upgrade codex, or expect `missing field`
    errors from its model catalog parser.
```

升级/降级 codex 后，先跑 `python3 private_api.py --detect` 看这一行，再跑 `codex debug models` 做权威确认。

**证据一**：目录里只列网关模型时，`codex -m gpt-5.6-sol` 会打印 `Model metadata for 'gpt-5.6-sol' not found. Defaulting to fallback metadata`——说明这份目录是**替换**而不是**追加**。

**证据二（推荐随时自己验）**：`codex debug models` 让 **codex 二进制自己**把当前生效的目录 dump 成 JSON。目录写得对不对，这是唯一的权威答案——serde 缺字段会在这里报 `missing field`，而我们这份它是干净退出的：

```bash
codex debug models            # 当前生效的目录（会读 model_catalog_json）
codex debug models --bundled  # 对比：codex 内置的那份，不联网
```

实测 `codex-cli 0.153.4` + 本工具生成的目录：

```
count: 10
  deepseek-v4-flash        vis=list  efforts=low,high,max                mods=text        ctx=1000000
  deepseek-v4.1-flash-test vis=list  efforts=low,high,max                mods=text,image  ctx=128000
  glm-5.3-flash            vis=list  efforts=low,high,max                mods=text,image  ctx=128000
  gpt-5.5                  vis=list  efforts=low,medium,high,xhigh       mods=text,image  ctx=1050000
  gpt-5.6-sol              vis=list  efforts=low,medium,high,xhigh,max,ultra  mods=text,image  ctx=922000
  qwen3-5-397b             vis=list  efforts=low,high,max                mods=text,image  ctx=128000
  xinghai-ultra            vis=list  efforts=low,high,max                mods=text        ctx=128000
  ...
```

10 个模型、全部 `vis=list`（都会出现在 `/model` 里）、档位/图片能力与配置一致，且**没有任何网络请求**（`model_catalog_json` 一设，Codex 就改用静态目录，不再去后端拉 `/models`）。

> 注意 `codex debug models` 会**优先从后端拉一次**（`OnlineIfUncached`），只有设了 `model_catalog_json` 才走静态目录。想完全离线用 `--bundled` 那种对比方式。

### 3. `auth.json`：不写它就一直弹登录

**这是最反直觉的一条。**

即使 `model_provider` 已经指向私有网关、`requires_openai_auth = false`，Codex **照样**会弹登录界面：

```bash
$ codex login status
Not logged in
```

因为 Codex 判断"是否已登录"的方法就是**看 `$CODEX_HOME/auth.json` 存不存在**，和这一轮的请求会走哪个 provider 无关。文件不在，就要登录——provider 表里写什么都没用。

修法：往里写一个 **API key**（Codex 本来就有 apikey 模式）：

```json
{
  "OPENAI_API_KEY": "sk-XXXXXXXX",
  "tokens": null,
  "last_refresh": null
}
```

写进去的值就是**网关的 key**——provider 表本来就用它认证，所以这没有引入新密钥，只是把一个**已经在这台机器上的**密钥（`config.toml` 里）挪到 Codex 真正会读的那个文件里。

单独处理登录（会**先验证 key 真能用**，被网关拒绝就不写，免得看起来登录了实际每轮都失败）：

```bash
python3 private_api.py --fix-login          # 已有登录态时不覆盖
python3 private_api.py --fix-login --force  # 强制重写
```

⚠️ **已经用 ChatGPT 账号登录过的话，配置向导不会覆盖它**。一个真的 ChatGPT 登录**已经满足**了登录闸门，而私有 provider 根本不会读它那份 token，覆盖掉纯属白扔一个能用的账号。要强行覆盖用 `--fix-login --force`。

### 4. 自动刷新：`codex` 包装脚本

网关加了模型，`/model` 得能看到。两种办法：

```bash
python3 private_api.py --refresh-models     # 手动刷新一次
python3 private_api.py --install-wrapper    # 装包装脚本，每次启动自动刷
```

`--install-wrapper` 会在 `~/.local/bin/codex` 写一个 shell 脚本，exec 真正的 codex 之前**先刷一次目录**。它的设计约束是**绝不能拖慢或挡住 codex 启动**：

- **节流**：`$CODEX_HOME/.gateway-models.stamp` 在 300 秒内就不刷（`--refresh-ttl` 可调）；
- **永远退出 0**：网关慢、挂了、token 过期，都只是"继续用上次那份目录"，codex 照常启动。`--sync` 里所有异常都被吞掉；
- **超时 8 秒**：故意不耐烦。宁可让你拿一份旧列表，也不要卡住。

```bash
python3 private_api.py --sync --force   # 手动跑一次看它干了什么（不加 --force 是静默的）
python3 private_api.py --uninstall-wrapper
```

⚠️ **包装脚本要生效，`~/.local/bin` 必须在 `PATH` 里**，而且要比真实 codex 所在的目录靠前。脚本装完会检查并提示：

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
```

没装包装脚本也完全能用，只是目录不会自动更新（`/model` 里看不到新加的模型）。

---

## 思考档位（reasoning）

Codex 的思考强度菜单**完全由我们生成的目录决定**，两个字段：

- `supported_reasoning_levels` —— 菜单里有哪几档（**是对象数组，不是字符串数组**，写 `["low","high"]` 会让整个目录解析失败、Codex 起不来）
- `default_reasoning_level` —— 打开时停在哪一档

**默认给私有模型的是 `low / high / max` 三档**（对应设计需求里"custom-openai 仅支持三级"）。

哪些档位"有意义"是实测出来的——对着本部署的后端逐个模型、逐个档位打 `POST /v1/responses`：

| 模型 | 实测结果 |
|---|---|
| `deepseek-v4.1-flash-test` | `low high xhigh max` → 200；`minimal medium` → **400**（"reasoning_effort must be low, high, xhigh, max, or an integer within [1, 10]"） |
| `deepseek-v4-flash`、`glm-5.3-flash`、`qwen3-5-397b`、`xinghai-ultra` | 所有值都 200——**但后端根本不校验**，`medium` 收下就悄悄忽略，**所以 200 什么都证明不了** |

注意严格后端实际接受的是**四档**，`xhigh` 是 `max` 下面紧邻的一档，并不是 `max` 的同义词。它不在默认值里（需求要三档），但**一个参数就能加回来**：

```bash
python3 private_api.py --configure-reasoning \
    --reasoning-model deepseek-v4.1-flash-test \
    --reasoning-levels xhigh          # low,high,xhigh,max —— 当需要那额外一档时
```

**`minimal` 和 `medium` 故意不给**：严格后端上它们每轮都是 400，宽松后端上它们和 `high` 没区别——一个毫无意义却看着像选项的档位。

> ⚠️ **去掉 `xhigh` 的代价是 TUI 上的一个细节**：Codex 把 `max`（和 `ultra`）归到 **"Advanced Reasoning"** 子菜单里（带 `⚠ Consumes usage limits faster` 警告），所以普通思考菜单现在只显示 `low` / `high`，**选 `max` 要按快捷键进高级子菜单**。留着 `xhigh` 的话它本来能在普通菜单里直接选到。

简写档位方案：

| 简写 | 展开 |
|---|---|
| `private` | `low,high,max`（默认） |
| `three` | `low,high,max`（同上，别名） |
| `xhigh` | `low,high,xhigh,max`（严格后端的完整集合） |
| `none` | 空——**该模型没有思考菜单** |

也可以直接写逗号分隔的档位，或交互式选：

```bash
python3 private_api.py --configure-reasoning --reasoning-model glm-5.3-flash
python3 private_api.py --configure-reasoning --reasoning-model glm-5.3-flash \
    --reasoning-levels low,high,max --reasoning-default high
python3 private_api.py --reasoning-clear glm-5.3-flash   # 清单个
python3 private_api.py --reasoning-clear all             # 清全部
```

覆盖存在 `$CODEX_HOME/private-reasoning.json`，和 `vscode/` 那份**同名同格式**，两边互通。改完记得 `--refresh-models` 重新生成目录。

**OpenAI 自家模型（`gpt-*`/`o*`）的档位是只读的**，从 `$CODEX_HOME/models_cache.json` 原样抄——因为我们的目录会**替换**掉 OpenAI 那份，不抄一遍就等于把官方模型的能力覆盖没了。没登录过的机器没有这个缓存，退回保守的 `low/medium/high/xhigh`。

⚠️ **如果 `config.toml` 根上有 `model_reasoning_effort`，上面全都不生效。** 那是个全局默认值，**优先级高于每个模型的 `default_reasoning_level`**——菜单要么整份消失，要么所有模型都卡在同一档。脚本在写入目录时会**自动删掉这一行**并告诉你。

---

## 上下文窗口（context_window）

目录里的 `context_window` / `max_context_window` 决定 Codex **什么时候压缩历史**，而**不是**一个客户端硬上限。这一点容易搞反，所以在源码里核实过（`codex-rs/protocol/src/openai_models.rs` + `codex-rs/core/src/session/`）：

| 量 | 算法 | 默认 |
|---|---|---|
| 有效窗口 | `context_window`，缺省时退回 `max_context_window` | 本工具填网关的 `max_input_tokens` |
| 压缩触发线 | `auto_compact_token_limit()` = `min(配置值, 有效窗口 × 90%)` | 窗口的 **90%** |
| 硬上限线 | 有效窗口 × `effective_context_window_percent` | 窗口的 **95%** |

两条线任意一条被触到 → `token_limit_reached` → `run_auto_compact(..., CompactionReason::ContextLimit)`。**是压缩（roll over 到新上下文窗口），不是拒绝请求。** 客户端**不会**因为 `context_window` 而硬报错。

唯一会硬报错的是**后端**拒了这次请求——上游返回 `context_length_exceeded` 时，Codex 才抛 `ContextWindowExceeded`（`codex-api/src/sse/responses.rs` 解析该错误码）。

**所以 `context_window` 填高了比填低了危险：**

- **填低** → 压缩偏早，浪费一点窗口，但**安全**。
- **填高** → Codex 放心发出超长请求，由**网关** 400 掉，用户看到的是任务中途失败。

本工具取网关 `GET /v1/models` 报的 `max_input_tokens`；网关没报就退回 **128K**（`DEFAULT_CONTEXT_WINDOW`，[`private-api/codex.py`](private-api/codex.py)）。LiteLLM 只给它有元数据的模型填这个字段，实测本部署有四个模型没报、都落在 128K。要让某个模型更贴合真实能力，直接在网关侧补 `max_input_tokens` 元数据最省事。

---

## 能不能贴图（input_modalities）

Codex **从不问模型能不能看图**，它问目录，然后**在客户端就拒掉附件**。所以 `input_modalities` 填错：

- 填宽了 → 请求发出去，上游失败（或者更糟，模型对着图**胡说**）
- 填窄了 → 用户根本贴不进图

所以这张表是**测出来的，不是声明的**：发一张纯红、一张纯蓝的 PNG，看模型能不能分辨。

| 模型 | 结论 | 依据 |
|---|---|---|
| `deepseek-v4.1-flash-test` | **image** | 正确答出 Red / Blue |
| `glm-5.3-flash` | **image** | 正确答出 Red / Blue |
| `qwen3-5-397b` | **image** | 正确答出 Red / Blue |
| `deepseek-v4-flash` | text | **HTTP 400** "Model only supports text input; received unsupported content type 'image_url'"——上游直接拒，开了每轮都炸 |
| `xinghai-ultra` | text | **200，但没在看图**：一次答 "Black"/"Orange"，另一次直接拒答 "I am unable to determine"。**没有报错可察觉**，这正是要实测的原因 |

重新测量（后端换版本后建议跑一次）：

```bash
python3 private_api.py --probe-modalities
python3 private_api.py --probe-modalities --probe-models glm-5.3-flash   # 只测一个
```

按结论开启 image：

```bash
python3 private_api.py --configure-modalities \
    --modalities-model glm-5.3-flash --modalities text,image
```

测出来能看图的模型，脚本会直接把上面这行命令打出来给你抄。

覆盖存在 `$CODEX_HOME/private-modalities.json`。**OpenAI 自家模型的 input types 同样是只读的**，从 `models_cache.json` 抄——这条以前出过事：目录里给所有模型硬编码 `["text"]`，把官方模型的贴图能力也一起没收了。

---

## 网关（LiteLLM）侧改动

**不改这里，Codex 对着私有模型每一轮请求都失败。** 两个根因，都必须在网关上解决——因为 Codex 二进制没法告诉它别发这两个字段。

**① `reasoning.effort` → HTTP 400**

Codex 每轮都带 `reasoning: {"effort": ...}`，LiteLLM 把它映射成 `reasoning_effort`，而 `custom_openai` 没声明这个参数，**代理在请求离开之前就拒了**：

```
litellm.UnsupportedParamsError: custom_openai does not support parameters:
['reasoning_effort'], for model=deepseek-v4-flash
```

**② `client_metadata` → HTTP 500**

Codex 每轮都挂一个 `client_metadata` 对象（session/turn id、沙箱模式、install id）。LiteLLM 1.100.1 **原样转发给 OpenAI SDK**，而 `create()` 没这个关键字参数：

```
Custom_openaiException - AsyncCompletions.create() got an unexpected
keyword argument 'client_metadata'
```

**修法**（两条都进模型定义的 `litellm_params`）：

```json
"allowed_openai_params": ["...", "reasoning_effort"],
"additional_drop_params": ["client_metadata"]
```

丢 `client_metadata` 没有任何代价——那是 Codex 自己的遥测信封，不是模型输入。

```bash
# 1) 先看 —— 只打印 curl，一个字节都不改网关
python3 private_api.py --emit-gateway-config

# 2) 确认无误再改。POST /model/update REPLACES litellm_params，
#    所以每条命令都会重发该模型的完整参数集 + 新增的那个 key。
#    需要 LiteLLM MASTER key（不是虚拟 key），且会影响网关上的所有用户
python3 private_api.py --apply-gateway-config
```

脚本**拒绝**发送缺 `api_base` 的请求体——那会把 `api_base` 和 `custom_llm_provider` 一起抹掉，把模型对所有人搞坏。

**当前状态（上次核对）：5 个私有模型全部已修好，无需再动。**

```
already accept Codex's Responses fields (5):
    deepseek-v4-flash  deepseek-v4.1-flash-test  glm-5.3-flash
    qwen3-5-397b       xinghai-ultra
not applicable (9): gpt-* / gpt-image-*  provider openai is out of scope
```

用 `--emit-gateway-config` 随时复验，输出 "nothing to do" 就是好的。

---

## 命令速查

```bash
# 交互式向导（推荐第一次用）
python3 private_api.py

# 一次配好
python3 private_api.py --api-base http://10.18.219.156:4000 --api-key sk-XXX
python3 private_api.py --api-base ... --api-key sk-XXX --model deepseek-v4.1-flash-test
python3 private_api.py --api-base ... --api-key sk-XXX --install-wrapper   # 顺带装自动刷新

# 只看不动
python3 private_api.py --detect        # 找到哪些文件
python3 private_api.py --status        # 当前配置状态（先跑这个）
python3 private_api.py --list-models   # 网关到底提供哪些模型

# 模型
python3 private_api.py --switch-model            # 交互式挑
python3 private_api.py --switch-model --model gpt-5.6-sol
./bin/codex-model                                # 上一条的快捷方式
python3 private_api.py --refresh-models          # 刷新目录
python3 private_api.py --sync --force            # 手动跑一次"启动时刷新"

# 登录
python3 private_api.py --fix-login               # 写 auth.json，不再弹登录
python3 private_api.py --fix-login --force

# 每个模型的思考档位（私有模型默认已是三档；xhigh 可加回第四档）
python3 private_api.py --configure-reasoning --reasoning-model <id> --reasoning-levels private
python3 private_api.py --configure-reasoning --reasoning-model <id> --reasoning-levels xhigh
python3 private_api.py --reasoning-clear all

# 能否贴图
python3 private_api.py --probe-modalities
python3 private_api.py --configure-modalities --modalities-model <id> --modalities text,image

# 网关侧参数（400 + 500 的两个根因）
python3 private_api.py --emit-gateway-config      # 只打印
python3 private_api.py --apply-gateway-config     # 真发送，需要 master key

# 包装脚本
python3 private_api.py --install-wrapper
python3 private_api.py --uninstall-wrapper

# 还原
python3 private_api.py --restore
```

---

## 常见问题

**Q：`codex` 里 `/model` 还是显示 GPT-5.6 Sol/Luna/Terra，没有网关的模型？**
`model_catalog_json` 没写进去，或者文件路径不对。先 `--status` 看 `model catalog` 那一段。另外根上的 `model_reasoning_effort` 会让档位显示不对（见[思考档位](#思考档位reasoning)），但**不影响模型列表本身**。

**Q：`codex` 打开就要登录 / `codex login status` 说 Not logged in？**
`auth.json` 不在。跑 `--fix-login`。provider 表里写了什么都不管用——Codex 就是看这个文件在不在。

**Q：`codex` 直接起不来，报 `failed to parse model_catalog_json ... missing field \`x\``？**
目录格式和你的 codex 版本对不上。**先看 `--status` 里那一行版本提示**——低于 0.143 就需要升级 codex。0.143~0.155 之间的改名问题本工具已经用"两种写法都发"覆盖了，如果你用的是本工具生成的目录还报这个错，说明是**别的字段**：serde 一次只报一个，照着报错字段名逐个补。完整字段集见[版本兼容](#版本兼容codex-的版本敏感字段)。

**Q：第一句话就 400，错误里有 `reasoning.summary` 或 `invalid type: map, expected a string`？**
网关不吃 `reasoning.summary`。目录里的 `supports_reasoning_summaries` / `supports_reasoning_summary_parameter` 必须都是 `false`（本工具默认如此）。如果你手改过目录，或者用的旧版工具生成的目录，重新 `--refresh-models` 生成一份。

**Q：第一句话就 400？**
网关侧 `allowed_openai_params` 缺 `reasoning_effort`。跑 `--emit-gateway-config` 看，然后按提示修。

**Q：第一句话就 500？**
网关侧 `additional_drop_params` 缺 `client_metadata`。同上。

**Q：提示 `` `wire_api = "chat"` is no longer supported ``？**
Codex 0.150 删掉了 chat 线协议。网关必须能代理 `/v1/responses`。LiteLLM 可以；如果你们的网关只能聊 `/v1/chat/completions`，那 Codex 驱动不了它。脚本在写配置前会先探这个端点。

**Q：报找不到 `env_key`？**
你用了 `--use-env-key`，但当前 shell 是非交互的（`docker exec`、cron、systemd），不 source `~/.bashrc`。**去掉 `--use-env-key` 用默认的内联方式**，这是服务器上推荐的做法。

**Q：包装脚本装了但目录不自动更新？**
`~/.local/bin` 不在 `PATH` 里，或者排在真实 codex 后面。`--status` 的 `codex wrapper` 段会告诉你 `on PATH: yes/NO`。

**Q：某个模型贴不进图？**
看[实测表](#能不能贴图input_modalities)。`deepseek-v4-flash` 和 `xinghai-ultra` 是故意关掉的——前者上游直接 400，后者会对着图编答案。

**Q：配置写坏了想回滚？**
`python3 private_api.py --restore`。它把 `config.toml.bak`、`auth.json.bak` 放回去，删掉生成的目录文件，卸掉包装脚本。**per-model 的 reasoning/modalities 覆盖会保留**，要清用 `--reasoning-clear all` / `--modalities-clear all`。

---

## 目录结构

```
codexcli/
├── README.md              # 本文档
├── 设计需求.md
├── private_api.py         # 入口脚本，所有命令都从这里走（唯一直接执行的）
├── bin/
│   └── codex-model        # 切模型的快捷方式（跑向导时生成，可删）
└── private-api/           # 模块，不会被直接调用
    ├── codex.py           # config.toml 读写 + 模型目录生成 + 包装脚本
    ├── codex_auth.py      # auth.json（免登录）
    ├── detect.py          # 找 $CODEX_HOME、config、auth、models_cache、codex 二进制
    ├── gateway.py         # 调网关：列模型、探 /v1/responses
    ├── reasoning.py       # 每个模型的思考档位
    ├── modalities.py      # 每个模型的输入类型（能否贴图）+ 实测探针
    ├── litellm_admin.py   # 网关侧 litellm_params 修复
    └── tomlpatch.py       # 保留原格式的 TOML 行编辑
```

`vscode/` 那份是同一套思路在编辑器插件上的实现（Claude Code + Codex 插件）。两份共用 `private-reasoning.json` / `private-modalities.json`，同一台机器都配过也没问题。
