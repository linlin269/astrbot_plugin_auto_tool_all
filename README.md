# astrbot_plugin_auto_tool_all

让 AstrBot 通过自然语言编排全局 LLM 工具；把工具返回的图片/视频以 base64 消息转发；对 OpenAI 兼容接口做"查看模型 + 逐模型测速"；在 aiocqhttp / OneBot v11 上把 QQ 头像作为生图参考图；同时提供管理员专属的会话上下文清理指令。

## 能力

- `list_available_tools`：列出当前 active 的外部 LLM 工具。
- `call_plugin_tool`：通过工具名和 JSON 参数调用其它 `llm_tool`、`FunctionTool` 或 MCP 工具；返回中的图片/视频自动下载后以 base64 消息转发。
- `probe`（事件监听）：对 OpenAI 兼容接口"看看里面有什么模型 / 帮我测试一下里面的模型"，逐模型测出首字时间与总回复时间并汇总。
- `avatar_draw`：获取机器人、发送者或被 @ 用户的 QQ 头像，合并当前消息图、回复引用图和外部图片 URL，调用生图工具。
- 工具列表在每次调用时动态读取，因此未来新安装并注册为 LLM 工具的插件无需修改本插件即可被发现。
- 上下文清理指令（见下节）：立即 / 定时 / 一键清空所有会话的 LLM 对话数据。

## 媒体 base64 转发

搜索等工具返回的图片/视频链接，QQ 客户端直接拉 URL 经常失败（防盗链、内网、被墙图床）。本插件把这类媒体**下载后转 base64** 再发：

- 图片上限 10MB（`media_image_max_mb`）、视频上限 100MB（`media_video_max_mb`）；超限、下载失败或发送失败自动**回退为发 URL**。
- 单次最多转发 5 个（`media_max_count`）；总开关 `deliver_media_base64`。
- 临时文件**发送后立即删除**，服务器不留媒体文件（启动时还会清扫异常残留）。
- 只转发 http/https 的公开地址（内网/回环地址按 SSRF 防护拒绝）。

## 模型查看与测速（OpenAI 兼容接口）

在消息里带上 **url 和 key**，再说触发语即可，四种给法都支持：

| 给法 | 示例 |
|---|---|
| 写在同一句 | `https://api.xx.com/v1 key是sk-xxx 帮我看看里面有什么模型` |
| 回复引用 | 回复那条含 url 和 key 的消息，说"帮我测试一下里面的模型" |
| 上一条同发 | 先发一条含 url+key 的消息，下一句说"帮我测试一下里面的模型" |
| 分条发送 | 一条只发 url，一条只发 key，再说"看看里面有什么模型"（顺序不限） |

- **查看模型**：调 `{url}/models` 返回模型列表。
- **测试模型**：对每个模型用流式 `hi` 测一次，记录**首字时间**与**回复总时间**；3 并发、单模型 10 秒超时；只有收到真实回复的才算可用。≥6 个模型时先回"开始测试，共 N 个模型"，测完主动推送汇总：
  ```text
  可用模型有：
  ✓ gpt-4o-mini - 首字 0.8s - 总回复 2.3s
  ✓ deepseek-v3 - 首字 1.2s - 总回复 4.5s

  不可用 1 个：
  ✗ some-model - 不可用（超时（10s））
  ```
  结果按总回复时间从快到慢排序；端点不支持流式时自动改非流式重试并注明（首字=总时间）。
- **URL 前缀兼容**：url 以 `/v1` 结尾直接用；不以版本段结尾默认补 `/v1`；说"**用 v3/api 查看里面的模型**"可显式指定前缀（`{url}/v3/api/models`）。
- **key 安全**：只存内存、不落盘、不写日志；分条发送的 url/key 会话内记忆 30 分钟（`probe_memory_ttl_minutes`），一次成功使用后 key 立即丢弃。
- 提醒：测试会真实消耗该 key 的 token（每模型一次对话，`max_tokens=32`）。


## 清空上下文（仅管理员，普通用户完全静默）

