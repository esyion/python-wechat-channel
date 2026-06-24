"""Outbound messaging — mirrors src/channel/outbound.ts."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from wechat.api import WechatApiClient
from wechat.crypto import aes_key_hex_to_base64
from wechat.media import get_mime_from_filename, upload_attachment, upload_image, upload_video
from wechat.types import MessageItem, MessageItemType, MessageState, MessageType, SendMessageReq

# ---------------------------------------------------------------------------
# Text chunking
# ---------------------------------------------------------------------------


def chunk_text(text: str, max_chars: int) -> list[str]:
    """Split text into chunks of at most ``max_chars``, preferring line/space breaks."""
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    rest = text
    while rest:
        if len(rest) <= max_chars:
            chunks.append(rest)
            break

        cut = rest.rfind("\n", 0, max_chars)
        if cut < max_chars * 0.6:
            cut = rest.rfind(" ", 0, max_chars)
        if cut < max_chars * 0.6:
            cut = max_chars

        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip()

    return chunks


def _new_client_id() -> str:
    return f"wac:{os.urandom(8).hex()}"


# ---------------------------------------------------------------------------
# Send context
# ---------------------------------------------------------------------------


class SendCtx:
    """Immutable context for sending outbound messages."""

    def __init__(
        self,
        api: WechatApiClient,
        to_user_id: str,
        context_token: str,
        *,
        default_max_chars: Optional[int] = None,
    ) -> None:
        self.api = api
        self.to_user_id = to_user_id
        self.context_token = context_token
        self.default_max_chars = default_max_chars


async def send_text(ctx: SendCtx, text: str, *, max_chars: Optional[int] = None) -> None:
    """Send a text message, auto-chunked."""
    max_len = max_chars or ctx.default_max_chars or 4000
    for chunk in chunk_text(text, max_len):
        await ctx.api.send_message(
            SendMessageReq(
                msg={
                    "from_user_id": "",
                    "to_user_id": ctx.to_user_id,
                    "client_id": _new_client_id(),
                    "message_type": MessageType.BOT,
                    "message_state": MessageState.FINISH,
                    "item_list": [{"type": MessageItemType.TEXT, "text_item": {"text": chunk}}],
                    "context_token": ctx.context_token,
                }
            )
        )


async def send_media(ctx: SendCtx, file_path: str, caption: Optional[str] = None) -> None:
    """Upload a file to CDN and send it as a media message."""
    path = Path(file_path)
    if not path.is_file():
        from wechat.errors import MediaError

        raise MediaError("upload", ValueError(f"not a file: {file_path}"), f"not a file: {file_path}")

    mime = get_mime_from_filename(file_path)

    if mime.startswith("image/"):
        uploaded = await upload_image(ctx.api, file_path, ctx.to_user_id)
        media_item: dict = {
            "type": MessageItemType.IMAGE,
            "image_item": {
                "aeskey": uploaded.aeskey,
                "media": {
                    "encrypt_query_param": uploaded.download_encrypted_query_param,
                    "aes_key": aes_key_hex_to_base64(uploaded.aeskey),
                    "encrypt_type": 1,
                },
                "mid_size": uploaded.file_size_ciphertext,
            },
        }
    elif mime.startswith("video/"):
        uploaded = await upload_video(ctx.api, file_path, ctx.to_user_id)
        media_item = {
            "type": MessageItemType.VIDEO,
            "video_item": {
                "media": {
                    "encrypt_query_param": uploaded.download_encrypted_query_param,
                    "aes_key": aes_key_hex_to_base64(uploaded.aeskey),
                    "encrypt_type": 1,
                },
                "video_size": uploaded.file_size_ciphertext,
            },
        }
    else:
        uploaded = await upload_attachment(ctx.api, file_path, ctx.to_user_id)
        file_name = path.name
        media_item = {
            "type": MessageItemType.FILE,
            "file_item": {
                "media": {
                    "encrypt_query_param": uploaded.download_encrypted_query_param,
                    "aes_key": aes_key_hex_to_base64(uploaded.aeskey),
                    "encrypt_type": 1,
                },
                "file_name": file_name,
                "len": str(uploaded.file_size),
            },
        }

    # Send caption as text first
    if caption:
        await send_text(ctx, caption, max_chars=ctx.default_max_chars)

    # Send media
    await ctx.api.send_message(
        SendMessageReq(
            msg={
                "from_user_id": "",
                "to_user_id": ctx.to_user_id,
                "client_id": _new_client_id(),
                "message_type": MessageType.BOT,
                "message_state": MessageState.FINISH,
                "item_list": [media_item],
                "context_token": ctx.context_token,
            }
        )
    )
