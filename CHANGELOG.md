# 更新日志

本项目的所有显著变更都记录在此文件中。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [v0.3.0] - 2026-09-03

本版本把 `astrbot_plugin_anysearch` 的核心联网检索能力内置到插件中，默认可使用 AnySearch 访客模式；同时对无凭据的百度、Google、关键词搜图和以图搜图进行了匿名可行性验证，只发布实际稳定的能力。

### 新增

- **内置 AnySearch JSON-RPC 客户端**：直接调用 `https://api.anysearch.com/mcp`，API Key 留空时使用访客模式；不再依赖另行安装 `astrbot_plugin_anysearch`。
- **联网搜索工具**：新增 `anysearch_search`、`anysearch_batch_search`、`anysearch_extract`、`anysearch_site_search` 和统一入口 `web_search`，支持普通搜索、1～5 条批量搜索、公开网页 Markdown 正文提取及 `domain` / `sub_domain` 站点/垂直搜索。
- **兼容命令入口**：新增 `/anysearch`，别名 `/搜索`、`/websearch`；支持普通搜索、`extract` 和 `batch` 子命令。
- **OneBot 长结果分段**：直接命令结果可按 `search_command_chunk_size` 分段；私聊默认开启、群聊默认关闭，规避 NapCat 私聊合并转发 `retcode=1200`。
- **搜索配置**：新增搜索总开关、endpoint、可选 API Key、超时、结果数、批量数和命令分段配置。
- **第三方许可**：新增 `THIRD_PARTY_NOTICES.md`，保留 `astrbot_plugin_anysearch` 的 MIT 许可与原作者版权声明。

### 变更

- 本插件自己的搜索工具加入内部工具名单，`call_plugin_tool` 不会递归调用这些工具；主 Agent 可直接选择它们。
- 搜索和网页正文统一标注为不可信外部资料；单次结果、批量查询数、响应字节数和返回字符数均有硬限制。
- README 更新为 v0.3.0 实际能力、访客额度、数据发送边界和发布流程；tag 示例改为当前版本/通用版本形式。

### 安全

- `anysearch_extract` 在提交给第三方服务前拒绝非 http/https、本地、回环、私网、链路本地、组播和保留地址。
- 搜索服务 HTTP 错误不回显响应正文，避免错误页面中的凭据或敏感内容进入聊天；API Key 不写日志。
- 配置中的 AnySearch API Key 由 AstrBot 配置系统保存；README 不再将其误描述为只存在内存。搜索关键词、站点参数和待提取 URL 会发送给 AnySearch，不应包含敏感数据。

### 未纳入

- **百度普通搜索 / 百度图片**：匿名访问连续触发安全验证，无法作为无凭据稳定 provider。
- **Google 普通搜索 / Google Images**：当前网络返回需要 JavaScript 的结果壳，普通异步 HTTP 客户端无法稳定解析；无 API Key 时不承诺支持。
- **Bing Images 关键词搜图**：完整结果页会在短时间内降级为简化/拦截页，匿名返回不稳定，未注册正式工具。
- **以图搜图**：Google Lens、Bing Visual Search、Yandex Images、TinEye 的访客上传方式涉及登录、凭据、动态验证或图片隐私，当前版本全部不启用。
- AnySearch 服务端当前不提供 `list_domains` 工具（实测返回 `tool not found`），因此不暴露无效的站点列表工具；站点搜索保留显式 `domain` / `sub_domain` 参数。

[v0.3.0]: https://github.com/linlin269/astrbot_plugin_auto_tool_all/compare/v0.2.5...v0.3.0

## [v0.2.5] - 2026-09-02


本版本扩展 OpenAI 兼容接口的 key 识别方式，方便直接使用中文自然表达提交探测凭据。

### 修复

- **扩展 key 标注写法**：支持 `key:`、`key=`、`key：`、`key是`、`key为` 以及 `密钥是`、`口令为`、`令牌是` 等形式，并继续限制 key 字符集与最小长度。
- **修复中文紧邻 `sk-` 前缀时无法识别**：改用 ASCII 标识符边界，避免 Python Unicode `\b` 在中文字符前失效，同时避免从英文标识符中间误提取。

