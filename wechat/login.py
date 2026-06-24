"""QR code login flow — mirrors src/wechat/login.ts.

Usage:
    from wechat.login import run_login_flow
    result = await run_login_flow(api, bot_type="3", on_qr_code=print)
"""

from __future__ import annotations

import asyncio
import base64
import io
import re
import uuid
from typing import Any, Callable, Optional

import qrcode

from wechat.api import WechatApiClient

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

QrStatus = str  # "wait" | "scaned" | "confirmed" | "expired" | ...

QrStatus_values = {
    "wait",
    "scaned",
    "confirmed",
    "expired",
    "scaned_but_redirect",
    "need_verifycode",
    "verify_code_blocked",
    "binded_redirect",
}


class LoginResult:
    connected: bool
    bot_token: Optional[str] = None
    account_id: Optional[str] = None
    base_url: Optional[str] = None
    user_id: Optional[str] = None
    already_connected: bool = False
    message: str

    def __init__(
        self,
        connected: bool,
        message: str,
        *,
        bot_token: Optional[str] = None,
        account_id: Optional[str] = None,
        base_url: Optional[str] = None,
        user_id: Optional[str] = None,
        already_connected: bool = False,
    ) -> None:
        self.connected = connected
        self.message = message
        self.bot_token = bot_token
        self.account_id = account_id
        self.base_url = base_url
        self.user_id = user_id
        self.already_connected = already_connected


class RequestedQr:
    qrcode: str
    qrcode_img_content: str

    def __init__(self, qrcode: str, qrcode_img_content: str) -> None:
        self.qrcode = qrcode
        self.qrcode_img_content = qrcode_img_content


# ---------------------------------------------------------------------------
# Step 1: fetch QR code
# ---------------------------------------------------------------------------


async def request_qr_code(
    api: WechatApiClient, *, bot_type: str = "3"
) -> RequestedQr:
    resp = await api.get_bot_qrcode(bot_type=bot_type)
    if not resp.qrcode or not resp.qrcode_img_content:
        raise RuntimeError("Failed to fetch QR code from ilink API")
    return RequestedQr(qrcode=resp.qrcode, qrcode_img_content=resp.qrcode_img_content)


# ---------------------------------------------------------------------------
# Step 1b: decode qrcode_img_content into a boolean matrix
# ---------------------------------------------------------------------------

def decode_qr_matrix(qrcode_img_content: str) -> list[list[bool]]:
    """
    Decode ``qrcode_img_content`` into a 2D boolean matrix.

    Three accepted shapes per the ilink protocol doc:
      - ``data:image/png;base64,...``  — embedded PNG; decode → QR text → matrix
      - ``https://liteapp.weixin.qq.com/q/...?qrcode=...`` — URL; encode directly
      - ``weixin://...``                — WeChat deep link; encode as text

    Returns rows × cols, True = dark module.
    """
    # Handle embedded PNG
    m = re.match(r"^data:image/png;base64,(.+)$", qrcode_img_content)
    if m:
        png_bytes = base64.b64decode(m.group(1))
        img = _load_png_matrix(io.BytesIO(png_bytes))
        return img

    # HTTP(S) URL or arbitrary text — encode directly
    return text_to_qr_matrix(qrcode_img_content)


