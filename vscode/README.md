# private-api —— 把 VSCode 的 Claude Code / Codex 插件指向私有化网关

一条命令，把编辑器里这两个插件从官方 API 切到你们自己的 LiteLLM 网关：

```bash
python private_api.py --target both \
    --api-base http://10.18.219.156:4000 \
    --api-key  sk-XXXXXXXX
```

配置文件的路径**不需要自己找**——脚本会自动扫描所有可能的位置（见下面的[自动检索](#自动检索哪些位置)），没有的会创建。
不带 `--target` 在终端里跑，会进入交互式向导。

**web search 也在这条命令里配好了**，不用再跑第二条：两个插件各自注册一个指向网关的 MCP 端点，凭据复用你刚填的那把 LiteLLM key —— 全程只有**一把 key**，没有第二个 token 要申请。细节见[搜索（web search）](#搜索web-search)。

只用标准库，不需要 `pip install`。

> 这里此前写的是"需要 Python 3.11+（用到标准库 `tomllib`）"——**那是错的**：全仓库没有一处 `import tomllib`（它只在 `tomlpatch.py` 的一句注释里被提到，用来说明"标准库能读 TOML 但不能写，所以这个模块按行编辑")。已实测的是 Python 3.13；代码里没有 3.10+ 才有的语法（没有 `match`、没有运行期的 `X | Y` 联合类型，标注统一靠 `from __future__ import annotations` 延迟求值），所以 3.8+ 理论上都行，但没有在旧版本上验证过。

入口脚本是 `vscode/private_api.py`，下面所有命令都假设你在 `vscode/` 目录里执行；在仓库根目录跑的话把 `private_api.py` 换成 `vscode/private_api.py` 即可。

## 插件安装（.vsix 下载）

这两个插件的 `.vsix` **不随仓库提交**——加起来 1.2 GB 以上，进 git 不划算。版本号记在这里，需要时按下面任意一种方式拿原版安装包。

| 插件 | 版本 | 平台 | 大小 |
|---|---|---|---:|
| Claude Code（`anthropic.claude-code`） | 2.1.89 | 全平台共用一个包 | 18.45 MB |
| Codex（`openai.chatgpt`） | 26.5908.31748 | `win32-x64` | 384.67 MB |
| Codex（`openai.chatgpt`） | 26.5908.31748 | `win32-arm64` | 367.84 MB |
| Codex（`openai.chatgpt`） | 26.5908.31748 | `linux-x64` | 238.39 MB |
| Codex（`openai.chatgpt`） | 26.5908.31748 | `darwin-x64` | 232.85 MB |

**方式一：让 VSCode 自己下（推荐）**——`code` 会按当前平台挑对的那个 Codex 包：

```bash
code --install-extension anthropic.claude-code@2.1.89
code --install-extension openai.chatgpt@26.5908.31748
```

**方式二：直接下载 `.vsix`**——把版本号填进这个直链，`targetPlatform` 只有 Codex 这种按平台拆包的插件才需要（完整来源是 VS Code 官方 Marketplace，`publishers/<publisher>/vsextensions/<extension>/<version>/vspackage`）：

```
# Claude Code：全平台一个包，不带 targetPlatform
https://marketplace.visualstudio.com/_apis/public/gallery/publishers/anthropic/vsextensions/claude-code/2.1.89/vspackage

# Codex：按平台取，取值 win32-x64 / win32-arm64 / linux-x64 / linux-arm64 / darwin-x64 / darwin-arm64
https://marketplace.visualstudio.com/_apis/public/gallery/publishers/openai/vsextensions/chatgpt/26.5908.31748/vspackage?targetPlatform=win32-x64
```

下完用 `code --install-extension <文件>.vsix` 装。不想拼 URL 就去插件页点右侧的 Download Extension：<https://marketplace.visualstudio.com/items?itemName=openai.chatgpt>、<https://marketplace.visualstudio.com/items?itemName=anthropic.claude-code>。

> 版本号要卡准：`--patch-only` 改的是 Claude Code 插件自带的 `claude.exe`，插件一升级就被换回原版，补丁会丢（见 [Claude Code 模型名过滤](#模型名过滤本工具存在的主要原因)）。Codex 那个插件不靠二进制补丁，但也建议先按这里的版本校验一遍，再决定要不要升级。

---

## 目录

- [插件安装（.vsix 下载）](#插件安装vsix-下载)
- [做了什么](#做了什么)
- [自动检索哪些位置](#自动检索哪些位置)
- [Claude Code](#claude-code)
- [Codex](#codex)
  - [模型列表（`model_catalog_json`）](#模型列表model_catalog_json)
  - [⚠️ 本机代理会让 Codex 报 503](#️-本机代理会让-codex-报-503)
- [疑难根因与修复](#疑难根因与修复)
  - [Codex 请求私有模型全部 400](#-codex-请求私有模型全部-400)
  - [修完 400 之后变成 500（client_metadata）](#-修完-400-之后变成-500client_metadata)
  - [qwen3-5-397b：上游只认"系统消息在最前"（已确认暂不修）](#️-qwen3-5-397b上游只认系统消息在最前已确认暂不修)
  - [Codex 打开就要求登录](#-codex-打开就要求登录)
  - [模型能力配置：`model-config.jsonc`](#模型能力配置model-configjsonc)
  - [每个模型配思考档位](#-每个模型配思考档位)
  - [图片贴不进去：input_modalities](#️-图片贴不进去input_modalities)
- [搜索（web search）](#搜索web-search)
- [网关（LiteLLM）侧改动：脚本与用法](#网关litellm侧改动脚本与用法)
- [命令速查](#命令速查)
- [常见问题](#常见问题)

---

## 做了什么

| 插件 | 改动的东西 | 说明 |
|---|---|---|
| Claude Code | `<编辑器>/User/settings.json` 里的 `claudeCode.environmentVariables` | 插件启动 CLI 时注入的环境变量 |
| Claude Code | `~/.claude/settings.json` 的 `env` 块 | 终端里直接跑 `claude` 时生效 |
| Claude Code | 插件自带的 `claude.exe` | **解除硬编码的 `/(claude\|anthropic)/i` 模型名过滤**，见下 |
| Claude Code | `~/.claude/settings.json` 的 `modelPicker` 块 | **把 `/model` 列表换成网关模型**，不再显示内置的 Opus/Sonnet/Haiku |
| Codex | `~/.codex/config.toml` | 写 `[model_providers.private]` + `model` + `model_catalog_json` |
| Codex | `~/.codex/gateway-models.json` | **把模型下拉框换成网关模型**，不再显示 GPT-5.6 Sol/Terra/Luna 这些网关上没有的（见下） |
| Codex | `~/.codex/auth.json` | 仅 `--fix-login`。**不写这个 Codex 永远弹登录**（见 [Codex 要求登录](#-codex-打开就要求登录)） |
| Codex | `vscode/model-config.jsonc` | **模型能力配置**（能不能贴图 / 几档思考 / 上下文多长），生成在**脚本旁边**、归你所有。改完用 `--apply-model-config` 同步进目录。**不在 `~/.codex` 里**，也**不随包发**（见 [模型能力配置](#模型能力配置model-configjsonc)） |
| Codex | `~/.codex/private-reasoning.json` | 仅 `--configure-reasoning`。旧的按模型档位覆盖，**仍读取但优先级低于 `model-config.jsonc`**（见 [思考模式](#-每个模型配思考档位)） |
| Codex | `~/.codex/private-modalities.json` | 仅 `--configure-modalities`。同上，**仍读取但优先级更低**（见 [图片贴不进去](#️-图片贴不进去input_modalities)） |
| Claude Code | `~/.claude.json` 的 `mcpServers` | 主流程最后一步自动写，也可用 `--configure-search` 单独重配。**让模型能搜索**；注意**不是** `settings.json`（见下） |
| Codex | `~/.codex/config.toml` 的 `[mcp_servers.searxng]` | 同上，也由主流程自动写。两个插件共用同一个 MCP 端点，用的是同一个 LiteLLM key |
| Codex | `PRIVATE_API_KEY` 用户级环境变量 | 或改用 `--inline-key` 直接写进配置（见下） |
| 网关 | 私有模型的 `litellm_params.allowed_openai_params` | 仅 `--apply-gateway-config`。**不改这个，Codex 每轮请求都 400**（见 [Codex 全 400](#-codex-请求私有模型全部-400)） |
| 网关 | 私有模型的 `litellm_params.additional_drop_params` | 仅 `--apply-gateway-config`。**不改这个，Codex 每轮请求都 500**（见 [Codex 变 500](#-修完-400-之后变成-500client_metadata)） |

**每个文件在写入前都会备份成 `<文件名>.bak`**，`--restore` 可以一键还原。
`.bak` 里存的**永远是最初的原始版本**：反复跑脚本不会把备份覆盖成上一次的输出，所以 `--restore` 一定回到你没动过的状态。还原后 `.bak` 会被删掉，下次再配置时重新快照。

新机器上 `~/.claude/settings.json` 可能还不存在，脚本会**直接创建**（连同 `~/.claude/` 目录）。这种情况下的 `.bak` 是一个**空文件**，表示"原来没有这个文件"——`--restore` 会把创建出来的文件删掉，而不是恢复成空文件。同一轮里被改两次的文件（比如先写 `env`、再写 `modelPicker`），快照在动笔**之前**就取好了，所以 `.bak` 永远是"跑脚本之前"的状态。

写入采用"文本手术"而不是「解析→重新序列化」，所以 `settings.json` 里的 **`//` 注释、键顺序、缩进全部原样保留**。

---

## 自动检索哪些位置

### Claude Code

按可能性从高到低扫描，只保留真实存在的：

| 类型 | Windows | macOS / Linux |
|---|---|---|
| 编辑器用户设置 | `%APPDATA%\{Code, Code - Insiders, VSCodium, Cursor, Windsurf, Trae, Trae CN}\User\settings.json` | `~/Library/Application Support/...`、`~/.config/...` |
| 远程开发机器设置 | `~/.vscode-server/data/Machine/settings.json` | 同左 |
| 工作区设置 | 从当前目录往上逐级找 `.vscode/settings.json` | 同左 |
| CLI 设置 | `~/.claude/settings.json`、各级 `.claude/settings.json` | 同左 |
| 自带二进制 | `~/.{vscode,vscode-insiders,cursor,windsurf,vscode-server}/extensions/anthropic.claude-code-*/resources/native-binary/claude.exe` | 同上，文件名 `claude` |

### Codex

| 类型 | 位置 |
|---|---|
| 配置 | `$CODEX_HOME/config.toml`，默认 `~/.codex/config.toml` |
| 登录态 | `~/.codex/auth.json` |
| 二进制 | 插件内置 `~/.vscode/extensions/openai.chatgpt-*/bin/<平台>/codex.exe`，找不到再找 `PATH` |

**新机器上这些文件大多还不存在**，脚本会把该建的建出来（`--detect` 里标 `(will be created)`）：`~/.claude/settings.json` 和 `~/.claude/` 会直接创建；编辑器设置同理——只要 VSCode 装过 Claude Code 扩展，即使 `User/settings.json` 还没被保存过，也会按扩展的安装位置找对目录再创建。

想先看一眼结果、不做任何改动：

```bash
python private_api.py --detect     # 只列出找到的文件
python private_api.py --status     # 列出文件 + 当前配置状态
```

---

## Claude Code

### 写入的环境变量

`ANTHROPIC_BASE_URL` 和 `ANTHROPIC_AUTH_TOKEN` 是**必写**（这是本工具的目的）；其余只在缺失时补默认值，你手工调过的值不会被覆盖：

| 变量 | 值 | 作用 |
|---|---|---|
| `ANTHROPIC_BASE_URL` | 你给的地址（**不含 `/v1`**，Claude Code 自己会拼 `/v1/messages`） | 指向网关 |
| `ANTHROPIC_AUTH_TOKEN` | 你给的 key | 鉴权 |
| `API_TIMEOUT_MS` | `3000000` | 默认 60s 对慢网关不够 |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | `1` | 关掉会打往公网的统计/遥测请求 |
| `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY` | `1` | 让 `/model` 列表改为从 `${BASE}/v1/models` 拉取 |

同时会写 `"claudeCode.disableLoginPrompt": true`——不然插件会弹浏览器等你走一个永远走不完的 OAuth。

传了 `--model` 的话，下面五个一起写（Claude Code 的 opus/sonnet/haiku 是分别解析的，只设 `ANTHROPIC_MODEL` 不够）：

```
ANTHROPIC_MODEL
ANTHROPIC_SMALL_FAST_MODEL
ANTHROPIC_DEFAULT_OPUS_MODEL
ANTHROPIC_DEFAULT_SONNET_MODEL
ANTHROPIC_DEFAULT_HAIKU_MODEL
```

### 模型名过滤（本工具存在的主要原因）

打开 `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1` 后，Claude Code 会请求网关的 `/v1/models`，然后**丢掉所有名字里不含 `claude` / `anthropic` 的模型**：

```js
j.data.data.filter((F) => /(claude|anthropic)/i.test(F.id))   // gatewayDiscovery
c.data.data.filter((r) => /(claude|anthropic)/i.test(r.id))   // bootstrap options
```

这个正则是**硬编码的**，没有任何环境变量或设置项能放宽它。所以网关上 14 个模型，`/model` 里只看得见 1 个。跑一次脚本就能看到你被丢了哪些：

```
  models Claude Code can see WITHOUT the binary patch: 1
      claude-deepseek-v4.1-flash-test
  models it silently drops: 13
      deepseek-v4-flash
      glm-5.3-flash
      ...
  -> the binary patch below is what recovers these.
```

**补丁原理**：`claude.exe` 是 Bun 单文件可执行程序，JS bundle 原样内嵌，可以直接改字节——但**替换后长度必须一模一样**，因为 Bun 记录了每个模块的字节长度，长度一变 bundle 就废了：

```
/(claude|anthropic)/i   ->   /.*||||||||||||||||/i
 ^^^^^^^^^^^^^^^^^^             ^^^^^^^^^^^^^^^^^^
      18 字节                        18 字节
```

`.*` 加一串空分支（`|`）能匹配任意字符串，`.test()` 恒为真，两处过滤同时失效。空分支是合法 JS 正则。

**重要**：VSCode 必须**完全退出**才能装补丁，否则 Windows 锁着 `claude.exe` 的映像文件。脚本检测到进程还在时会拒绝安装，并给出两个选择：

1. 双击它生成的 `swap_claude_binary.bat`；
2. 关掉 VSCode 后重跑 `python private_api.py --target claude --patch-only`。

> 插件每次升级都会把 `claude.exe` 换回原版，**补丁会丢**。升级后重跑一次 `--patch-only` 即可。
> 补丁后图片类模型（`gpt-image-*`）也会出现在列表里，选中会报错——那是网关侧该过滤的事，脚本在选择器里已经把这类模型标了 `<- not a chat model`。

### /model 列表只剩下网关模型

打完补丁后，`/model` 里会出现**两组**东西：Claude Code 自带的 Opus / Sonnet / Sonnet 5 (1M) / Haiku，以及从网关拉来的模型。前面那组是内置目录，私有网关根本不提供，选中必然报错。

脚本会把 `/model` 列表**改成只有网关模型**——用 `modelPicker` 这个官方设置项写进 `~/.claude/settings.json`：

```json
"modelPicker": {
  "replaceBuiltInOptions": true,
  "options": [
    { "model": "deepseek-v4-flash", "label": "deepseek-v4-flash", "description": "From gateway · 1M context" },
    ...
  ]
}
```

`replaceBuiltInOptions: true` 是关键：它让选择器**只显示 Default 行 + 下面这些行**，内置目录和被过滤过的 discovery 列表都不再出现。图片/embedding 端点（`gpt-image-*`）会被排除掉——网关虽然提供，但选中会在第一轮就失败。

> `options` 里是**静态快照**。网关加了新模型，跑一次 `python private_api.py --target claude --refresh-models` 重新拉取即可——它会自动复用你设置里已有的 base 和 key，不用再带参数。
> 想保留内置那几项就加 `--keep-builtin-models`，网关模型会追加在内置目录后面。
> `modelPicker` 只从**用户级**设置生效，项目里的 `.claude/settings.json` 会被忽略——所以脚本只写 `~/.claude/settings.json`。

Default 行是去不掉的（Claude Code 一定会保留它），它解析成 `~/.claude/settings.json` 里的 `model` 字段，也就是你 `/model` 选过的那个，所以它指向的就是网关模型。

### ⚠️ 模型列表缓存（打了补丁却看不到模型，多半是这个）

Claude Code 会把 discovery 的结果缓存在 `~/.claude/cache/gateway-models.json`（按 base URL 区分），
**`/model` 选择器优先读这份缓存，而不是重新拉取**。

关键在于：**写进缓存的是「已经过滤完」的列表**。所以只要在打补丁之前成功拉取过一次，
这份缓存就被污染了——之后你把 `claude.exe` 重新打多少遍补丁都没用，选择器还是显示那份旧的短列表，
而且里面的模型名可能早就从网关上消失了。

典型症状：**选择器里只有一个 `claude-xxx`，而网关上的模型明显不止这些。**

脚本每次打补丁（包括 `--patch-only` 幂等执行）都会顺带检查和清理这份缓存，并在 `--status` 里报告：

```
--- gateway model cache -----------------------------------------------
  C:\Users\mengweiming\.claude\cache\gateway-models.json
      1 cached: claude-deepseek-v4.1-flash-test
      written by: http://10.18.219.156:4000  <- looks like a PRE-PATCH (filtered) result
```

也可以单独清：

```bash
python private_api.py --target claude --clear-model-cache
```

清完**必须重启 VSCode**才会重新拉取。缓存会备份成 `gateway-models.json.bak`。

---

## Codex

### 写入的配置

```toml
model = "glm-5.3-flash"
model_provider = "private"
model_catalog_json = 'C:\Users\you\.codex\gateway-models.json'

[model_providers.private]
name = "Private Gateway"
base_url = "http://10.18.219.156:4000/v1"
wire_api = "responses"
requires_openai_auth = false
env_key = "PRIVATE_API_KEY"
```

- `base_url` **要带 `/v1`**（Codex 自己拼 `/responses`），和 Claude Code 的要求正好相反；脚本在命令行上两种写法都收，会各自转成正确的形状。
- `requires_openai_auth = false`：避免 Codex 拿着 ChatGPT 登录态去要求一个永远不会接受它的 provider。

### 关于 `wire_api`

Codex 0.150 已经**移除**了 `wire_api = "chat"`，二进制里明确写着：

```
`wire_api = "chat"` is no longer supported.
How to fix: set `wire_api = "responses"` in your provider config.
```

也就是说**网关必须支持 `POST /v1/responses`**，LiteLLM 是支持的。脚本在写配置前会用你选的模型实打一次 `/v1/responses` 探针（`max_output_tokens=16`，几乎不花钱），不通过就当场报错，而不是等你在编辑器里踩坑。

### API key 怎么给

两种方式，脚本默认第一种：

| 方式 | 命令 | 优点 | 代价 |
|---|---|---|---|
| 环境变量（默认） | `setx PRIVATE_API_KEY sk-...` | key 不落在配置文件里 | **必须重启 VSCode** 新进程才继承得到 |
| 内联 | `--inline-key` | 立刻生效，不用重启 | key 以明文写在 `config.toml` 里 |

Windows 用 `setx` 写用户级环境变量；macOS/Linux 会往 `~/.zshrc` / `~/.bashrc` 追加一行带注释的 `export`（幂等，重复跑不会重复追加）。

### 模型列表（`model_catalog_json`）

Codex **不会**像 Claude Code 那样去枚举 `/v1/models`。它的模型下拉框读的是一份**目录文件**，默认那份是从 `chatgpt_base_url` 拉来的 OpenAI 自家目录——所以你会看到 GPT-5.6 Sol / Terra / Luna / GPT-5.5 / GPT-5.2，而网关上一个都没有，选了必然第一步就失败。

`model_catalog_json` 指到一个 JSON 文件就**整体替换**那份目录。脚本从网关实时列表生成它：

```json
{
  "models": [
    {
      "slug": "glm-5.3-flash",
      "display_name": "glm-5.3-flash",
      "description": "Private gateway model · 128K context",
      "priority": 2,
      "visibility": "list",
      "supported_reasoning_levels": [],
      "shell_type": "unified_exec",
      "supported_in_api": true,
      "support_verbosity": false,
      "truncation_policy": { "mode": "tokens", "limit": 10000 },
      "experimental_supported_tools": [],
      "base_instructions": "You are Codex, a coding agent. ...",
      "context_window": 128000,
      "max_context_window": 128000,
      "apply_patch_tool_type": "freeform",
      "supports_search_tool": false,
      "input_modalities": ["text"]
    }
  ]
}
```

几个关键点，都是踩出来的：

| 字段 | 为什么要写 |
|---|---|
| `slug` | **就是网关上要发的模型名**，Codex 原样当成 `model` 发出去，LiteLLM 按它路由 |
| `visibility: "list"` | `"list"` 才进下拉框；Codex 自己的隐藏项用的是 `"hide"` |
| `supported_reasoning_levels: []` | 空数组合法，等于不给 Reasoning 子菜单。网关对 `reasoning_effort` 的支持参差不齐，给了一堆会被静默忽略的档位比不给更糟 |
| `apply_patch_tool_type: "freeform"` | **不写这个 Codex 就不给 `apply_patch` 工具**，agent 根本改不了文件 |
| `supports_search_tool: false` | 联网搜索会走 OpenAI 后端，不是网关 |
| `base_instructions` | serde 要求有它或 `model_messages.instructions_template` 二选一，否则整个文件解析失败 |

**必填字段是硬要求**：`model_catalog_json` 指向的文件里每个条目必须带 `slug`、`display_name`、`supported_reasoning_levels`、`shell_type`、`visibility`、`supported_in_api`、`priority`、`support_verbosity`、`truncation_policy`、`experimental_supported_tools`，加上 `base_instructions`；`models` 数组还不能为空。少一个 Codex 会**整个文件拒绝解析并启动失败**，报 `missing field \`x\``。其余字段都有默认值，脚本只写会改变行为的那几个。

验证目录确实生效（而不是被追加）：目录里只列网关模型时，`-m gpt-5.6-sol` 会打印

```
warning: Model metadata for `gpt-5.6-sol` not found. Defaulting to fallback metadata;
```

而选一个目录里有的网关 id 则干干净净——说明这份目录是**替换**而非补充。

生成的文件放在 `$CODEX_HOME/gateway-models.json`（默认 `~/.codex/gateway-models.json`），和 `config.toml` 作伴，`CODEX_HOME` 整个搬走也还指向它。`--restore` 只删自己生成的那个文件；如果 `model_catalog_json` 指向别处，那是你自己配的，脚本不碰。

### 拉取并切换模型

```bash
python private_api.py --target codex --list-models          # 看看网关上有什么
python private_api.py --target codex --switch-model         # 交互式选，写回 config.toml
python private_api.py --target codex --model gpt-5.6-sol    # 直接指定
python private_api.py --target codex --refresh-models       # 只重生成目录，其余不动
```

`--switch-model` 每次重新拉实时列表；`--refresh-models` 只重写模型目录（网关新加了模型就跑这个，`--target codex` 和 `--target claude` 都认这个参数）。选择器接受三种输入：**序号**、**子串**（唯一匹配即选中）、**完整模型 id**。

`--refresh-models` 不带 `--api-key` 时会自己找回 key：`config.toml` 里的 `experimental_bearer_token`（`--inline-key` 写的），或 `env_key` 指的那个环境变量。所以 `--inline-key` 装完机器后，刷新是一条命令的事。

脚本还会在 `vscode/` 目录下生成两个免记参数的小工具：

```
codex-model.sh      # macOS / Linux / Git Bash
codex-model.bat     # Windows 双击
```

### ⚠️ 本机代理会让 Codex 报 503

如果机器上开着 Clash / V2Ray 这类本地代理（`127.0.0.1:7897` 之类），Codex 会**把发往局域网网关的请求也塞进代理**，流式响应被代理掐断，表现为反复重连然后失败：

```
ERROR: Reconnecting... 3/5
sampling_error=unexpected status 503 Service Unavailable: litellm.ServiceUnavailableError:
  auth_unavailable: no auth available (providers=codex, model=gpt-5.6-sol ...)
```

但这个网关本身是好的——同一时刻直接 `curl` 它的 `/v1/responses` 是 200。**解决办法是把网关加进代理白名单**：

```bash
setx NO_PROXY "10.18.219.156,localhost,127.0.0.1"      # Windows
export NO_PROXY="10.18.219.156,localhost,127.0.0.1"    # macOS / Linux
```

（`codex exec` 的 debug 日志里能看到证据：`reqwest::connect: proxy(http://127.0.0.1:7897/) intercepts 'http://10.18.219.156:4000/'`。）

---

## 疑难根因与修复

这一节记录已经**对线上网关实测定位**的问题。前两个的修复是 Codex 能正常工作的前提。

> 问题 2（Codex 发 `v1/response` 走私有模型异常）其实是**两个独立的根因**叠在一起：
> `reasoning_effort` 造成 400，`client_metadata` 造成 500。修掉前一个，后一个才会露出来。
> 两个都在网关侧解决，因为 Codex 二进制改不了参数。

### ⚠️ Codex 请求私有模型全部 400

**现象**：Codex 选私有模型（`deepseek-v4-flash`、`qwen3-5-397b` 等）发消息，界面上只看到
`ERROR: Reconnecting... 1/5`，最终失败。

**根因**：Codex 0.150 起只能发 `POST /v1/responses`（`wire_api = "chat"` 已被移除），
而它**每一轮都带 `reasoning: {"effort": ...}`**。LiteLLM 把这个字段翻译成 OpenAI 风格的
`reasoning_effort`，然后交给模型的 provider——而私有模型全部注册成
`custom_llm_provider = custom_openai`，这个 provider 没声明支持该参数，于是 LiteLLM
**在请求出网关之前**就拒绝了：

```
litellm.UnsupportedParamsError: custom_openai does not support parameters:
['reasoning_effort'], for model=deepseek-v4-flash
```

注意：**`effort: "none"` 也一样 400**，所以改 Codex 的档位设置绕不过去。

**修复**：在模型定义上放开这个参数。LiteLLM 的报错原文就给了出口：

```
If you want to use these params dynamically send
allowed_openai_params=['reasoning_effort'] in your request.
```

Codex 的二进制改不了，所以放到模型定义里：

```toml
litellm_params.allowed_openai_params = ["reasoning_effort"]
```

**怎么改**：

```bash
# 默认只打印命令，不动网关
python private_api.py --emit-gateway-config

# 确认无误后，直接从这里发过去（需要 LiteLLM master key）
python private_api.py --apply-gateway-config
```

> ⚠️ `POST /model/update` 是**整体替换** `litellm_params`，不是合并。所以本工具生成的
> 每一条命令都重发该模型**完整的**参数集（含 `api_base`、`custom_llm_provider`）再追加新键；
> `--apply` 也会在 `api_base` 缺失时直接拒绝发送，避免把模型改坏给全网关的人用。

**验证**（应返回 200）：

```bash
curl -sS -o /dev/null -w "%{http_code}\n" -X POST http://10.18.219.156:4000/v1/responses \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4-flash","input":"hi","stream":true,
       "reasoning":{"effort":"high"},"allowed_openai_params":["reasoning_effort"]}'
```

### ⚠️ 修完 400 之后变成 500（`client_metadata`）

**现象**：`reasoning_effort` 放开之后 Codex 不再 400 了，但依然
`ERROR: Reconnecting... 1/5`。抓 Codex 自己的日志看到它确实连上了网关，收到的是
`status=500 Internal Server Error`，响应体只有 293 字节。

**定位过程**：与其一个字段一个字段猜，不如把 Codex 真正发的东西抓下来。用一个本地
`127.0.0.1:4999` 的小服务接住请求（临时 `-c model_providers.private.base_url=...` 改指向），
拿到 Codex 的**完整原始请求体**，再原样重放到网关，就读到了那个 500 的内容：

```
litellm.InternalServerError: InternalServerError: Custom_openaiException -
AsyncCompletions.create() got an unexpected keyword argument 'client_metadata'
```

**根因**：Codex 每轮都会附一个 `client_metadata` 对象（session/turn id、沙箱模式、安装 id
等遥测信息）。LiteLLM 1.100.1 把它**原样透传**给 OpenAI SDK，而
`AsyncCompletions.create()` 没有这个关键字参数 → `TypeError` → 500。
它与 `reasoning_effort` 无关，所以修完 400 才会露出这个 500。

**修复**：让 LiteLLM 在调 provider 之前把这个字段丢掉：

```toml
litellm_params.additional_drop_params = ["client_metadata"]
```

丢掉的只是 Codex 自己的遥测信封，不是模型输入，没有损失。

**验证**：把抓到的**原始请求体原样**重放（`client_metadata` 保留在 body 里）应返回 200；然后：

```bash
codex exec -s read-only --skip-git-repo-check -m deepseek-v4-flash "say PONG"
# codex -> PONG
```

> 两个修复是一组：`--emit-gateway-config` / `--apply-gateway-config` 会**一起**检查，
> 缺哪个补哪个，已满足的不重发。

### ⚠️ `qwen3-5-397b`：上游只认"系统消息在最前"（已确认暂不修）

**现象**：上面两个网关参数补完之后，5 个私有模型里 4 个正常，只有 `qwen3-5-397b` 仍失败，
但**换了一个错**：

```
litellm.BadRequestError: Custom_openaiException - OpenAIException -
System message must be at the beginning.
```

**根因**：Codex 每轮同时发两样东西——顶层 `instructions`（目录里的 `base_instructions`，
431 字符的人设）和 `input` 数组里**第一条 `role: "developer"` 的消息**（6276 字符的技能清单 +
沙箱/权限说明）。LiteLLM 把两者都变成 system 消息交给上游，而 `qwen3-5-397b` 这一路的
chat template 要求系统消息只能有一条且在开头，于是 400。

可以对照着看：`deepseek-v4-flash`、`glm-5.3-flash` 跟它**用的是同一个 `api_base`**，
却都能跑——所以这不是网关配置问题，是那个模型自己后端的模板更严格。

**已验证可用的修法**（尚未应用）——给这一个模型额外丢掉 `instructions`：

```toml
litellm_params.additional_drop_params = ["client_metadata", "instructions"]
```

实测把 Codex 的**原始请求体**重放到网关返回 200。**代价**：这个模型会失去 Codex 的人设
system prompt（"You are Codex, a coding agent..."），只剩技能清单和工具定义。功能仍在，
但回答风格会变。所以这是一次**取舍**，需要单独确认，没有跟着上面两个修复一起下发。

> **当前决定（2026-09-16）：暂不应用**，先保持 4/5。上面那条 curl 随时可以补上——
> 想启用时，把 `qwen3-5-397b` 的 `additional_drop_params` 从 `["client_metadata"]`
> 改成 `["client_metadata", "instructions"]` 即可。

试过但**无效**的两条路（记录以免重复踩）：

- 目录里加 `include_skills_usage_instructions: false` —— Codex 还是会为沙箱/权限发 developer 消息；
- 指望 LiteLLM 合并 system 消息 —— 它没有这个配置项。

### ⚠️ Codex 打开就要求登录

**现象**：`model_provider` 已经指向私有网关、`requires_openai_auth = false` 也写了，
Codex 仍然弹登录；`codex login status` 说 `Not logged in`。

**根因**：Codex **判断"是否登录"只看 `~/.codex/auth.json` 在不在**，和这一轮要走哪个
provider 无关。文件不存在 → 未登录 → 弹登录，provider 表怎么写都拦不住。

**修复**：写入 API key 形态的 `auth.json`（Codex 本身就有 API key 登录模式，
二进制里有 `... will be stored locally in auth.json. Detected OPENAI_API_KEY ...`）：

```bash
python private_api.py --fix-login     # 撤销：--fix-login --restore
```

写入的是**网关的 key**，不是 OpenAI 的——它就是这个私有 provider table 本来就要用的那把，
和 `config.toml` / `PRIVATE_API_KEY` 里已有的完全相同，没有引入新凭据。工具在写之前会先拿
这把 key 打一次 `GET /v1/models`，**打不通就不写**，免得留下一个"看起来登录了、实则每轮都失败"的状态。

> 代价：`auth.json` 是磁盘上的明文，和它重复的 `env_key` 配置一样。这是换取"不需要 ChatGPT
> 账号"的代价。原本的 `auth.json` 会备份成 `auth.json.bak`。

### 模型能力配置：`model-config.jsonc`

下面两节（思考档位、图片贴不进去）说的是**同样三件事**——每个模型能不能贴图、有几档思考、上下文多长。这三项**不再是 Python 常量**，而是一份归你所有的数据文件：

```
vscode/                            ← 打完 patch 后就是这里
├── private_api.py
├── model-config.jsonc             ← 首次跑 --refresh-models 生成，之后归你改
└── private-api/
    └── model-config.seed.jsonc    ← 随包发的实测知识库（别改）
```

**为什么放在脚本旁边而不是 `~/.codex` 里。** 放 `~/.codex/` 更"规范"，但用户找不到的配置文件等于没人改的配置文件。`--status` 会把它全路径打出来。

#### 一次配置，两步走

```bash
python private_api.py --target codex --refresh-models    # 第 1 步：生成/追加，并【立即应用】
python private_api.py --apply-model-config               # 第 2 步：你改完之后，同步进真实环境
```

1. **首次运行（patch 流程 / `--refresh-models`）**生成 `model-config.jsonc`（= seed 的实测结论 ⊕ 网关当前模型列表），然后**立刻**写进 `model_catalog_json` 指向的那份目录。**打完 patch 就能用**，第 2 步不是前置条件。
2. 之后每次运行只把网关**新出现**的 slug 追加进去；**绝不重写你写下的条目，注释也不动**。
3. 你改完 → `--apply-model-config` 校验、重新解析、重写目录。

字段就这四项：

```jsonc
"glm-5.3-flash": {
  "input_modalities": ["text", "image"],   // 实测：能分辨红/蓝
  "reasoning_levels": ["low", "high", "xhigh"],
  "default_reasoning_level": "high",
  "context_window": 128000
}
```

**建议值**：`input_modalities` 默认只有 `text`；`context_window` 网关报了 `max_input_tokens` 就用网关的、没报就 **128K**；思考档位非 OpenAI 模型默认 **vscode 这一个工具包的三档** `low/high/xhigh`（见下）。实测能看图的模型已经预填成 `["text","image"]`。

#### 解析优先级（逐字段独立，高 → 低）

1. `model-config.jsonc` 里该模型的这个字段 ← **你写的**
2. 旧的 `private-modalities.json` / `private-reasoning.json`（**只读**，老装机沿用旧选择）
3. OpenAI 官方模型（`gpt-*`/`o*`）→ `models_cache.json`，**不覆盖官方声明**
4. 网关 `GET /v1/models` 主动广告的字段
5. `model-config.seed.jsonc` 里该模型的实测值
6. `_defaults` ⊕ `_toolkits.vscode`

**删一个字段就回落一个字段。**

#### 为什么 vscode 这一份停在 `xhigh`

同一个 `_defaults` 发给两个工具包会把已修好的 bug 带回来，所以按工具包覆盖：

```jsonc
"_toolkits": { "vscode": { "reasoning_levels": ["low", "high", "xhigh"] } }
```

理由就是下一节实测出来的那条：**这个插件的 webview 画不出 `max` 那一行**（画不出就整行不显示，看着像功能丢了）。CLI 那边没有这个限制，所以它默认到真正的天花板 `max`——两份默认值**故意不同**，不是漂移。

#### 为什么是平铺，不是嵌套在 `"models"` 下

因为 `private-api/jsonc.py` 的 `set()` **只支持顶层键**，且会重编码整个被替换的值。一旦嵌套，任何一次写入都会**抹掉 `models` 里所有注释**——而"追加新模型"恰恰是最常发生的写入。

#### 写坏了会怎样（fail-open）

文件语法错、值非法 → **目录照样生成，Codex 照样启动**，坏值被丢掉、回落到 seed 与 `_defaults`。但**不会静默**：`--status` 和 `--apply-model-config` 会把问题打出来，语法错还给出精确行列号。

```console
! 3 problem(s) -- the bad values are ignored, the rest still applies
    glm-5.3-flash.input_modalities: 'hologram' 不是 Codex 认的值（只有 text/image/audio）
    glm-5.3-flash.context_window: 应该是整数
```

值非法会让整个目录解析失败 → **Codex 直接起不来**（不是降级），所以枚举校验留在代码里（`KNOWN_MODALITIES` / `KNOWN_EFFORTS`），配置只提供值。

> **网关改了模型 id 就是一次重测，不是 find-and-replace。** 踩过：严格那个后端最早以
> `deepseek-v4.1-flash-test` 测出能看图，网关改成 serve `deepseek-v4.1-flash` 后，表里还是旧
> id → 新 id 落进默认的 `("text",)` → **插件在客户端就拒掉贴图**，提示 "This model does not
> support image inputs."。生效的是**代码**，本地文件里没有可改的东西，修复只能等新版本。
> 这件事就是这次改动的起因。现在改数据文件即可，`--probe-modalities` 给结论。

---

### ⚠️ 每个模型配思考档位

Codex 的 Reasoning 子菜单**完全由我们生成的模型目录决定**，靠两个字段：

```json
"supported_reasoning_levels": [{"effort": "low", "description": "..."}],
"default_reasoning_level": "low"
```

> 这两个字段是**对象数组**，不是字符串数组。给成 `["low","high"]` 会让目录解析失败。
> 权威格式取自 `~/.codex/models_cache.json`（Codex 自己拉的目录）和 `codex.exe` 内的
> schema 串（`... default_reasoning_level supported_reasoning_levels shell_type ...`）。

**OpenAI 官方模型保持原样**：它们的档位直接从 `models_cache.json` 原样抄，
不受本工具配置影响（`gpt-5.6-sol` 依旧是 low/medium/high/xhigh/max/ultra）。

**私有化模型可配**，默认 `low/high/xhigh`。**档位现在写在 `model-config.jsonc` 里**（见上一节）：

```jsonc
"deepseek-v4-flash": {
  "reasoning_levels": ["low", "medium", "high", "max"], "default_reasoning_level": "high"
}
```

```bash
python private_api.py --apply-model-config     # 改完同步进真实环境，重载 VSCode 窗口
```

旧的按模型覆盖仍然可用（写 `$CODEX_HOME/private-reasoning.json`，**只读兼容、优先级低于配置文件**）：

```bash
# 交互式挑模型、填档位
python private_api.py --configure-reasoning

# 或者直接指定
python private_api.py --configure-reasoning \
    --reasoning-model deepseek-v4-flash \
    --reasoning-levels low,medium,high,max \
    --reasoning-default high

# 不给某个模型 Reasoning 子菜单（空档位）
python private_api.py --configure-reasoning --reasoning-model glm-5.3-flash --reasoning-levels ""

# 清掉覆盖，回到默认
python private_api.py --configure-reasoning --reasoning-clear deepseek-v4-flash
```

> 要**不给**某个模型 Reasoning 子菜单，在配置文件里写 `"reasoning_levels": []` —— 空数组是有效值，
> 表示"这个模型没有思考菜单"，对纯聊天模型正合适。

> 档位名必须在 Codex 认识的集合内（`none/minimal/low/medium/high/xhigh/max/ultra/persistent`），
> 否则整个目录会让 Codex **启动即失败**。工具会校验并拒绝非法值。
>
> 另外：`config.toml` 里如果写了顶层 `model_reasoning_effort`，它会**覆盖**目录里的
> `default_reasoning_level`。想用目录默认值就把那一行删掉。

#### 为什么默认到 `xhigh` 就停了，不是 `max`

试出来的，不是设计出来的。**Codex 的下拉菜单画不出 `max`**：

- 目录里给 `low, high, max`，菜单只显示 **Light / High** 两项；
- 同样的菜单去选 `gpt-5.6-sol`（它自己的目录条目列了 6 档，含 `max`）——**也没有 max**。

两件事说明：菜单的上限**跟着 provider 走，不跟着模型走**，私有 provider 就是画不出 `max`。
`xhigh` 是菜单真能画出来的最高一档，OpenAI 自己的 `gpt-5.5` 条目也停在 `xhigh`。

> **`max` 本身没坏**：手写 `model_reasoning_effort = "max"`、或
> `--reasoning-levels low,high,max`，请求照样带 `max` 出去、后端照样收。只是**点不到**。

#### 为什么菜单写的是 "Light" 而不是 "low"

这是 Codex 自己的文案，不是我们写错了。它的 webview 把档位映射成：

| 目录里的值 | 菜单显示 |
|---|---|
| `none` | None |
| `minimal` | Minimal |
| `low` | **Light**（label id `composer.mode.local.reasoning.low.label.v2`） |
| `medium` | Medium |
| `high` | High |
| `xhigh` | Extra High |
| `max` | Max |

想确认 Codex 到底解析到了什么，用插件自带的二进制问它，比看菜单准：

```bash
codex debug models        # 打印 app-server 实际解析的模型目录 JSON
```

---

### ⚠️ 图片贴不进去：`input_modalities`

**现象**：往 Codex 输入框里粘图，立刻弹红条
`This model does not support image inputs. Try a different model`。

**根因**：**这个提示根本走不到网关**，是 Codex 客户端拿目录（`model_catalog_json`）里的
`input_modalities` 字段自己拦的，两处都拦：

- **webview**（`webview/assets/app-initial-*.js`）——贴图那一刻就弹 toast：
  `e.inputModalities.includes("image") === false`
- **app-server**（`codex.exe`）——提交前还有一道：
  `Model <id> does not support image inputs. Remove images or switch models.`

app-server 把目录里的 `input_modalities` 转成 camelCase 的 `inputModalities` 交给
webview。本工具原先**写死了 `["text"]`**，所以每个模型都贴不了图。

> ⚠️ 这也连带弄坏了 OpenAI 官方模型：我们的目录是**整体替换**内置列表的，
> 而 `input_modalities` 从来没从 `models_cache.json` 抄进去过——所以
> `gpt-5.6-sol` 在我们的目录下同样贴不了图，"保持原样"在这一项上是假的。
> 现已修好：OpenAI 模型的 `input_modalities` 和思考档位一样从缓存原样抄。

**取值**：`["text", "image"]`（另有 `audio`，本部署没有模型用）。
取自 Codex 自己写的 `~/.codex/models_cache.json`，并用 `codex debug models` 复验过。

**关键：能不能看图要实测，不能靠"它收下了"判断。**

网关不给任何提示——`GET /v1/models` 只有 `mode`（`chat` / `image_generation`），
没有任何 modality 字段。而**返回 200 不等于真看见了**：本部署有个模型对红色图和蓝色图
分别回 200 和"Black"/"Orange"，纯属编造。所以判断标准是**红蓝能否区分**。

```bash
# 发一张纯红、一张纯蓝，看模型能不能分辨
python private_api.py --probe-modalities
python private_api.py --probe-modalities --probe-models glm-5.3-flash   # 只测一个
```

2026-09-16 实测结果（64×64 纯色图，问主色）：

> 表按网关**实际 serve** 的 slug 记。严格那个后端最早以 `deepseek-v4.1-flash-test` 测得能看图，
> 网关后来改 serve `deepseek-v4.1-flash`，旧 id 留在表里 → 新 id 落进默认值 `("text",)` → 贴图被拒。
> 2026-09-17 用新 slug 重测，仍是 Red / Blue。

| 模型 | 结果 | 结论 |
|---|---|---|
| `deepseek-v4.1-flash` | Red / Blue ✓ | 能看图 |
| `glm-5.3-flash` | Red / Blue ✓ | 能看图 |
| `qwen3-5-397b` | Red / Blue ✓ | 能看图 |
| `xinghai-ultra` | 拒答，或答 Black/Orange | ❌ **收得下但看不见**，两次行为还不一致 |
| `deepseek-v4-flash` | HTTP 400 | ❌ 上游直接拒：`Model only supports text input` |

这个结果**已经写进随包发的 `private-api/model-config.seed.jsonc`**，并预填进了
`model-config.jsonc`，所以按默认值重生成目录就直接对了——不用手工配。

**手工改**（换个模型、或后端升级后）——直接改 `model-config.jsonc`：

```jsonc
"glm-5.3-flash": { "input_modalities": ["text", "image"] }
```

```bash
python private_api.py --apply-model-config     # 或 --target codex --refresh-models
```

`--probe-modalities` **不改文件**，它把**可以直接粘贴的片段**打出来给你抄（写回要重编码嵌套
条目、会抹注释，收益不抵风险）：

```console
To enable, paste these entries into ...\vscode\model-config.jsonc
(or edit the ones already there) and run: python private_api.py --apply-model-config

    "glm-5.3-flash": { "input_modalities": ["text", "image"] },
```

旧的按模型覆盖仍可用（写 `~/.codex/private-modalities.json`，**只读兼容、优先级低于配置文件**）：

```bash
python private_api.py --configure-modalities \
    --modalities-model <模型id> --modalities text,image
python private_api.py --configure-modalities --modalities-model <模型id> --modalities text
python private_api.py --configure-modalities --modalities-clear <模型id|all>
```

> **别"全开"**。把 `deepseek-v4-flash` 打开 → 每轮 400；把 `xinghai-ultra` 打开 →
> 每轮拿到编造的答案且**没有任何报错**。后一种比现在贴不了图更糟。
>
> **OpenAI 官方模型不可配**：它们的 `input_modalities` 和思考档位一样从 `models_cache.json`
> 原样抄，`model-config.jsonc` 里**刻意不放** `gpt-*`/`o*` 条目——目录是整体替换的，我们写什么
> 就盖掉 OpenAI 自己的定义。旧的 `~/.codex/private-modalities.json` 只对私有模型有效。

## 搜索（web search）

### 两个插件的自带搜索都用不了，原因相同

Claude Code 的 `WebSearch` 和 Codex 插件的 web_search **都是服务端（托管）工具**——它们请求的是
厂商的服务器，不是这个网关。网关又只代理 `/v1/*`，所以两个都没法工作。**服务端没有开关可配。**

出路是把搜索放到**客户端侧**做成一个模型能主动调用的工具，也就是 MCP。两个插件都是 MCP 客户端，
连同一个地址即可，与它们背后是什么模型无关。

### ★ 你不用为搜索做任何额外的事

搜索配置是主流程的**最后一步**，自动完成，而且**复用的就是刚写进去的那个 LiteLLM key**。
所以整件事仍然只有一条命令：

```bash
python private_api.py --target both --api-base http://<网关>:4000 --api-key sk-你自己的
```

**没有第二个 key。** 搜索端点挂在网关后面（`<网关>:4000/searxng/mcp`），用的就是你自己的
LiteLLM virtual key——和模型调用是同一把。服务端有一个 `MCP_SEARXNG_TOKEN`，但那是
**服务端内部凭据**，只用于「网关 → searxng-mcp」那一跳，用户永远看不到、也不需要知道。

搜索放在最后一步是因为它要复用前面刚写的 key；拿不到 key 时它只打印一行提示、
**不影响退出码**（配模型才是主任务，不该被附加项拖失败）。`--no-search` 可以跳过。

### 两个插件写的是**两个不同**的文件，格式还**不能互换**

```toml
# ~/.codex/config.toml
[mcp_servers.searxng]
url = "http://<网关>:4000/searxng/mcp"
startup_timeout_sec = 20
tool_timeout_sec = 120

[mcp_servers.searxng.http_headers]
Authorization = "Bearer sk-<你自己的 LiteLLM key>"
```

```jsonc
// ~/.claude.json
{
  "mcpServers": {
    "searxng": {
      "type": "http",
      "url": "http://<网关>:4000/searxng/mcp",
      "headers": { "Authorization": "Bearer sk-<你自己的 LiteLLM key>" }
    }
  }
}
```

| | Codex | Claude Code |
|---|---|---|
| 文件 | `~/.codex/config.toml` | **`~/.claude.json`** |
| `type` 字段 | **不能有**（从 `url` 推断传输） | **必需**，缺了会被当成 stdio 跳过 |
| 认证 | `http_headers` | `headers` |
| 写错的表现 | 报 `is not supported for streamable_http` | **静默跳过**，`/mcp` 里什么都没有 |

**Claude Code 的文件是 `~/.claude.json`，不是 `~/.claude/settings.json`。** 这一点是照着
装机版扩展逐一核对出来的，不是猜的：扩展用一个 helper 同时构造
`globalConfig = <home>/.claude.json` 和 `userSettings = <home>/.claude/settings.json`，而
`mcpServers` **不在** `claude-code-settings.schema.json` 里——写进 settings.json 会被**静默忽略**。

Codex 侧的 token 为什么走 `http_headers` 而不是 `bearer_token_env_var`：后者只在那个变量存在于
**Codex 继承到的环境**里才有效，而 VSCode 插件由编辑器启动，环境我们控制不了。变量缺失不会有任何
提示，请求直接裸奔成 401。字面 header 在所有启动方式下行为一致。

**这一点现在几乎没有代价了**：写进去的值就是你自己的 LiteLLM key，而它本来就在同一个文件里
（模型配置用的就是它）。没有引入新秘密，也没有第二个文件要同步。

### 工具名会带 `searxng-` 前缀，这是正常的

网关暴露的是 `<服务名>-<工具名>`，所以模型看到的是 `searxng-web_url_read`、
`searxng-searxng_web_search`（后者双重，因为服务名和工具名本身都叫 searxng）。

**这个前缀改不掉。** 实测把 `tool_name_to_display_name` 设成完整的反向映射后，`tools/list`
返回的**仍然是**前缀名——那个字段只影响显示层。所以别去调它，看到前缀就当没看见。

### 命令

```bash
# 平时不需要单独跑：主流程的 --target both 已经包含搜索
python private_api.py --target both --api-base http://<网关>:4000 --api-key sk-xxx

# 以下是单独重配 / 排查用的
python private_api.py --configure-search --target both   # 只重配搜索
python private_api.py --check-search                     # 握手 + 列工具
python private_api.py --search-clear                     # 撤销
python private_api.py --target both --no-search           # 配模型但跳过搜索
```

不带 `--target` 时搜索默认写**两个**——一个 MCP 端点服务两个插件，没有理由只配一半。

token 的取值顺序是 `--search-token` → **当前配置里的 LiteLLM key** → `$MCP_SEARXNG_TOKEN`。
环境变量**故意排在最后**：一个残留在 shell 里的旧 `MCP_SEARXNG_TOKEN` 不应该悄悄遮蔽掉正常路径。
`--search-token` 保留是为了「后端直连、不走网关」这种排错场景。

`--check-search` 除了握手，还会报两种**静默漂移**：

- 两个插件的配置指向了不同的服务器——症状是「一个编辑器能用，另一个 401」，而两边配置看起来一样；
- 配置里的 URL 和「当前网关推导出的端点」已经不一致——旧配置在网关换地址后不会自己更新，
  症状是连接错误而不是配置错误。

配完**重启 VSCode**（插件在启动时读配置）。Claude Code 里 `/mcp` 应该能看到 `searxng`。

> **搜索服务端本身的部署与分层验证见 [`../searxng/README.md`](../searxng/README.md)。**
> 客户端配好了不代表服务端是好的——那边第 4 层（用**普通用户 key**走网关真握手）跑通之前，
> 不要认为这套东西是好的。另外：**VSCode 里 Claude Code 的 MCP 客户端尚未被实测过**
> （这台机器上没有 `claude` CLI），请自己点一次搜索确认；Codex 那半边是真跑过的。

---

## 网关（LiteLLM）侧改动：脚本与用法

**有脚本，不用手写 curl，也不用登到网关机器上。** 就是
[private-api/litellm_admin.py](private-api/litellm_admin.py)，通过下面两个开关驱动。

### 它改的是什么

| `litellm_params` 键 | 加进去的值 | 不改会怎样 |
|---|---|---|
| `allowed_openai_params` | `[..., "reasoning_effort"]` | Codex 每轮 **400** |
| `additional_drop_params` | `[..., "client_metadata"]` | Codex 每轮 **500** |

**两个一起检查，缺哪个补哪个**；已经满足的模型不重发。根因分别见上面的
[Codex 请求私有模型全部 400](#️-codex-请求私有模型全部-400) 和
[修完 400 之后变成 500](#️-修完-400-之后变成-500client_metadata)。

### 用法

```bash
# 1) 先看 —— 只打印 curl，一个字节都不改网关
python private_api.py --emit-gateway-config

# 2) 确认无误再改 —— 需要 master key
python private_api.py --apply-gateway-config
```

`--emit-gateway-config` 的输出长这样，每行都能直接粘到"能连网关的机器"上执行：

```bash
# deepseek-v4-flash  (allowed_openai_params += "reasoning_effort"; additional_drop_params += "client_metadata")
curl -sS -X POST http://10.18.219.156:4000/model/update \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model_info":{"id":"<uuid>"},"litellm_params":{...(完整参数)...,
       "allowed_openai_params":["reasoning_effort"],
       "additional_drop_params":["client_metadata"]}}'
```

`--apply-gateway-config` 会逐模型打印 `ok <模型名>`，失败的打 `! <模型名>: <原因>` 并返回退出码 1。

### 鉴权从哪来

`/model/info` 和 `/model/update` 都是 **master key** 端点，**不是**虚拟 key。取值顺序：

1. `--api-key` 显式传入；
2. 上次跑 `--target codex` / `--target claude` 时工具写进 `config.toml` / settings.json 的那把；
3. 环境变量。

> **生成的命令里引用的是 `$LITELLM_MASTER_KEY` 变量，不是明文 key。** 这些命令会被贴进工单
> 和群里，而 master key 能开整个网关。执行前自己先 `export LITELLM_MASTER_KEY=...`。

### 三条必须知道的

1. **`POST /model/update` 是整体替换 `litellm_params`，不是合并。** 所以脚本每条命令都重发该模型
   **完整的**参数集（含 `api_base`、`custom_llm_provider`）再追加新键；`--apply` 在 `api_base`
   缺失时**直接拒绝发送**，避免把模型改坏给全网关的人用。
2. **这是同事共用的生产网关，改完立即对所有人生效。** 所以默认行为是"只生成命令"，
   `--apply-gateway-config` 必须显式指定。
3. **只碰 `custom_llm_provider = custom_openai` 的模型。** 其他 provider 要么自己声明了
   `reasoning_effort`，要么根本不是 chat 模型，报告里会写 `provider <x> is out of scope`。

### 复验

```bash
python private_api.py --emit-gateway-config     # 应报 "nothing to do"
```

端到端——这一步才算真的通了：

```bash
codex exec -s read-only --skip-git-repo-check -m deepseek-v4-flash "say PONG"
# codex -> PONG
```

### 回滚

脚本不记录旧值，但它只**追加两个键**，原有键一个都没动。要撤就在 `/model/update` 的 body 里
把这两个键从数组里去掉（数组空了就把该键删掉）重发一次。旧值可从 `POST` 之前的
`GET /model/info` 输出，或网关自己的 `config.yaml` 里找回。

### 怎么搬到同事机器上

`litellm_admin.py` 只用标准库 + 同目录的 `gateway.py`，**不写任何本地文件、不读 `CODEX_HOME`**。
所以同事那台机器上可以直接：

```bash
python private_api.py --emit-gateway-config     # 只要网络能到网关 + master key
```

把输出贴到有权限改网关的地方执行即可；本机不需要装任何东西。这和 Claude Code / Codex 的配置
是**完全独立**的两件事——不改网关，插件配置写得再对也照样 400/500。

---

## 命令速查

```bash
# 交互式向导（推荐第一次用）
python private_api.py

# 一次配好两个插件
python private_api.py --target both --api-base http://10.18.219.156:4000 --api-key sk-XXX

# 只配其中一个
python private_api.py --target claude --api-base ... --api-key ...
python private_api.py --target codex  --api-base ... --api-key ...

# 只看不动
python private_api.py --detect
python private_api.py --status

# Claude Code
python private_api.py --target claude --no-patch          # 只写设置，不动 claude.exe
python private_api.py --target claude --patch-only        # 只重打补丁（升级插件后用）
python private_api.py --target claude --clear-model-cache # 清掉 gateway-models.json，强制重新拉取
python private_api.py --target claude --refresh-models    # 只重拉模型列表，重写 /model 列表
python private_api.py --target claude --keep-builtin-models  # /model 里保留内置的 Opus/Sonnet/Haiku
python private_api.py --target claude --claude-binary <path>

# Codex
python private_api.py --target codex --list-models
python private_api.py --target codex --switch-model
python private_api.py --target codex --refresh-models       # 只重生成模型目录
python private_api.py --target codex --inline-key          # key 写进 config.toml
python private_api.py --target codex --profile work        # 写 [profiles.work]，不动根配置
python private_api.py --target codex --skip-probe          # 跳过 /v1/responses 探针
python private_api.py --target codex --strip-openai-keys   # 删掉 service_tier 这类 OpenAI 专属键

# 搜索（让模型能搜网页）。平时不用单独跑：--target both 已包含，用的是同一个 LiteLLM key
python private_api.py --configure-search                 # 只重配搜索，两个插件都写
python private_api.py --configure-search --target claude # 只写 Claude Code
python private_api.py --check-search                     # 握手 + 列工具 + 检查两种漂移
python private_api.py --search-clear
python private_api.py --target both --no-search          # 配模型但跳过搜索

# Codex 登录（不写 auth.json 就会一直弹登录）
python private_api.py --fix-login                          # 写入网关 key 作为 API key 登录
python private_api.py --fix-login --force                  # 已登录也覆盖（轮换 key 后用）
python private_api.py --fix-login --restore                # 还原 auth.json

# 模型能力（能不能贴图 / 几档思考 / 上下文多长）—— 直接改 vscode/model-config.jsonc
# 大部分情况不用动：实测结论已预填，网关新模型会自动追加进来
$EDITOR model-config.jsonc
python private_api.py --apply-model-config                  # 改完同步进真实环境

# 旧的按模型覆盖（仍读取，但 model-config.jsonc 优先级更高）
python private_api.py --configure-reasoning                # 交互式
python private_api.py --configure-reasoning --reasoning-model <id> \
    --reasoning-levels low,high,xhigh --reasoning-default high
python private_api.py --configure-reasoning --reasoning-clear <id|all>

# 能否贴图：测量不改文件，给结论 + 可粘贴片段
python private_api.py --probe-modalities                    # 实测哪些模型真能看图
python private_api.py --configure-modalities --modalities-model <id> \
    --modalities text,image
python private_api.py --configure-modalities --modalities-clear <id|all>

# 网关侧参数（Codex 400 + 500 的两个根因，见「网关（LiteLLM）侧改动」）
python private_api.py --emit-gateway-config                # 只打印 curl，不动网关
python private_api.py --apply-gateway-config               # 直接改网关（需 master key）

# 还原
python private_api.py --restore --target both
```

`--yes` / `-y` 关掉所有交互，缺参数直接报错退出——适合写进批量脚本。

---

## 常见问题

**设了没用？**
大概率是工作区里有 `.vscode/settings.json` 也定义了 `claudeCode.environmentVariables`。工作区配置会**整体替换**用户级数组而不是合并。脚本检测到这种情况会打警告，照着改或对那个文件重跑一次。

**Codex 报找不到 key？**
用了默认的环境变量方案时，**必须重启 VSCode**——`setx` 只对之后启动的进程生效。不想重启就加 `--inline-key`。

**改了 `config.toml` 里的 `service_tier`？**
脚本只警告不擅自删。`service_tier = "priority"` 是 OpenAI 专属概念，私有网关可能直接报错，用 `--strip-openai-keys` 删掉。

**Claude Code 升级后模型列表又只剩 claude 开头的了？**
补丁被新版 `claude.exe` 覆盖了。关掉 VSCode，跑 `--target claude --patch-only`。

**`/model` 里还有 Opus / Sonnet / Haiku，也不是这个网关的模型？**
那几项是 Claude Code 的内置目录，补丁管不着。跑 `--target claude --refresh-models` 写 `modelPicker` 把内置目录换成网关模型；想两者都留就加 `--keep-builtin-models`。

**补丁打了，`/model` 里还是只有 claude 开头的模型？**
多半是 `~/.claude/cache/gateway-models.json` 那份**补丁前写的**缓存在作祟（选择器优先读它），见上面 [模型列表缓存](#️-模型列表缓存打了补丁却看不到模型多半是这个)。`--patch-only` 会自动清掉它，清完要重启 VSCode。

**替换 `claudeCode.environmentVariables` 时注释丢了吗？**
那个数组**内部**的注释会随旧值一起被替换掉（文本手术只能保住数组外的内容）。改动前的完整内容在 `settings.json.bak` 里。

**模型不会搜索 / 说它不能联网？**
两个插件自带的搜索**都是服务端工具**，网关又只代理 `/v1/*`，所以在这个部署里**不可能**工作（见[搜索](#搜索web-search)）。正常跑一次主流程命令就会接上 MCP。已经配过还是不行就用 `--check-search` 定位。

**配了搜索，Claude Code 的 `/mcp` 里什么都没有？**
多半写错文件了。Claude Code 的 MCP 配置在 **`~/.claude.json`**（user scope），**不是** `~/.claude/settings.json`——后者的 schema 里根本没有 `mcpServers`，写进去会被静默忽略。另一个常见原因是漏了 `type: "http"`：只有 `url` 没有 `type` 会被当成 stdio 跳过。

**`--check-search` 报 404 `MCP server ... not found`？**
网关起来了但不知道这个 MCP server。在**网关那台机器**上跑 `python ../searxng/register_mcp.py`。这是服务器一次性动作，客户端无能为力。

**`--check-search` 报 401？**
客户端用的是**你自己的 LiteLLM key**，所以基本只有一个原因：搜索那步跑在模型配置之前，或者用了和模型配置不同的 `--api-key`。重跑主流程命令即可。**不存在第二个搜索 key**——服务端的 `MCP_SEARXNG_TOKEN` 客户端根本不碰。

**服务器上 `register_mcp.py --check` 报 `unhealthy`，但 searxng-mcp 的 `/health` 是绿的？**
`MCP_HTTP_ALLOWED_HOSTS` 里没有 `searxng-mcp:8090`。它默认只允许回环地址，且拿它跟请求的 `Host` 头**连端口精确比对**——网关以容器身份来连，`Host` 就是服务名那个值。用**服务名**而不是 IP，换机器就不用改。详见 [`../searxng/README.md`](../searxng/README.md)。

**`--check-search` 提示「配置的 URL 与网关端点不一致」？**
配置是旧地址（网关换过地址或端口）。旧配置不会自己更新，症状是连接错误而不是配置错误。重跑主流程命令重写即可。

**一个编辑器能搜索，另一个不行？**
两边的配置文件看起来一样但 `url` 不同（典型是一条被单独重配过）。`--check-search` 会直接报出来，重跑主流程命令或 `--configure-search` 对齐即可。

**key 泄露风险？**
`--status` 输出里的 key 是打码的（`sk-d4s...5LJQ`）。但 `settings.json`、`~/.claude/settings.json`、`config.toml` 本身都是明文存 key 的——和官方客户端的做法一致，注意别把这些文件连同 `.bak` 一起提交到仓库。

---

## 目录结构

```
vscode/
├── private_api.py           # 入口脚本，唯一需要直接执行的文件
├── model-config.jsonc       # 模型能力配置（跑过一次 --refresh-models 后生成，之后归你改）
├── README.md                # 本文档
├── codex-model.sh/.bat      # 跑过一次 --target codex 后自动生成
└── private-api/             # 支持模块，无需直接调用
    ├── detect.py            # 自动检索各类配置/二进制位置
    ├── jsonc.py             # 保留注释与缩进的 JSONC 读写
    ├── modelconfig.py       # model-config.jsonc 的 schema / 读写 / seed 合并 / 校验
    ├── model-config.seed.jsonc  # 随包发的实测知识库（数据，不是模块）
    ├── tomlpatch.py         # 只改目标行的 TOML 读写
    ├── gateway.py           # /v1/models 拉取、base URL 归一化、端点探针
    ├── claude.py            # Claude Code 环境变量写入与合并
    ├── claude_patch.py      # claude.exe 模型名过滤补丁
    ├── codex.py             # Codex config.toml 写入、模型切换、模型目录生成
    ├── codex_auth.py        # auth.json 写入，解决 Codex 登录门
    ├── reasoning.py         # 每个模型的思考档位（值在 model-config.jsonc；这里只留枚举与解析）
    ├── modalities.py        # 每个模型能不能贴图：红蓝判别探针 + 解析（值同上）
    └── litellm_admin.py     # 网关模型参数：生成 /model/update 命令或直接应用
```

`jsonc.py` / `modelconfig.py` / `model-config.seed.jsonc` / `search.py` 与 `codexcli/` 里对应文件是**逐字节相同**的拷贝，改一份就要同步另一份。
`modalities.py` 和 `reasoning.py` 只差**开头几行**（声明 `TOOLKIT = modelconfig.TOOLKIT_VSCODE`，以及 `reasoning.py` 里那个 vscode 专用的 `DEFAULT_EFFORTS`），其余必须保持一致 —— 用 `diff` 应该只看到那几行。