上下文太长导致回复变慢时，可以随时清空 LLM 对话数据（重置为全新对话，不影响头像缓存等其它数据）：

| 指令 | 作用 |
|---|---|
| `/清空上文`（别名 `/清空对话`） | 立即清空**当前会话**的上下文 |
| `/清空上文 all` | 立即清空**所有会话**的上下文 |
| `/清空上文 6s`（或 `6秒`、`5min`、`5分钟`、`1h`、`1小时`） | 定时清理：延时到点后清空当前会话一次；到点前重复设置会**覆盖**旧定时 |
| `/查看清空上文定时任务` | 列出所有定时任务（按触发时间排序，含剩余时间） |
| `/取消清空上文定时任务 序号\|qq号\|群号\|all` | 按列表序号、qq 号/群号或 `all` 取消定时任务 |

行为说明：

- 到点执行时会向该会话主动推送“上下文已定时清空。”；平台不支持主动消息时静默跳过。
- 同一会话同一时间只有一个定时任务，重复设置即覆盖（回复中会提示“已覆盖原定时”）。
- 定时任务**不持久化**：AstrBot 重启后全部消失。
- 每次 AstrBot 启动完成后，插件会读取 WebUI 配置的管理员列表（`admins_id`），向每位管理员的 QQ 私聊发送“报告主人，我刚刚重启啦，清理上下文定时任务全部清空。”；未配置管理员或发送失败则只记日志。热重载插件不会触发该通知。

## 安装

### 方式一：AstrBot WebUI 从 GitHub 仓库安装（推荐）

1. 把本仓库推送到 GitHub，**仓库名必须为 `astrbot_plugin_auto_tool_all`**（AstrBot 用仓库 URL 里的仓库名作为插件目录名，仅把 `-` 转为 `_` 并小写）。
2. 发布前把 `metadata.yaml` 中的 `repo` 字段改成你的实际仓库地址——WebUI 的"检查插件更新"依赖该字段。
3. 在 AstrBot WebUI「插件管理 → 从仓库安装」填入仓库地址，例如：
   `https://github.com/<你的用户名>/astrbot_plugin_auto_tool_all`
   支持 GitHub 仓库主页链接、`.git` 结尾链接、SSH 形式；默认分支由 GitHub 自动解析。
4. 安装器会校验根目录 `metadata.yaml`（必需字段：`name`、`desc`、`version`、`author`）、克隆源码（不保留 `.git`）、加载时自动按 `requirements.txt` 恢复依赖。本仓库满足全部要求。
5. 更新插件时递增 `metadata.yaml` 的 `version` 并推送，WebUI 即可检测到新版本；建议同时打 `v0.1.0` 形式的 tag。

### 方式二：手动复制

将整个目录复制到实际 AstrBot 运行根目录：

```text
<AstrBotRoot>/data/plugins/astrbot_plugin_auto_tool_all/
```

当前工作区不包含 AstrBot 本体，`C:\astrbot` 不是自动推断出的运行根目录。请以 AstrBot 启动目录或 `ASTRBOT_ROOT` 为准。

安装依赖并在 WebUI 重载插件：

```text
pip install -r requirements.txt
```

头像功能要求真实的 aiocqhttp / OneBot v11 事件。WebChat 可以用来检查工具是否注册，但没有 QQ `self_id` 时不能取得真实 QQ 头像。

## 与 selfie_image 配合

默认生图工具名为 `generate_image`，兼容 `astrbot_plugin_selfie_image` 1.4.x 的 LLM 工具。

请在 selfie_image 的 Web 管理面板中开启 `image_enable_llm_tool`。本插件不调用 selfie_image 的 `/画` 或 `/看看你` 指令，而是调用其 `generate_image` 工具，因此不会与现有 command 冲突。

selfie_image 会从当前事件读取消息图片、引用图片、@头像和本地参考图。本插件把 QQ 头像下载到插件数据目录后，以本地 `Image` 组件临时注入事件，避免被 selfie_image 的“过滤机器人 qlogo URL”逻辑排除。

### 身份与“画谁都是机器人”的防护