def _load_png_matrix(stream: io.BytesIO) -> list[list[bool]]:
    """
    Load a PNG from a BytesIO and return a boolean matrix (dark = True).

    Minimal pure-Python PNG loader for QR recovery — handles 1-bit grayscale only.
    """
    # Check PNG signature
    sig = stream.read(8)
    if sig != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Not a valid PNG file")

    def _read_chunk(stream: io.BytesIO) -> tuple[str, bytes]:
        length = struct.unpack(">I", stream.read(4))[0]
        chunk_type = stream.read(4).decode("ascii")
        data = stream.read(length)
        stream.read(4)  # CRC
        return chunk_type, data

    import struct

    width = height = bit_depth = color_type = 0
    raw_data = b""
    compression_method = 0
    interlace_method = 0

    while True:
        chunk_type, data = _read_chunk(stream)

        if chunk_type == "IHDR":
            (width, height, bit_depth, color_type, compression_method, filter_method, interlace_method) = struct.unpack(">IIBBBBB", data)
        elif chunk_type == "IDAT":
            raw_data += data
        elif chunk_type == "IEND":
            break

    if color_type != 0:  # Only grayscale is simple to decode here
        raise ValueError(f"Only grayscale PNG (color_type=0) supported for QR recovery, got {color_type}")

    # Decompress with zlib
    import zlib

    decompressed = zlib.decompress(raw_data)

    # Parse scanlines (filter byte + row bytes per scanline)
    stride = width * bit_depth // 8
    matrix: list[list[bool]] = []

    for y in range(height):
        filter_type = decompressed[y * (stride + 1)]
        row_data = decompressed[y * (stride + 1) + 1 : (y + 1) * (stride + 1)]

        # For 1-bit grayscale, each byte = 8 pixels (MSB first)
        row: list[bool] = []
        for byte in row_data:
            for bit in range(7, -1, -1):
                if len(row) < width:
                    row.append(bool((byte >> bit) & 1))
        matrix.append(row)

    return matrix


def text_to_qr_matrix(text: str) -> list[list[bool]]:
    """Encode arbitrary text as a QR boolean matrix."""
    bits = _encode_qr_raw(text)
    if not bits:
        raise ValueError("encodeQR returned empty result")
    width = len(bits[0])
    height = len(bits)
    matrix: list[list[bool]] = []
    for row in bits:
        matrix.append([bool(pixel) for pixel in row])
    return matrix


