# python-wechat-channel

![PyPI Version](https://img.shields.io/pypi/v/python-wechat-channel)
![Python Versions](https://img.shields.io/pypi/pyversions/python-wechat-channel)
![License](https://img.shields.io/pypi/l/python-wechat-channel)

微信 ilink 协议 SDK，扫码登录 → 长连接收消息 → 回复，支持图片/文件/视频。

## 安装

```bash
pip install python-wechat-channel
```

或使用 uv：

```bash
uv add python-wechat-channel
```

## 快速开始

```python
import asyncio
from wechat import create_channel

async def handler(msg, reply):
    print(f"[{msg.from_user_id}] {msg.text}")
    await reply.text("收到！")

async def main():
    handle = await create_channel(
        bot_token="你的 bot_token",
        account_id="你的 account_id",
        on_message=handler,
    )
    await handle.start()

asyncio.run(main())
```

## 目录

- [扫码登录](#扫码登录)
- [接收消息与回复](#接收消息与回复)
- [完整示例](#完整示例)
- [API 参考](#api-参考)
- [错误处理](#错误处理)
- [环境变量](#环境变量)

---

## 扫码登录

首次使用时需要扫码登录获取 `bot_token` 和 `account_id`：

```python
import asyncio
from wechat.api import WechatApiClient
from wechat.login import run_login_flow

async def main():
    api = WechatApiClient(
        base_url="https://ilinkai.weixin.qq.com",
        cdn_base_url="https://novac2c.cdn.weixin.qq.com/c2c",
        channel_version="my-bot/1.0",
        bot_agent="my-bot/1.0",
    )

    result = await run_login_flow(
        api,
        bot_type="3",
        timeout_ms=120_000,
        on_qr_code=lambda img: print("请用微信扫码登录:", img[:80]),
        on_status=lambda s, info: print(f"  → {s}"),
    )

    if result.connected:
        print(f"bot_token  = {result.bot_token}")
        print(f"account_id = {result.account_id}")
    else:
        print(f"登录失败: {result.message}")

asyncio.run(main())
```

`run_login_flow` 回调说明：

| 回调 | 说明 |
|------|------|
| `on_qr_code(content)` | 扫码登录二维码内容（data URL 或 URL 字符串） |
| `on_status(status, info)` | 登录状态变化：`wait` / `scaned` / `confirmed` 等 |
| `on_verify_code(prompt)` | 服务器要求验证码时调用，需返回用户输入的 6 位验证码 |
| `on_qr_refresh(content)` | 二维码过期刷新时调用，收到新二维码后重新渲染 |
| `signal` | `asyncio.Event`，可随时中止登录轮询 |

---

## 接收消息与回复

`on_message` 回调接收 `InboundMsg`，回复使用 `Reply`：

```python
async def handler(msg, reply):
    user = msg.from_user_id
    text = msg.text

    # 文本消息
    await reply.text(f"你说了: {text}")

    # 图片/文件/视频（自动根据 MIME 类型上传）
    # await reply.media("/path/to/image.jpg", caption="这是图片")

    # 发送"对方正在输入"状态
    await reply.typing(True)   # 开始
    await asyncio.sleep(2)
    await reply.typing(False)  # 取消
```

### InboundMsg 属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `from_user_id` | `str` | 发送者用户 ID |
| `context_token` | `str` | 上下文 token，回复时需透传 |
| `text` | `str` | 文本内容 |
| `media` | `list[MediaRef]` | 解密后的本地媒体文件列表 |
| `raw` | `WeixinMessage` | 原始协议消息 |

### MediaRef 属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `path` | `str` | 本地文件绝对路径（已解密） |
| `mime` | `str` | MIME 类型，如 `image/jpeg` |

### Reply 方法

| 方法 | 说明 |
|------|------|
| `reply.text(content, max_chars?)` | 发送文本，自动分块（默认 4000 字/块） |
| `reply.media(file_path, caption?)` | 发送图片/视频/文件，自动上传到 CDN |
| `reply.typing(on=True)` | 发送"正在输入"心跳 |
| `reply.typing(on=False)` | 取消"正在输入" |

---

## 完整示例

扫码登录 + 消息收发的完整流程：

```python
import asyncio
from wechat.api import WechatApiClient
from wechat.login import run_login_flow
from wechat import create_channel


async def main():
    # ── 步骤 1：扫码登录 ──────────────────────────────────
    api = WechatApiClient(
        base_url="https://ilinkai.weixin.qq.com",
        cdn_base_url="https://novac2c.cdn.weixin.qq.com/c2c",
        channel_version="my-bot/1.0",
        bot_agent="my-bot/1.0",
    )

    login_result = await run_login_flow(
        api,
        bot_type="3",
        on_qr_code=lambda img: print("请用微信扫码登录"),
        on_status=lambda s, _: print(f"  → {s}"),
    )

    if not login_result.connected:
        print(f"登录失败: {login_result.message}")
        return

    print("登录成功！")

    # ── 步骤 2：启动消息 channel ──────────────────────────
    async def handler(msg, reply):
        print(f"[{msg.from_user_id}] {msg.text}")
        await reply.text("收到！")

    handle = await create_channel(
        bot_token=login_result.bot_token,
        account_id=login_result.account_id,
        base_url=login_result.base_url,
        on_message=handler,
    )

    print("消息 channel 已启动，长连接运行中（Ctrl+C 退出）...")
    await handle.start()


if __name__ == "__main__":
    asyncio.run(main())
```

---

## API 参考

### `create_channel`

```python
handle = await create_channel(
    bot_token="...",          # 必填，登录获取
    account_id="...",         # 必填，登录获取
    base_url="...",           # 可选，默认 https://ilinkai.weixin.qq.com
    cdn_base_url="...",       # 可选，默认 https://novac2c.cdn.weixin.qq.com/c2c
    channel_version="...",    # 可选，默认 wechat-channel/0.1.0
    state_dir="...",           # 可选，默认 ~/.wechat-channel/
    on_message=handler,        # 消息回调
    on_error=error_handler,    # 可选，错误回调
    blocked_users=set(),       # 可选，屏蔽用户 ID
    long_poll_timeout_ms=35000,# 可选，长轮询超时
)
```

### `ChannelHandle`

```python
await handle.start()   # 开始长连接
await handle.stop()     # 优雅停止
```

---

## 错误处理

```python
from wechat import ChannelError, WechatApiError, MediaError

# ChannelError — 配置错误，如缺少 bot_token
# WechatApiError — 微信服务器返回错误
# MediaError — 媒体上传/下载/解密失败，有 phase 属性指明阶段
```

### 常见 errcode

| errcode | 说明 | 处理方式 |
|---------|------|----------|
| `-14` | 会话过期 | 暂停 1 小时后自动恢复（SDK 自动处理） |

---

## 环境变量

| 变量 | 说明 |
|------|------|
| `WECHAT_CHANNEL_BASE_URL` | ilink 网关地址 |
| `WECHAT_CHANNEL_CDN_BASE_URL` | CDN 地址 |
| `WECHAT_CHANNEL_STATE_DIR` | 状态文件目录 |
| `WECHAT_CHANNEL_LONG_POLL_TIMEOUT_MS` | 长轮询超时 |

---

## 开发

```bash
git clone https://github.com/esyion/python-wechat-channel.git
cd python-wechat-channel
uv sync
uv run python -m pytest
```

## 发布

```bash
# 打标签触发 GitHub Action 自动发布到 PyPI
git tag v0.x.x
git push origin v0.x.x
```

---

## License

MIT