selfie_image 的 `generate_image` 内部会把带“自拍/合影/同框/和我/形象照”等关键词的请求改道到它的 AI 自拍流程，并以机器人形象图作为身份锚点——这会让“看看我”也画出机器人。本插件做了三层防护：

- **身份识别报错不兜底**：`identity` 传了无法识别的值时直接返回错误说明（列出合法取值），让模型带正确参数重试，而不是静默画成机器人；“我/我自己/myself”等口语别名都能正确解析为发送者。
- **自拍工具自动跳过**：画“我/TA”等非机器人身份时，`generate_selfie` 这类只能画机器人自己的工具会从候选列表中剔除；若首选工具不可用，会明确告知原因而不是悄悄回退到自拍工具。
- **触发词改写 + 身份声明**：为非机器人身份合成提示词时，把 selfie 触发词改写为等价说法（如“自拍”→“个人生活照”），并显式声明“主角不是机器人本人，忽略内置 AI 自身形象参考图”。

排查方法：开启 `debug_logging` 后，日志会输出每次实际使用的 identity、解析到的 QQ 号与注入的参考图路径；机器人回复中的“已使用 XX 的 QQ 头像调用 …”也会如实反映当次解析结果。

## 自然语言示例

在开启 AstrBot 对话/Agent 且 @机器人或使用唤醒词后，可以说：

- `看看你`
- `画一张你的头像变成赛博朋克角色`
- `看看我`
- `@某人 看看他`
- 直接发送一张图并说：`把图里的人物换成你`
- 回复一条带图消息并说：`把图里的人物换成你`
- `先搜一张雨夜街道的图，再把图里的人换成你`

对于最后一种链路，LLM 可以先调用 anysearch，再把搜索结果中的图片 URL 放入 `avatar_draw.reference_image_urls`。

## 其它插件和未来插件

本插件不会复制 anysearch 或 BitTorrent 的业务逻辑，也不会接管它们的 command：

- anysearch 的 `anysearch_batch_search` 等工具可以通过 `call_plugin_tool` 调用。
- BitTorrent 的 `search_magnet`、`preview_magnet` 可以通过 `call_plugin_tool` 调用。
- 以后新增的插件只要使用 `@filter.llm_tool`、`context.add_llm_tools()` 或 AstrBot 支持的 MCP 工具注册能力，就会进入全局工具列表。

只有 `/指令` 而没有 LLM 工具注册的插件，不属于第一版通用桥接范围。

## 配置

主要配置项：

- `image_tool_name`：默认 `generate_image`。
- `image_tool_candidates`：首选工具不可用时的回退列表（画非机器人身份时其中的 `generate_selfie` 会被自动跳过）。
- `debug_logging`：输出身份解析、头像下载与工具选择细节，排查“画错人”问题时开启。
- `allowed_tool_names` / `blocked_tool_names`：限制万能工具入口可调用的工具。
- `avatar_spec`、`avatar_cache_ttl_minutes`：QQ头像规格和缓存时间。
- `max_reference_images`、`external_image_max_mb`：参考图数量和下载大小上限。
- `tool_call_timeout_seconds`、`tool_loop_max_steps`：目标工具执行限制。
- `deliver_media_base64`、`media_image_max_mb`、`media_video_max_mb`、`media_max_count`：媒体 base64 转发开关、图片/视频大小上限与单次数量上限。
- `probe_enabled`、`probe_concurrency`、`probe_timeout_seconds`、`probe_memory_ttl_minutes`：模型查看/测速的开关、并发数、单模型超时与 url/key 记忆时长。

## 安全边界

- 外部图片只允许 http/https，并拒绝 localhost、内网 IP、回环地址和链路本地地址。
- 工具桥不会调用本插件自身的工具，防止递归。
- 停用工具、黑名单工具和 schema 不允许的参数会被拒绝或过滤。
- 图片缓存存放于 `data/plugin_data/astrbot_plugin_auto_tool_all/`，不会写入插件目录。
- 目标环境仍应通过 AstrBot 的工具权限配置限制高风险工具；本插件不会绕过工具权限。
