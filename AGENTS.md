# 项目执行规范

## 模块边界

- `components/event_listeners/whale_listener.py`：LangBot 事件处理、会话缓存、私聊/群聊记忆和后台任务。
- `components/event_listeners/diana_features.py`：链接与文件解析、微信发送和公网下载校验。
- `README.md` 与 `readme/README_zh_Hans.md`：用户可见规则；行为变化时同步更新。

## 依赖与验证

LangBot 宿主提供 `langbot_plugin` SDK；独立测试环境需同时安装 SDK 与插件依赖：

```powershell
python -m pip install -r requirements.txt langbot-plugin
python -m unittest discover -s tests -v
python -m compileall -q main.py components tests
git diff --check
```

- 测试采用标准库 `unittest`，网络通过 `httpx.MockTransport` 模拟；不要调用真实模型、微信发送或朋友圈发布接口。
- 文档缓存的生产、读取和重置必须使用同一隔离键，覆盖机器人、会话和上传者。
- 公开朋友圈生成不得自动引入私聊记忆；自动发布由每日任务统一管理。
- 网页及用户提供的图片/表情包 URL 必须经过共享的公网请求检查，重定向不得跳过检查。
- 保持 UTF-8 和原有换行；不要记录密钥或用户原始私聊内容。