### 安全与兼容

- 保持 `sk-` 后缀至少 6 个字符的约束；文档示例使用伪造 key，不应填入真实密钥。
- 本次仅调整凭据文本解析和版本文档，不改变接口请求、key 内存缓存或脱敏逻辑。

[v0.2.5]: https://github.com/linlin269/astrbot_plugin_auto_tool_all/compare/v0.2.4...v0.2.5

## [v0.2.4] - 2026-09-02

本版本解决 fetch_media 抓取网页时把网站图标、站标、用户头像等干扰图一并发出的体验问题，实现"规则粗筛 + AI 精筛"两级清洗。

### 新增

- **规则粗筛（第一级）**：抓取页面时按完整 `<img>` 标签解析，依据 URL 特征（favicon/logo/icon/sprite/二维码/跟踪像素等）、头像特征（avatar/gravatar/qlogo/B 站 `/face/` 目录）、标签 class/id/alt 语义、声明尺寸（宽高均小于 64px 视为装饰）过滤干扰候选；补采懒加载 `data-src` 系属性与 `<video poster>` 封面。og:image 封面只拦头像类特征，避免误杀内容图。
- **AI 审阅精筛（第二级）**：规则粗筛后，把候选图以 `base64://` 引用交给当前聊天模型（需支持视觉）看图审阅——剔除图标/头像等非内容图，并按 `fetch_media` 新增的 `user_intent` 参数（用户诉求要点）挑选内容图。模型不支持视觉、超时（`llm_filter_timeout`，默认 20 秒）或输出无法解析时自动跳过（fail-open，不误删）；视频与超大图直接保留不送审。
- 全灭兜底：AI 判定全部候选都是干扰图时，如实告知用户"没有找到符合要求的内容图片"并附原始链接，不发噪声图。
- 新增配置：`fetch_media_filter_noise`、`llm_filter_enabled`、`llm_filter_timeout`（默认开启/20 秒）。

### 变更

- 媒体交付拆分为"下载准备"（`prepare_media`）与"发送"（`send_prepared`）两阶段，`deliver` 与 `fetch_media_result` 共用；行为与旧版一致（图片批量发、视频逐个发、失败回退 URL）。
- `fetch_media` 对同一次会话轮内已发送过的链接直接返回"刚刚已经发送过"，避免重复下载刷屏。
- 审阅输出解析（`parse_kept_indexes`）为纯函数：非 JSON、非整数、越界编号一律 fail-open，明确空数组才判定"全部为干扰图"。

[v0.2.4]: https://github.com/linlin269/astrbot_plugin_auto_tool_all/compare/v0.2.3...v0.2.4
## [v0.2.3] - 2026-09-02

本版本解决“用户要求下载图片发给自己时，机器人却调用生图工具画一张”的问题，并补齐媒体转发的两处覆盖盲区：搜索结果里没有媒体直链、模型绕过 `call_plugin_tool` 直接调用全局工具。

### 新增

- **`fetch_media` 媒体下载工具**：模型把图片/视频直链或网页链接交给本工具，插件下载后以 base64 媒体消息发出。收到 B 站视频页等网页链接时，自动抓取页面中的 `og:image` 封面与 `<img>` 插图候选（相对路径自动补全、og:image 优先、逐个过 SSRF 校验）；无法判定为网页的链接回退按内容类型直连下载。大小上限、数量上限与回退逻辑与现有媒体转发一致。
- **任意工具结果自动媒体转发（钩子层）**：监听 `on_llm_tool_respond`，主 Agent 直接调用任意工具（如 anysearch）返回后，自动把结果中的媒体直链下载转发，不再依赖模型走 `call_plugin_tool`。同一次会话轮内相同 URL 只发一次；`call_plugin_tool`、`fetch_media`、生图类工具自带交付逻辑，自动跳过防重复。
- 新增配置：`fetch_media_enabled`、`fetch_media_page_extract`、`fetch_media_page_max_kb`、`auto_deliver_tool_media`（默认全部开启）。

### 变更

