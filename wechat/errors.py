"""Channel-layer errors, mirroring src/errors.ts."""

from __future__ import annotations

from typing import Optional


class ChannelError(Exception):
    """Lifecycle / configuration error from the channel layer."""

    code: str

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.name = "ChannelError"


class WechatApiError(Exception):
    """Error returned by the WeChat ilink server or HTTP transport layer."""

    ret: Optional[int]
    errcode: Optional[int]
    errmsg: Optional[str]

    def __init__(self, payload: dict, message: Optional[str] = None) -> None:
        super().__init__(
            message
            or payload.get("errmsg")
            or f"wechat api error {payload.get('errcode') or payload.get('ret') or 'unknown'}"
        )
        self.name = "WechatApiError"
        self.ret = payload.get("ret")
        self.errcode = payload.get("errcode")
        self.errmsg = payload.get("errmsg")


MediaPhase = {"download", "decrypt", "upload", "encrypt"}


class MediaError(Exception):
    """Media I/O failure (download / decrypt / upload / encrypt)."""

    phase: str
    cause: Optional[object]

    def __init__(
        self, phase: str, cause: object, message: Optional[str] = None
    ) -> None:
        super().__init__(
            message or f"media {phase} failed: {cause!s}"
        )
        self.name = "MediaError"
        self.phase = phase
        self.cause = cause
