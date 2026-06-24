"""CDN upload / download + AES-128-ECB encryption — mirrors src/wechat/media.ts."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import httpx

from wechat.api import WechatApiClient
from wechat.crypto import (
    aes_ecb_padded_size,
    aes_key_hex_to_base64,
    aes_key_hex_to_buffer,
    decrypt_aes_ecb,
    encrypt_aes_ecb,
    generate_aes_key,
    generate_filekey,
    md5_hex,
    parse_aes_key,
)
from wechat.types import GetUploadUrlReq, UploadedFileInfo, UploadMediaType

UPLOAD_MAX_RETRIES = 3

# ---------------------------------------------------------------------------
# CDN URL builders
# ---------------------------------------------------------------------------


def build_cdn_upload_url(
    cdn_base_url: str, upload_param: str, filekey: str
) -> str:
    return (
        f"{cdn_base_url}/upload"
        f"?encrypted_query_param={upload_param}"
        f"&filekey={filekey}"
    )


def build_cdn_download_url(cdn_base_url: str, encrypted_query_param: str) -> str:
    return (
        f"{cdn_base_url}/download"
        f"?encrypted_query_param={encrypted_query_param}"
    )


# ---------------------------------------------------------------------------
# MIME helpers
# ---------------------------------------------------------------------------

_EXT_TO_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".zip": "application/zip",
}


def get_mime_from_filename(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    return _EXT_TO_MIME.get(ext, "application/octet-stream")


# ---------------------------------------------------------------------------
# Upload pipeline
# ---------------------------------------------------------------------------


async def upload_buffer_to_cdn(
    *,
    cdn_base_url: str,
    buf: bytes,
    upload_full_url: Optional[str] = None,
    upload_param: Optional[str] = None,
    filekey: str,
    aeskey: bytes,
    label: str = "uploadBufferToCdn",
) -> dict:
    """
    Encrypt ``buf`` with AES-128-ECB and POST to CDN.

    Returns ``{"download_param": str}`` — the ``x-encrypted-param`` from the
    CDN response headers.

    Raises:
        RuntimeError: if CDN response is missing ``x-encrypted-param`` header.
    """
    ciphertext = encrypt_aes_ecb(buf, aeskey)

    if upload_full_url and upload_full_url.strip():
        cdn_url = upload_full_url.strip()
    elif upload_param:
        cdn_url = build_cdn_upload_url(cdn_base_url, upload_param, filekey)
    else:
        raise RuntimeError(
            f"{label}: CDN upload URL missing (need upload_full_url or upload_param)"
        )

    download_param: Optional[str] = None
    last_error: Optional[Exception] = None

    for attempt in range(1, UPLOAD_MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
                response = await client.post(
                    cdn_url,
                    content=ciphertext,
                    headers={"Content-Type": "application/octet-stream"},
                )

            if response.status_code >= 400 and response.status_code < 500:
                err_msg = response.headers.get("x-error-message") or response.text
                raise RuntimeError(f"CDN upload client error {response.status_code}: {err_msg}")

            if response.status_code != 200:
                err_msg = response.headers.get("x-error-message") or f"status {response.status_code}"
                raise RuntimeError(f"CDN upload server error: {err_msg}")

            download_param = response.headers.get("x-encrypted-param") or None
            if not download_param:
                raise RuntimeError("CDN upload response missing x-encrypted-param header")

            break  # success

        except Exception as exc:
            last_error = exc
            # Don't retry client errors (4xx)
            if isinstance(exc, RuntimeError) and "client error" in str(exc):
                raise
            if attempt < UPLOAD_MAX_RETRIES:
                continue

    if not download_param:
        raise RuntimeError(
            f"CDN upload failed after {UPLOAD_MAX_RETRIES} attempts: {last_error}"
        )

    return {"download_param": download_param}


async def upload_file(
    api: WechatApiClient,
    file_path: str,
    to_user_id: str,
    media_type: int,
    *,
    no_need_thumb: bool = True,
    label: str = "uploadFile",
) -> UploadedFileInfo:
    """
    Upload a local file to the CDN with AES-128-ECB encryption.

    Returns UploadedFileInfo needed for ``sendMessage``.
    """
    # Read file
    with open(file_path, "rb") as f:
        plaintext = f.read()

    rawsize = len(plaintext)
    rawfilemd5 = md5_hex(plaintext)
    filesize = aes_ecb_padded_size(rawsize)
    filekey = generate_filekey()
    aeskey = generate_aes_key()

    # Get upload URL from ilink
    upload_url_resp = await api.get_upload_url(
        GetUploadUrlReq(
            filekey=filekey,
            media_type=media_type,
            to_user_id=to_user_id,
            rawsize=rawsize,
            rawfilemd5=rawfilemd5,
            filesize=filesize,
            no_need_thumb=no_need_thumb,
            aeskey=aeskey.hex(),  # hex string, NOT aesKeyHexToBase64 here
        )
    )

    upload_full_url = upload_url_resp.upload_full_url
    upload_param = upload_url_resp.upload_param

    if not upload_full_url and not upload_param:
        raise RuntimeError(f"{label}: getUploadUrl returned no upload URL")

    # Upload to CDN
    result = await upload_buffer_to_cdn(
        cdn_base_url=api.cdn_base_url,
        buf=plaintext,
        upload_full_url=upload_full_url,
        upload_param=upload_param,
        filekey=filekey,
        aeskey=aeskey,
        label=f"{label}[filekey={filekey}]",
    )

    return UploadedFileInfo(
        filekey=filekey,
        download_encrypted_query_param=result["download_param"],
        aeskey=aeskey.hex(),  # hex-encoded raw 16-byte key
        file_size=rawsize,
        file_size_ciphertext=filesize,
    )


async def upload_image(
    api: WechatApiClient, file_path: str, to_user_id: str
) -> UploadedFileInfo:
    return upload_file(api, file_path, to_user_id, UploadMediaType.IMAGE)


async def upload_video(
    api: WechatApiClient, file_path: str, to_user_id: str
) -> UploadedFileInfo:
    return upload_file(api, file_path, to_user_id, UploadMediaType.VIDEO)


async def upload_attachment(
    api: WechatApiClient, file_path: str, to_user_id: str
) -> UploadedFileInfo:
    return upload_file(api, file_path, to_user_id, UploadMediaType.FILE)


# ---------------------------------------------------------------------------
# Download pipeline
# ---------------------------------------------------------------------------


async def download_and_decrypt_cdn(
    *,
    cdn_base_url: str,
    encrypted_query_param: str,
    aes_key_base64: Optional[str] = None,
    aes_key_hex: Optional[str] = None,
    full_url: Optional[str] = None,
    label: str = "download",
) -> bytes:
    """
    Download (and decrypt) a CDN media file referenced by encrypt_query_param.

    Returns the plaintext buffer.

    One of ``aes_key_base64`` or ``aes_key_hex`` must be provided.
    """
    if aes_key_hex:
        key = aes_key_hex_to_buffer(aes_key_hex)
    elif aes_key_base64:
        key = parse_aes_key(aes_key_base64)
    else:
        raise ValueError(f"{label}: neither aes_key_hex nor aes_key_base64 provided")

    url = full_url or build_cdn_download_url(cdn_base_url, encrypted_query_param)

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        response = await client.get(url)

    if not response.is_success:
        body_text = response.text[:200] if hasattr(response, "text") else "(unreadable)"
        raise RuntimeError(
            f"{label}: CDN {response.status_code} {getattr(response, 'reason_phrase', '')} body={body_text}"
        )

    ciphertext = response.content
    return decrypt_aes_ecb(ciphertext, key)


# ---------------------------------------------------------------------------
# Save inbound media to disk
# ---------------------------------------------------------------------------


async def save_inbound_media(
    *, dest_dir: str, filename: str, buf: bytes
) -> str:
    """Save decrypted inbound media buffer to disk."""
    dest_path = Path(dest_dir)
    dest_path.mkdir(parents=True, exist_ok=True)
    file_path = dest_path / filename
    file_path.write_bytes(buf)
    return str(file_path)