- `call_plugin_tool` 引导语调整：搜索/信息类工具可直接调用，但“下载图片/视频发给我”类意图必须在拿到链接后调用 `fetch_media` 完成发送；找不到可下载媒体时如实告知用户，不得改用生图工具冒充下载结果。
- `_deliver_tool_media` 与钩子层共用同一份“本轮已发送 URL”记录（挂在事件对象上），杜绝同一链接双发。
- `fetch_media` 加入插件内部工具名单（`INTERNAL_TOOLS`），不能被 `call_plugin_tool` 递归调用。

### 安全

- 网页抓图仅接受 text/html 且限制单页大小（`fetch_media_page_max_kb`，默认 2048KB），重定向逐跳复查 SSRF；提取出的每个候选 URL 重新过公开地址校验（拒绝内网/回环/链路本地）。
- 钩子层全程防御式解析工具结果（兼容 CallToolResult 与纯文本载荷），异常只记日志，不影响消息主流程。

[v0.2.3]: https://github.com/linlin269/astrbot_plugin_auto_tool_all/compare/v0.2.2...v0.2.3
## [v0.2.2] - 2026-09-01

本版本新增两项能力：工具返回媒体（图片/视频）的 base64 转发，以及 OpenAI 兼容接口的模型查看与逐模型测速。

### 新增

- **媒体 base64 转发**：`call_plugin_tool` 调用的工具返回中出现的图片/视频（内层响应消息链组件或文本里的媒体直链），由插件下载后以 `Image.fromBase64` / `Video.fromBase64` 消息发出，规避 QQ 端拉取 URL 失败（防盗链/内网/被墙图床）。
  - 大小上限：图片 10MB（`media_image_max_mb`）、视频 100MB（`media_video_max_mb`），超限、下载失败或发送失败自动回退为发送 URL，消息不丢。
  - 单次最多转发 5 个媒体（`media_max_count`），防止刷屏；可用 `deliver_media_base64` 总开关关闭。
  - **临时文件即用即清**：为转 base64 下载的媒体在发送流程结束（无论成败）后立即删除；插件启动时还会清扫上次异常退出可能残留的临时文件。QQ 头像缓存等其它数据不受影响。
- **OpenAI 兼容接口模型查看/测速**：在消息里带上 url 和 key（四种给法：写在同一句、回复引用含 url/key 的消息、先发一条 url+key 再说指令、url 和 key 分两条消息先后发），然后说"看看里面有什么模型"或"帮我测试一下里面的模型"：
  - 查看模型：调用 `{url}/models`（url 已带版本段结尾则直接用，否则默认补 `/v1`；说"用 v3/api 查看"这类话可显式指定前缀），把模型列表分条发送。
  - 测试模型：对列表中**每个**模型用流式 `hi` 各测一次，记录首字时间（到第一个内容 token）与回复总时间；端点不支持流式时自动按非流式重试并注明。3 并发（`probe_concurrency`）、单模型 10 秒超时（`probe_timeout_seconds`）。
  - 只有收到真实内容回复的模型才算可用；报错（403/429/5xx）、超时、空回复一律视为不可用。
  - ≥6 个模型时先回"开始测试，共 N 个模型"，测完把汇总结果主动推送；结果按总回复时间从快到慢排序，模板形如 `gpt-4o-mini - 首字 0.8s - 总回复 2.3s`，并附不可用模型及原因。
  - url/key 分条发送依赖会话内内存记忆（`probe_memory_ttl_minutes`，默认 30 分钟）。

### 变更

- 工具桥内层提示词要求保留工具返回中的媒体链接，避免外层模型转述时丢掉图片/视频 URL。
- `call_plugin_tool` 返回文本在媒体发出后会附加一行发送摘要（发了几张图/几个视频或回退链接）。

### 安全

- key 只在内存中使用与记忆，不落盘、不写日志；错误信息与调试输出统一做 key 脱敏；一次成功的列表/测试后 key 立即从记忆中丢弃。
- 媒体下载复用现有 SSRF 防护：仅 http/https，拒绝内网、回环、链路本地与保留地址，重定向逐跳复查。

[v0.2.2]: https://github.com/linlin269/astrbot_plugin_auto_tool_all/compare/v0.2.1...v0.2.2
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
