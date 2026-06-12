"""Media download/upload helpers for WeChat addon."""
from __future__ import annotations

import base64
import hashlib
import logging
import mimetypes
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import api
from .types import (
    CDNMedia, UploadMediaType, MessageItemType,
    ImageItem, VoiceItem, FileItem, VideoItem, MessageItem,
)


@dataclass
class UploadedMediaInfo:
    """The full result of a CDN upload, carrying everything sendMessage needs.

    `upload_media` returns this so non-image media (video, file) can populate
    their type-specific size fields — OpenClaw sets `video_item.video_size` to
    the ciphertext byte count and `file_item.len` to the plaintext byte count,
    in addition to `image_item.mid_size`. Without those, the WeChat client may
    accept the message but fail to render or download the attachment.
    """

    cdn_media: CDNMedia
    media_type: UploadMediaType
    raw_size: int          # plaintext byte count
    ciphertext_size: int   # AES-128-ECB + PKCS#7 byte count
    filekey: str

log = logging.getLogger(__name__)

# Extension → UploadMediaType mapping
_UPLOAD_TYPE_MAP = {
    ".jpg": UploadMediaType.IMAGE,
    ".jpeg": UploadMediaType.IMAGE,
    ".png": UploadMediaType.IMAGE,
    ".gif": UploadMediaType.IMAGE,
    ".webp": UploadMediaType.IMAGE,
    ".bmp": UploadMediaType.IMAGE,
    ".mp4": UploadMediaType.VIDEO,
    ".avi": UploadMediaType.VIDEO,
    ".mov": UploadMediaType.VIDEO,
    ".mkv": UploadMediaType.VIDEO,
    ".wav": UploadMediaType.VOICE,
    ".mp3": UploadMediaType.VOICE,
    ".ogg": UploadMediaType.VOICE,
    ".silk": UploadMediaType.VOICE,
    ".amr": UploadMediaType.VOICE,
}

# UploadMediaType → MessageItemType mapping
_ITEM_TYPE_MAP = {
    UploadMediaType.IMAGE: MessageItemType.IMAGE,
    UploadMediaType.VIDEO: MessageItemType.VIDEO,
    UploadMediaType.VOICE: MessageItemType.VOICE,
    UploadMediaType.FILE: MessageItemType.FILE,
}