def _encode_qr_raw(text: str) -> list[list[int]]:
    """
    Encode text into a raw QR bit array using the qr library.
    Returns list of rows, each row is list of 0/1 integers.
    """
    qr = qrcode.QRCode(border=0, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image()
    # img is a PIL Image; convert to 2D bit array
    pixels = img.load()
    w, h = img.size
    rows: list[list[int]] = []
    for y in range(h):
        row: list[int] = []
        for x in range(w):
            row.append(1 if pixels[x, y] else 0)
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Step 2: poll get_qrcode_status until terminal state
# ---------------------------------------------------------------------------


async def poll_qr_login(
    api: WechatApiClient,
    *,
    qrcode: str,
    timeout_ms: int = 480_000,
    signal: Optional[asyncio.Event] = None,
    on_verify_code: Optional[Callable[[str], Any]] = None,
    on_status: Optional[Callable[[str, Optional[dict]], Any]] = None,
    on_qr_refresh: Optional[Callable[[str], Any]] = None,
) -> LoginResult:
    """
    Poll get_qrcode_status until a terminal state is reached.

    Args:
        api: WechatApiClient instance
        qrcode: QR code string from request_qr_code
        timeout_ms: Total polling timeout (default 8 minutes)
        signal: asyncio.Event to abort early
        on_verify_code: Called when server requests verify code; should return the code string
        on_status: Called on each status update for logging
        on_qr_refresh: Called when QR has expired and a new one has been fetched
    """
    deadline = asyncio.get_event_loop().time() + (timeout_ms / 1000)
    pending_verify_code: Optional[str] = None
    refresh_count = 0
    current_qrcode = qrcode

    while True:
        loop = asyncio.get_event_loop()
        remaining = deadline - loop.time()
        if remaining <= 0:
            return LoginResult(connected=False, message="Login timed out")

        if signal is not None and signal.is_set():
            return LoginResult(connected=False, message="aborted")

        try:
            status_resp = await api.get_qrcode_status(
                qrcode=current_qrcode,
                verify_code=pending_verify_code,
                timeout_ms=35_000,
            )
        except Exception:
            # Network/gateway error — treat as "wait", keep polling
            await asyncio.sleep(1.0)
            continue

        s: str = status_resp.status
        await on_status(
            s,
            {
                "botId": status_resp.ilink_bot_id,
                "hasBotToken": bool(status_resp.bot_token),
            },
        ) if on_status else None

        if s == "wait":
            await asyncio.sleep(1.0)
            continue

        elif s == "scaned":
            pending_verify_code = None
            await asyncio.sleep(1.0)
            continue

        elif s == "need_verifycode":
            if not on_verify_code:
                return LoginResult(connected=False, message="Server requested verify code but no handler provided")
            prompt = "You entered the wrong code. Please retry:" if pending_verify_code else "Enter the 6-digit code shown on WeChat:"
            code = await on_verify_code(prompt)
            pending_verify_code = code.strip()
            # continue immediately, no 1s sleep
            continue

        elif s in ("expired", "verify_code_blocked"):
            refresh_count += 1
            MAX_QR_REFRESH = 3
            if refresh_count > MAX_QR_REFRESH:
                return LoginResult(connected=False, message=f"QR expired {MAX_QR_REFRESH} times. Please retry later.")
            refreshed = await api.get_bot_qrcode(bot_type="3")
            current_qrcode = refreshed.qrcode
            pending_verify_code = None
            await on_qr_refresh(refreshed.qrcode_img_content) if on_qr_refresh else None
            await asyncio.sleep(1.0)
            continue

        elif s == "binded_redirect":
            return LoginResult(connected=False, already_connected=True, message="Already connected to this OpenClaw instance.")

        elif s == "scaned_but_redirect":
            if status_resp.redirect_host:
                return LoginResult(connected=False, message=f"IDC redirect required to {status_resp.redirect_host}. Please re-run login.")
            await asyncio.sleep(1.0)
            continue

        elif s == "confirmed":
            if not status_resp.ilink_bot_id:
                return LoginResult(connected=False, message="Login confirmed but ilink_bot_id missing")
            return LoginResult(
                connected=True,
                bot_token=status_resp.bot_token,
                account_id=status_resp.ilink_bot_id,
                base_url=status_resp.baseurl or api.base_url,
                user_id=status_resp.ilink_user_id,
                message="Login confirmed.",
            )

        # Unknown status — wait and retry
        await asyncio.sleep(1.0)


# ---------------------------------------------------------------------------
# Top-level login wrapper
# ---------------------------------------------------------------------------


async def run_login_flow(
    api: WechatApiClient,
    *,
    bot_type: str = "3",
    timeout_ms: int = 480_000,
    on_qr_code: Optional[Callable[[str], Any]] = None,
    on_verify_code: Optional[Callable[[str], Any]] = None,
    on_qr_refresh: Optional[Callable[[str], Any]] = None,
    on_status: Optional[Callable[[str, Optional[dict]], Any]] = None,
    signal: Optional[asyncio.Event] = None,
) -> LoginResult:
    """
    Full login flow: request QR → notify caller → poll status.

    Args:
        api: WechatApiClient instance
        bot_type: Bot type (default "3")
        timeout_ms: Total login timeout (default 8 minutes)
        on_qr_code: Called with ``qrcode_img_content`` string when QR is ready
        on_verify_code: Called when verify code is needed; should return the code string
        on_qr_refresh: Called when QR is refreshed; receives new ``qrcode_img_content``
        on_status: Called on each status update
        signal: asyncio.Event to abort early
    """
    try:
        qr = await request_qr_code(api, bot_type=bot_type)
    except Exception:
        return LoginResult(connected=False, message="Failed to fetch QR code")

    await on_qr_code(qr.qrcode_img_content) if on_qr_code else None

    return await poll_qr_login(
        api,
        qrcode=qr.qrcode,
        timeout_ms=timeout_ms,
        signal=signal,
        on_verify_code=on_verify_code,
        on_status=on_status,
        on_qr_refresh=on_qr_refresh,
    )


def new_session_key() -> str:
    """Generate a sessionKey suitable for the login flow."""
    return str(uuid.uuid4())
