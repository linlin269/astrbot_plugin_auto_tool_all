# 更新日志

本项目的所有显著变更都记录在此文件中。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [v0.2.1] - 2026-09-01

本版本修复“看看我”画出来仍是机器人形象的问题：`avatar_draw` 链路中的身份解析与下游 selfie_image 的自拍流程劫持都做了防护。

### 修复

- **身份参数不再静默兜底为 bot**：`normalize_identity` 收到无法识别的值（如 `self`、`某个人`）时返回错误说明并列出合法取值，让上层模型带正确参数重试；此前任何未知值都会被当成“机器人”处理，导致“看看我”画成机器人头像。
- **新增身份别名**：`我自己`、`我本人`、`myself`、`my`、`mine`、`用户`、`ai`、`你自己`、`机器人自己` 等口语说法可正确解析。
- **非机器人身份自动跳过 selfie 专用工具**：`identity` 为 `sender`/`at` 时，`generate_selfie` 这类固定以机器人形象出镜的工具不再参与首选或回退，避免头像被无视、画出机器人；此时找不到可用工具会返回明确原因。
- **防 selfie 流程劫持**：为非机器人身份合成提示词时，把 selfie_image 意图路由的触发词（自拍/合影/合照/同框/形象照/和你/跟我/一起拍/你自己/你的照片及对应英文短语）改写为等价说法，并追加“主角不是机器人/AI 助手本人，禁止使用 AI 自身形象作为身份来源”的显式声明。
- **`debug_logging` 真正生效**：开启后在日志输出 identity 归一化结果、解析到的 QQ 号、头像 URL 与工具选择细节（含被跳过的 selfie 工具），便于排查“画错人”类问题。

### 变更

- `avatar_draw` 工具描述强化了 `identity` 传参规则：“看看我”必须传 `sender`，不要省略。

### 移除

- 移除仓库中的 `tests/` 测试目录及本地测试缓存（`.pytest_cache/`、`.ruff_cache/`）。测试不影响插件运行时行为，移除后插件功能不变；代码风格检查（ruff，见 `pyproject.toml`）保留。

## [v0.2.0] - 2026-09-01

本版本新增管理员专属的会话上下文清理能力，解决上下文过长导致回复变慢的问题。

### 新增

- 管理员专属指令组（仅 AstrBot WebUI 管理员可用，普通用户触发时**完全静默**）：
  - `/清空上文`（别名 `/清空对话`）：立即清空当前会话的 LLM 对话数据，重置为全新对话。
  - `/清空上文 all`：一键清空**所有会话**的上下文，并汇报清空的会话数量。
  - `/清空上文 6s|6秒|5min|5分钟|1h|1小时`：定时清理，延时到点后自动清空当前会话一次（非周期任务）；到点前重复设置会**覆盖**旧定时并在回复中提示。
  - `/查看清空上文定时任务`：列出所有定时任务，按触发时间排序编号，展示"群号/qq 号 + 触发时间 + 剩余时间"。
  - `/取消清空上文定时任务 序号|qq号|群号|all`：按列表序号、qq 号/群号或 `all` 取消定时任务。
- 定时任务到点执行时会向该会话主动推送"上下文已定时清空。"；平台不支持主动消息时静默跳过，仅记录日志。
- 重启通知：AstrBot 每次启动完成后（`on_astrbot_loaded` 钩子），读取 WebUI 配置的管理员列表（`admins_id`），向每位管理员的 QQ 私聊发送"报告主人，我刚刚重启啦，清理上下文定时任务全部清空。"。发送失败自动重试（最多 3 次，间隔 5 秒），仍未送达则只记录日志；未配置管理员则不发送。

### 变更

- `metadata.yaml`：版本号升至 v0.2.0，`desc` 补充上下文清理能力，新增 `session-management` 标签。
- `README.md`：新增「清空上下文」章节，说明全部指令与行为。
- 新增 `pyproject.toml` 声明 ruff lint 规则（后台任务宽泛异常捕获、本地墙钟时间均为有意为之）。

### 说明

- "清空"通过框架 `conversation_manager` 删除该会话的全部对话数据并重置当前对话指针，框架会在下次消息时自动新建对话，不影响头像缓存等其它插件数据。
- 定时任务**不持久化**：AstrBot 重启后全部消失，与重启通知文案对应。
- WebUI 热重载插件不触发重启通知（该通知仅在 AstrBot 完整启动时发送一次）。

## [v0.1.0] - 2026-09-01

首个版本：自然语言编排 AstrBot 全局 LLM 工具，并在 aiocqhttp / OneBot v11 上支持 QQ 头像参考生图。

### 新增

- **自然语言工具编排**（LLM 工具）：
  - `list_available_tools`：列出当前 active 的外部 LLM 工具，可被管理员开关（`enable_tool_listing`）。
  - `call_plugin_tool`：按工具名 + JSON 参数调用任意已注册的 `llm_tool`、`FunctionTool` 或 MCP 工具，经 Agent 工具循环执行，支持超时（`tool_call_timeout_seconds`）与最大步数（`tool_loop_max_steps`）配置。
  - 动态发现：工具列表在每次调用时实时读取，未来新安装并注册为 LLM 工具的插件无需修改本插件即可被发现。
- **QQ 头像参考生图**（`avatar_draw`，仅 aiocqhttp/OneBot v11）：
  - 头像身份支持机器人（bot）、发送者（sender）、被 @ 用户（at）。
  - 通过 qlogo 下载机器人/用户头像并缓存（`avatar_spec` 尺寸、`avatar_cache_ttl_minutes` 有效期）。
  - 参考图接力：当前消息图片、回复/引用消息图片、其它工具（如搜索）返回的外部图片 URL，均自动作为生图参考图。
  - 以本地 `Image` 组件临时注入事件，配合 `astrbot_plugin_selfie_image` 的 `generate_image` 等生图工具使用，支持工具回退列表（`image_tool_candidates`）。
- **安全边界**：
  - 外部图片仅允许 http/https，拒绝 localhost、内网 IP、回环与链路本地地址（SSRF 防护）。
  - 单张外部图片大小上限（`external_image_max_mb`）、单次参考图数量上限（`max_reference_images`）。
  - 工具桥禁止调用本插件自身工具，防止递归；支持工具白/黑名单（`allowed_tool_names` / `blocked_tool_names`），停用工具与 schema 之外的参数会被过滤。
  - 缓存数据存放在 `data/plugin_data/astrbot_plugin_auto_tool_all/`，不写入插件目录。
- 用户可视化配置面板（`_conf_schema.json`）。
- 单元测试（无需 AstrBot 运行环境即可执行）。

[v0.2.1]: https://github.com/linlin269/astrbot_plugin_auto_tool_all/compare/v0.2.0...v0.2.1
[v0.2.0]: https://github.com/linlin269/astrbot_plugin_auto_tool_all/compare/8a8f4e2...v0.2.0
[v0.1.0]: https://github.com/linlin269/astrbot_plugin_auto_tool_all/commit/8a8f4e2
