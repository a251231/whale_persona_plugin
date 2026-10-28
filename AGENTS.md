# 项目执行规范

## 项目概览

本项目为 **DeepSeek 大肥鱼（Whale Girl / Dafeiyu）** LangBot 微信全能插件（`whale_persona_plugin`），提供双人格（傲娇毒舌 / 甜美粘人）私聊与群聊陪伴、前缀缓存优化、三层长期记忆、原生多模态看图与表情包收发、TTS 语音合成、联网搜索、链接与文档解析、多曲库点歌、朋友圈发布/浏览/自动点赞评论及自动同意好友申请等能力。

## 模块边界

- `main.py` 与 `manifest.yaml`：插件入口类 `WhalePersonaPlugin` 与配置项元数据定义。
- `components/event_listeners/whale_listener.py`：LangBot 事件处理、双人格 Prompt 构建、会话缓存、私聊/群聊记忆、多模态与工具调用（`web_search` / `send_meme` / `send_voice` / `share_music` / `post_moments` / `view_moments` / `like_moments` / `comment_moments`）以及后台定时任务（主动找人、每日作息与发圈、近 7 天活跃好友朋友圈互动、自动通过好友问候）。
- `components/event_listeners/diana_features.py`：链接与文件解析、多模态识图兜底与表情包入库、多曲库点歌、微信网关接口调用（发图/发语音/音乐卡片/发朋友圈/读朋友圈/点赞评论/自动通过好友）和公网下载校验。
- `components/event_listeners/interaction_settings.py`：设置注册表、交互档位、旧口令别名、菜单和会话覆盖项；设置解析不调用模型。
- `README.md` 与 `readme/README_zh_Hans.md`：用户可见规则；行为变化时同步更新。
- `tests/`：标准库 `unittest` 测试套件（设置解析、会话隔离、Diana 扩展、回复优化与防复读、朋友圈与好友申请等回归测试）。

## 模型与多模态规范

- 默认主模型：使用 `https://api.deepseek.com` 的 `deepseek-flash`（对应 `DeepSeek-V4.1-Flash-1017`），原生支持多模态 `image_url` 识图与 `tools` 调用。
- 模型名归一化：`_normalize_api_model` 必须保留 `deepseek-flash`（并将 `deepseek-v4.1-flash`、`deepseek-v4.1-flash-1017` 归一化为 `deepseek-flash`），不得将其降级为 `deepseek-chat`。
- 原生识图优先：当消息包含图片（`vision_b64`）且接口为 `api.deepseek.com` 时，直接以 `[{"type": "text", ...}, {"type": "image_url", ...}]` 请求 `deepseek-flash`（若配置仍为 `deepseek-chat` 则自动升级为 `deepseek-flash`）；硅基流动 VL（`Qwen/Qwen3-VL-30B-A3B-Instruct` / `Qwen/Qwen3-VL-8B-Instruct`）仅作为非多模态接口或异常时的兜底桥接，并按图片 SHA-256 缓存识图结果。

## 核心业务与安全规范

- 文档缓存的生产、读取和重置必须使用同一隔离键，覆盖机器人、会话和上传者。
- 公开朋友圈生成不得自动引入私聊记忆；自动发布由每日任务统一管理。自身发出的朋友圈需记录于 `self_moments_history`，向聊天上下文注入时优先展示最新一条。
- 会话设置保存于 `conversation_settings` 的 `group:<group_id>` / `person:<sender_id>` 键；旧全局值仅作为默认，读取不得写入覆盖项。群白名单与回应开关需同步。
- 设置行为由统一模块处理；监听器及扩展消费者读取有效设置。档位不更改语音、表情、人格或全局发布开关；自动朋友圈、朋友圈互动开关与自动加好友开关仅配置的主人私聊可改。
- 回复质量护栏：
  - 严禁原样复读用户的口头禅或消息首句，出站前通过 `_sanitize_reply_echo_and_catchphrases` 清洗复读前缀与高频词堆积。
  - 群聊回复不得由代码硬编码拼接 `@昵称` 前缀；支持口令触发群聊临时闭嘴冷却，被直接 `@` 或点名时自动解除。
  - 主动找人（Speak-up）在用户未回复期间最多触发 2 次，达到上限后必须等待用户实际回复方可清零计数。
- 网页及用户提供的图片/表情包 URL 必须经过共享的公网请求检查，重定向不得跳过检查。
- 保持 UTF-8 和原有换行；不要记录密钥或用户原始私聊内容。
- 线上容器热更新时，仅更新 `WhalePersonaPlugin` 对应的制品目录（通过 `main.py` 中的 `WhalePersonaPlugin` 识别），严禁全局覆盖其他插件制品的 `manifest.yaml`；线上本地安装制品的 `metadata.author` 保持为 `local`。

## 依赖与验证

LangBot 宿主提供 `langbot_plugin` SDK；独立测试环境需同时安装 SDK 与插件依赖：

```powershell
python -m pip install -r requirements.txt langbot-plugin
python -m unittest discover -s tests -v
python -m compileall -q main.py components tests
git diff --check
```

- 测试采用标准库 `unittest`，网络通过 `httpx.MockTransport` 模拟；不要调用真实模型、微信发送或朋友圈发布接口。
