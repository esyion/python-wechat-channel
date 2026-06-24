"""python-wechat-channel — WeChat ilink protocol for Python.

Usage:
    from wechat import create_channel

    handle = await create_channel(
        bot_token="...",
        account_id="...",
        on_message=my_handler,
    )
    await handle.start()
    # ...
    await handle.stop()
"""

from wechat.channel import (
    ChannelHandle,
    InboundMsg,
    MediaRef,
    Reply,
    create_channel,
)
from wechat.errors import ChannelError, MediaError, WechatApiError
from wechat.login import LoginResult, QrStatus, RequestedQr, LoginResult

__all__ = [
    # errors
    "ChannelError",
    "WechatApiError",
    "MediaError",
    # factory
    "create_channel",
    "ChannelHandle",
    # inbound
    "InboundMsg",
    "MediaRef",
    # outbound reply
    "Reply",
    # login
    "LoginResult",
    "QrStatus",
    "RequestedQr",
]