async def download_media(
    cdn_media: CDNMedia,
    dest_dir: str | Path,
    filename: str = "media",
) -> str:
    """Download media from CDN. Returns local file path."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    url = cdn_media.full_url
    if not url:
        raise ValueError("CDN media has no full_url")

    dest_path = dest_dir / filename
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=60.0)
        resp.raise_for_status()
        dest_path.write_bytes(resp.content)

    return str(dest_path)


def decode_voice(silk_path: str | Path, out_path: str | Path) -> str:
    """Decode Silk audio to WAV. Returns output path.

    Requires the `pilk` package: pip install pilk
    """
    try:
        import pilk
    except ImportError:
        log.warning("pilk not installed — cannot decode Silk voice. pip install pilk")
        return str(silk_path)

    silk_path = str(silk_path)
    out_path = str(out_path)
    pilk.decode(silk_path, out_path)
    return out_path


def detect_upload_type(file_path: str | Path) -> UploadMediaType:
    """Detect UploadMediaType from file extension. Defaults to FILE."""
    ext = Path(file_path).suffix.lower()
    return _UPLOAD_TYPE_MAP.get(ext, UploadMediaType.FILE)


def _pkcs7_pad(data: bytes, block: int = 16) -> bytes:
    pad = block - (len(data) % block)
    return data + bytes([pad]) * pad


def _aes128_ecb_encrypt(data: bytes, key: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    enc = cipher.encryptor()
    return enc.update(_pkcs7_pad(data, 16)) + enc.finalize()


def _encrypted_size(raw_size: int, block: int = 16) -> int:
    # PKCS#7 always appends at least one block when already aligned.
    return raw_size + (block - (raw_size % block))


async def upload_media(
    file_path: str | Path,
    base_url: str,
    token: str,
    to_user_id: str,
) -> UploadedMediaInfo:
    """Upload a file to WeChat CDN.

    Returns an UploadedMediaInfo carrying the CDNMedia reference plus the
    raw/ciphertext sizes and filekey that non-image media (video, file)
    need to populate their type-specific size fields downstream.

    Mirrors Hermes/OpenClaw: iLink expects getuploadurl to receive raw size,
    raw MD5, AES key, and padded ciphertext size; the CDN upload body is
    AES-128-ECB encrypted with PKCS#7 padding and posted with HTTP POST
    (not PUT); sendMessage then references encrypt_query_param + aes_key +
    encrypt_type=1. Earlier versions of this addon used plaintext PUT, which
    iLink would accept (HTTP 200) but produce an image the WeChat client
    could not decrypt/open.

    The final download parameter MUST come from the CDN's `x-encrypted-param`
    response header (or, as a documented fallback, a JSON body containing
    `encrypt_query_param` / `download_param`). Falling back to the
    pre-upload `upload_param` or to the locally-generated `filekey` would
    silently recreate the prior "sendMessage returns ok but WeChat client
    can't open the image" false-positive — those values are NOT the
    download parameter and the WeChat client cannot decrypt the payload
    with them. So we raise instead.
    """
    file_path = Path(file_path)
    if not file_path.is_file():
        raise FileNotFoundError(f"File not found: {file_path}")

    data = file_path.read_bytes()
    md5 = hashlib.md5(data).hexdigest()
    media_type = detect_upload_type(file_path)
    aeskey = secrets.token_bytes(16)
    aeskey_hex = aeskey.hex()
    ciphertext = _aes128_ecb_encrypt(data, aeskey)
    filekey = secrets.token_hex(16)

    # Get upload URL. filesize is ciphertext size, rawsize is plaintext size.
    # Hermes/OpenClaw include filekey + no_need_thumb; without filekey iLink
    # may return HTTP 200 but omit upload_full_url.
    upload_resp = await api.get_upload_url(
        base_url, token,
        media_type=int(media_type),
        to_user_id=to_user_id,
        rawsize=len(data),
        rawfilemd5=md5,
        filesize=len(ciphertext),
        aeskey=aeskey_hex,
        filekey=filekey,
        no_need_thumb=True,
    )

    upload_url = upload_resp.upload_full_url
    if not upload_url:
        raise RuntimeError("Server did not return an upload URL")

    # Upload encrypted bytes to CDN. OpenClaw uses POST (not PUT) and reads
    # the final download encrypted_query_param from the x-encrypted-param
    # response header. Some CDN responses also embed it in a JSON body.
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            upload_url,
            content=ciphertext,
            headers={"Content-Type": "application/octet-stream"},
            timeout=120.0,
        )
        resp.raise_for_status()
        raw = resp.text

    header_param = resp.headers.get("x-encrypted-param")
    json_param: str | None = None
    if raw and raw.strip().startswith("{"):
        try:
            body = resp.json()
            json_param = (
                body.get("encrypt_query_param")
                or body.get("download_param")
            )
        except Exception:
            json_param = None

    download_param = header_param or json_param
    if not download_param:
        # Be strict here. The prior code fell back to upload_param/filekey,
        # which made the function appear to succeed while producing media
        # the WeChat client could not open. Better to raise loudly.
        raise RuntimeError(
            "CDN upload response missing x-encrypted-param header and "
            "encrypt_query_param / download_param JSON field. The upload "
            "may have failed silently — refusing to return media reference."
        )

    cdn_media = CDNMedia(
        encrypt_query_param=download_param,
        # OpenClaw sends media.aes_key as base64(32-char hex string), not
        # base64(raw 16 bytes). The WeChat client decrypts the CDN payload
        # using this exact form; the raw-bytes form looks valid but renders
        # as an un-openable image.
        aes_key=base64.b64encode(aeskey_hex.encode("ascii")).decode("ascii"),
        encrypt_type=1,
    )
    return UploadedMediaInfo(
        cdn_media=cdn_media,
        media_type=media_type,
        raw_size=len(data),
        ciphertext_size=len(ciphertext),
        filekey=filekey,
    )


def make_media_item(info: UploadedMediaInfo, file_path: Path) -> MessageItem:
    """Create a MessageItem for sending uploaded media.

    For each media type, sets the OpenClaw/Hermes size fields that the
    WeChat client requires to render/download the attachment:

    - image: ``image_item.mid_size = ciphertext byte count``
    - video: ``video_item.video_size = ciphertext byte count``
    - file:  ``file_item.len = str(plaintext byte count)``
    - voice: no extra size field documented in OpenClaw outbound; the
      iLink schema does include ``playtime`` and ``encode_type`` if you
      have them, but outbound voice send is not part of the validated
      path. The MessageItem still carries the encrypted CDN reference,
      so a downstream client with looser validation may render it.
    """
    item_type = _ITEM_TYPE_MAP.get(info.media_type, MessageItemType.FILE)
    item = MessageItem(type=int(item_type))
    if item_type == MessageItemType.IMAGE:
        item.image_item = ImageItem(
            media=info.cdn_media,
            mid_size=info.ciphertext_size,
        )
    elif item_type == MessageItemType.VIDEO:
        item.video_item = VideoItem(
            media=info.cdn_media,
            video_size=info.ciphertext_size,
        )
    elif item_type == MessageItemType.VOICE:
        item.voice_item = VoiceItem(media=info.cdn_media)
    else:
        item.file_item = FileItem(
            media=info.cdn_media,
            file_name=file_path.name,
            len=str(info.raw_size),
        )

    return item
