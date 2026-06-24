"""
Top-level Channel — mirrors src/channel/create.ts + long-poll.ts + inbound.ts.

Usage:
    from wechat import create_channel

    handle = await create_channel(
        bot_token="...",
        account_id="...",
        on_message=my_handler,
    )
    await handle.start()
    # ... bot runs
    await handle.stop()
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Optional

from wechat.api import WechatApiClient
from wechat.errors import ChannelError, MediaError, WechatApiError
from wechat.media import download_and_decrypt_cdn, save_inbound_media
from wechat.outbound import SendCtx, send_media, send_text
from wechat.store import JsonFileStore, MemoryStore, Store
from wechat.types import (
    GetUpdatesReq,
    MessageItemType,
    SendMessageReq,
    SendTypingReq,
    TypingStatus,
    WeixinMessage,
)

__all__ = ["create_channel", "ChannelHandle"]

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
DEFAULT_CHANNEL_VERSION = "wechat-channel/0.1.0"

# ---------------------------------------------------------------------------
# Long-poll loop
# ---------------------------------------------------------------------------

_SESSION_EXPIRED = -14
_CONSECUTIVE_FAILURE_LIMIT = 3
_BACKOFF_MS = 30_000
_RETRY_DELAY_MS = 2_000
_SESSION_PAUSE_S = 3600  # 1 hour


def _sanitize_user_id(user_id: str) -> str:
    """Sanitize user ID for use as a directory name."""
    return re.sub(r"[^a-zA-Z0-9_@.\-]", "_", user_id)[:64]


async def _build_inbound(
    api: WechatApiClient,
    media_tmp_dir: str,
    msg: WeixinMessage,
) -> "InboundMsg":
    """Parse a WeixinMessage into an InboundMsg with decrypted media files."""
    from_user_id = msg.from_user_id or "unknown"
    context_token = msg.context_token or ""

    user_dir = Path(media_tmp_dir) / _sanitize_user_id(from_user_id)
    user_dir.mkdir(parents=True, exist_ok=True)

    text = ""
    media: list[MediaRef] = []

    for item in msg.item_list or []:
        # Text
        if item.type == MessageItemType.TEXT and item.text_item and item.text_item.text:
            text = str(item.text_item.text)
            continue

        # Voice (text transcript)
        if item.type == MessageItemType.VOICE and item.voice_item and item.voice_item.text:
            text = str(item.voice_item.text)

        # Image
        if item.type == MessageItemType.IMAGE and item.image_item:
            img = item.image_item
            if not (img.media and (img.media.encrypt_query_param or img.media.full_url)):
                continue
            try:
                buf = await download_and_decrypt_cdn(
                    cdn_base_url=api.cdn_base_url,
                    encrypted_query_param=img.media.encrypt_query_param or "",
                    aes_key_hex=img.aeskey,
                    aes_key_base64=img.media.aes_key if not img.aeskey else None,
                    full_url=img.media.full_url,
                    label="image",
                )
                path_str = await save_inbound_media(
                    dest_dir=str(user_dir),
                    filename=f"img-{os.urandom(4).hex()}.jpg",
                    buf=buf,
                )
                media.append(MediaRef(path=path_str, mime="image/jpeg"))
            except Exception as exc:
                raise MediaError("decrypt", exc) from exc
            continue

        # File
        if item.type == MessageItemType.FILE and item.file_item:
            f = item.file_item
            if not (f.media and (f.media.encrypt_query_param or f.media.full_url)):
                continue
            try:
                buf = await download_and_decrypt_cdn(
                    cdn_base_url=api.cdn_base_url,
                    encrypted_query_param=f.media.encrypt_query_param or "",
                    aes_key_base64=f.media.aes_key,
                    full_url=f.media.full_url,
                    label="file",
                )
                name = f.file_name or f"file-{os.urandom(4).hex()}.bin"
                path_str = await save_inbound_media(dest_dir=str(user_dir), filename=name, buf=buf)
                media.append(MediaRef(path=path_str, mime="application/octet-stream"))
            except Exception as exc:
                raise MediaError("decrypt", exc) from exc
            continue

        # Voice (audio)
        if item.type == MessageItemType.VOICE and item.voice_item:
            v = item.voice_item
            if not (v.media and (v.media.encrypt_query_param or v.media.full_url)):
                continue
            try:
                buf = await download_and_decrypt_cdn(
                    cdn_base_url=api.cdn_base_url,
                    encrypted_query_param=v.media.encrypt_query_param or "",
                    aes_key_base64=v.media.aes_key,
                    full_url=v.media.full_url,
                    label="voice",
                )
                path_str = await save_inbound_media(
                    dest_dir=str(user_dir),
                    filename=f"voice-{os.urandom(4).hex()}.silk",
                    buf=buf,
                )
                media.append(MediaRef(path=path_str, mime="audio/silk"))
            except Exception as exc:
                raise MediaError("decrypt", exc) from exc
            continue

        # Video
        if item.type == MessageItemType.VIDEO and item.video_item:
            v = item.video_item
            if not (v.media and (v.media.encrypt_query_param or v.media.full_url)):
                continue
            try:
                buf = await download_and_decrypt_cdn(
                    cdn_base_url=api.cdn_base_url,
                    encrypted_query_param=v.media.encrypt_query_param or "",
                    aes_key_base64=v.media.aes_key,
                    full_url=v.media.full_url,
                    label="video",
                )
                path_str = await save_inbound_media(
                    dest_dir=str(user_dir),
                    filename=f"video-{os.urandom(4).hex()}.mp4",
                    buf=buf,
                )
                media.append(MediaRef(path=path_str, mime="video/mp4"))
            except Exception as exc:
                raise MediaError("decrypt", exc) from exc
            continue

    if not text and not media:
        text = "[empty message]"

    return InboundMsg(
        from_user_id=from_user_id,
        context_token=context_token,
        text=text,
        media=media,
        raw=msg,
    )


# ---------------------------------------------------------------------------
# Typing keepalive
# ---------------------------------------------------------------------------


class TypingKeepalive:
    """Sends "typing" heartbeats to a user at regular intervals."""

    def __init__(self, api: WechatApiClient, user_id: str, context_token: str, *, interval_s: float = 5.0) -> None:
        self.api = api
        self.user_id = user_id
        self.context_token = context_token
        self.interval_s = interval_s
        self._ticket: Optional[str] = None
        self._timer: Optional[asyncio.Task[None]] = None
        self._stopped = False

    async def start(self) -> None:
        if self._stopped:
            return
        try:
            cfg = await self.api.get_config(
                ilink_user_id=self.user_id,
                context_token=self.context_token,
            )
            self._ticket = cfg.typing_ticket
            if not self._ticket:
                return
            await self._fire(TypingStatus.TYPING)
            self._timer = asyncio.create_task(self._run())
        except Exception:
            pass  # best-effort

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._timer:
            self._timer.cancel()
            self._timer = None
        if self._ticket:
            asyncio.create_task(self._fire(TypingStatus.CANCEL))

    async def _run(self) -> None:
        while not self._stopped:
            await asyncio.sleep(self.interval_s)
            if self._stopped:
                break
            try:
                await self._fire(TypingStatus.TYPING)
            except Exception:
                self.stop()
                break

    async def _fire(self, status: TypingStatus) -> None:
        if not self._ticket:
            return
        await self.api.send_typing(
            SendTypingReq(
                ilink_user_id=self.user_id,
                typing_ticket=self._ticket,
                status=status,
            )
        )


# ---------------------------------------------------------------------------
# Reply helper
# ---------------------------------------------------------------------------


class Reply:
    """Per-message outbound helper returned to on_message handlers."""

    def __init__(
        self,
        api: WechatApiClient,
        to_user_id: str,
        context_token: str,
        *,
        default_max_chars: Optional[int] = None,
    ) -> None:
        self._ctx = SendCtx(api, to_user_id, context_token, default_max_chars=default_max_chars)
        self._typing: Optional[TypingKeepalive] = None
        self._typing_started = False

    async def text(self, content: str, *, max_chars: Optional[int] = None) -> None:
        await send_text(self._ctx, content, max_chars=max_chars)

    async def media(self, file_path: str, caption: Optional[str] = None) -> None:
        await send_media(self._ctx, file_path, caption)

    async def typing(self, on: bool = True) -> None:
        if on and not self._typing_started:
            self._typing = TypingKeepalive(
                self._ctx.api,
                self._ctx.to_user_id,
                self._ctx.context_token,
            )
            await self._typing.start()
            self._typing_started = True
        elif not on and self._typing_started:
            if self._typing:
                self._typing.stop()
            self._typing_started = False


# ---------------------------------------------------------------------------
# Inbound message type
# ---------------------------------------------------------------------------


class MediaRef:
    """Reference to a local decrypted media file."""

    path: str
    mime: str

    def __init__(self, *, path: str, mime: str) -> None:
        self.path = path
        self.mime = mime


class InboundMsg:
    """An inbound message passed to on_message handlers."""

    def __init__(
        self,
        *,
        from_user_id: str,
        context_token: str,
        text: str,
        media: list[MediaRef],
        raw: WeixinMessage,
    ) -> None:
        self.from_user_id = from_user_id
        self.context_token = context_token
        self.text = text
        self.media = media
        self.raw = raw


# ---------------------------------------------------------------------------
# Channel handle
# ---------------------------------------------------------------------------


class ChannelHandle:
    """Handle returned by create_channel(). Call start() to begin long-polling."""

    api: WechatApiClient

    def __init__(
        self,
        api: WechatApiClient,
        store: Store,
        media_tmp_dir: str,
        on_message: Optional[Callable[..., Any]] = None,
        on_error: Optional[Callable[..., Any]] = None,
        long_poll_timeout_ms: int = 35_000,
        blocked_users: Optional[frozenset[str]] = None,
        default_max_chars: Optional[int] = None,
    ) -> None:
        self.api = api
        self._store = store
        self._media_tmp_dir = media_tmp_dir
        self._on_message = on_message
        self._on_error = on_error or (lambda err, ctx=None: print(f"[wechat-channel] error: {err}"))
        self._long_poll_timeout_ms = long_poll_timeout_ms
        self._blocked_users = blocked_users or frozenset()
        self._default_max_chars = default_max_chars
        self._stop_event: Optional[asyncio.Event] = None
        self._loop_task: Optional[asyncio.Task[None]] = None

    async def start(self, *, signal: Optional[asyncio.Event] = None) -> None:
        if self._loop_task:
            raise ChannelError("ABORTED", "channel already started")

        stop_event = self._stop_event = asyncio.Event()
        external_signal = signal

        async def run_loop() -> None:
            # Notify start
            try:
                resp = await self.api.notify_start()
                if resp.ret and resp.ret != 0:
                    self._on_error(WechatApiError({"ret": resp.ret, "errmsg": resp.errmsg}), {"phase": "notifyStart"})
            except Exception as exc:
                self._on_error(exc, {"phase": "notifyStart"})

            consecutive = 0
            session_paused_until = 0.0

            while not stop_event.is_set():
                loop = asyncio.get_event_loop()
                now = loop.time()

                # Check pause
                if now < session_paused_until:
                    remaining = session_paused_until - now
                    try:
                        await asyncio.sleep(remaining)
                    except asyncio.CancelledError:
                        break
                    if stop_event.is_set():
                        break
                    continue

                # Check external signal
                if external_signal and external_signal.is_set():
                    break

                # Fetch updates
                sync_buf = await self._store.get("sync_buf") or ""
                try:
                    resp = await self.api.get_updates(
                        GetUpdatesReq(get_updates_buf=sync_buf),
                        timeout_ms=self._long_poll_timeout_ms,
                    )
                except Exception as exc:
                    consecutive += 1
                    self._on_error(exc, {"phase": "getUpdates"})
                    wait = _BACKOFF_MS / 1000 if consecutive >= _CONSECUTIVE_FAILURE_LIMIT else _RETRY_DELAY_MS / 1000
                    try:
                        await asyncio.sleep(wait)
                    except asyncio.CancelledError:
                        break
                    continue

                # Validate response
                if not isinstance(resp, dict):
                    self._on_error(ValueError("getUpdates returned non-object response"), {"phase": "getUpdates"})
                    consecutive += 1
                    await asyncio.sleep(_RETRY_DELAY_MS / 1000)
                    continue

                ret = resp.get("ret")
                errcode = resp.get("errcode")
                is_error = (ret is not None and ret != 0) or (errcode is not None and errcode != 0)
                if is_error:
                    if errcode == _SESSION_EXPIRED or ret == _SESSION_EXPIRED:
                        session_paused_until = loop.time() + _SESSION_PAUSE_S
                        self._on_error(
                            WechatApiError({"ret": ret, "errcode": errcode, "errmsg": resp.get("errmsg")}),
                            {"phase": "sessionExpired"},
                        )
                        continue
                    consecutive += 1
                    self._on_error(
                        WechatApiError({"ret": ret, "errcode": errcode, "errmsg": resp.get("errmsg")}),
                        {"phase": "getUpdates"},
                    )
                    wait = _BACKOFF_MS / 1000 if consecutive >= _CONSECUTIVE_FAILURE_LIMIT else _RETRY_DELAY_MS / 1000
                    try:
                        await asyncio.sleep(wait)
                    except asyncio.CancelledError:
                        break
                    continue

                consecutive = 0

                # Save sync buffer
                new_buf = resp.get("get_updates_buf")
                if new_buf:
                    await self._store.set("sync_buf", str(new_buf))

                # Process messages
                msgs: list[dict] = resp.get("msgs") or []
                for full_msg_dict in msgs:
                    if stop_event.is_set():
                        break
                    if external_signal and external_signal.is_set():
                        break

                    try:
                        full_msg = WeixinMessage.model_validate(full_msg_dict)
                    except Exception as exc:
                        self._on_error(exc, {"phase": "inbound"})
                        continue

                    user_id = full_msg.from_user_id or ""
                    if not user_id:
                        continue
                    if user_id in self._blocked_users:
                        continue

                    context_token = full_msg.context_token or await self._store.get(f"ctx:{user_id}") or ""
                    if full_msg.context_token:
                        await self._store.set(f"ctx:{user_id}", full_msg.context_token)

                    # Build inbound message
                    try:
                        inbound = await _build_inbound(self.api, self._media_tmp_dir, full_msg)
                    except Exception as exc:
                        self._on_error(exc, {"phase": "inbound"})
                        continue

                    # Call user handler
                    if self._on_message:
                        reply = Reply(
                            self.api,
                            user_id,
                            context_token,
                            default_max_chars=self._default_max_chars,
                        )
                        try:
                            await self._on_message(inbound, reply)
                        except Exception as exc:
                            self._on_error(exc, {"phase": "handler"})

            # notifyStop on exit
            try:
                await self.api.notify_stop()
            except Exception as exc:
                self._on_error(exc, {"phase": "notifyStop"})

        self._loop_task = asyncio.create_task(run_loop())
        await asyncio.sleep(0)  # let the loop start

    async def stop(self) -> None:
        if self._stop_event:
            self._stop_event.set()
        if self._loop_task:
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        await self._store.flush()
        self._loop_task = None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def _load_env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    val = os.environ.get(key)
    return int(val) if val else default


async def create_channel(
    *,
    bot_token: str,
    account_id: str,
    base_url: Optional[str] = None,
    cdn_base_url: Optional[str] = None,
    channel_version: Optional[str] = None,
    bot_agent: Optional[str] = None,
    state_dir: Optional[str] = None,
    store: Optional[Store] = None,
    on_message: Optional[Callable[..., Any]] = None,
    on_error: Optional[Callable[..., Any]] = None,
    long_poll_timeout_ms: Optional[int] = None,
    media_tmp_dir: Optional[str] = None,
    blocked_users: Optional[set[str]] = None,
    default_max_chars: Optional[int] = None,
) -> ChannelHandle:
    """Create a fully-wired WeChat channel handle."""

    if not bot_token:
        raise ChannelError("AUTH_REQUIRED", "bot_token is required")
    if not account_id:
        raise ChannelError("INVALID_TOKEN", "account_id is required")

    env_base_url = _load_env("WECHAT_CHANNEL_BASE_URL", DEFAULT_BASE_URL)
    env_cdn_base_url = _load_env("WECHAT_CHANNEL_CDN_BASE_URL", DEFAULT_CDN_BASE_URL)
    env_state_dir = _load_env("WECHAT_CHANNEL_STATE_DIR", "")
    env_long_poll = _env_int("WECHAT_CHANNEL_LONG_POLL_TIMEOUT_MS", 35_000)

    base_url = base_url or env_base_url
    cdn_base_url = cdn_base_url or env_cdn_base_url
    channel_version = channel_version or DEFAULT_CHANNEL_VERSION
    bot_agent = bot_agent or channel_version
    long_poll_timeout_ms = long_poll_timeout_ms or env_long_poll

    api = WechatApiClient(
        base_url=base_url,
        cdn_base_url=cdn_base_url,
        bot_token=bot_token,
        channel_version=channel_version,
        bot_agent=bot_agent,
        long_poll_timeout_ms=long_poll_timeout_ms,
    )

    if not state_dir:
        state_dir = env_state_dir or os.path.join(os.environ.get("HOME", "."), ".wechat-channel")

    if store is None:
        store = JsonFileStore(str(Path(state_dir) / "store.json"))

    media_tmp_dir = media_tmp_dir or str(Path(state_dir) / "media")

    blocked_users_frozen = frozenset(blocked_users) if blocked_users else frozenset()

    return ChannelHandle(
        api=api,
        store=store,
        media_tmp_dir=media_tmp_dir,
        on_message=on_message,
        on_error=on_error,
        long_poll_timeout_ms=long_poll_timeout_ms,
        blocked_users=blocked_users_frozen,
        default_max_chars=default_max_chars,
    )
