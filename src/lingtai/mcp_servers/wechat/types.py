"""iLink Bot protocol types — mirrors openclaw-weixin's type definitions."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class MessageItemType(IntEnum):
    NONE = 0
    TEXT = 1
    IMAGE = 2
    VOICE = 3
    FILE = 4
    VIDEO = 5


class UploadMediaType(IntEnum):
    IMAGE = 1
    VIDEO = 2
    FILE = 3
    VOICE = 4


class MessageState(IntEnum):
    NEW = 0
    GENERATING = 1
    FINISH = 2


@dataclass
class CDNMedia:
    encrypt_query_param: str | None = None
    aes_key: str | None = None
    encrypt_type: int | None = None
    full_url: str | None = None


@dataclass
class TextItem:
    text: str | None = None


@dataclass
class ImageItem:
    media: CDNMedia | None = None
    thumb_media: CDNMedia | None = None
    aeskey: str | None = None
    url: str | None = None
    mid_size: int | None = None
    thumb_size: int | None = None
    thumb_height: int | None = None
    thumb_width: int | None = None
    hd_size: int | None = None


@dataclass
class VoiceItem:
    media: CDNMedia | None = None
    encode_type: int | None = None
    playtime: int | None = None
    text: str | None = None  # server-side transcription


@dataclass
class FileItem:
    media: CDNMedia | None = None
    file_name: str | None = None
    md5: str | None = None
    len: str | None = None


@dataclass
class VideoItem:
    media: CDNMedia | None = None
    video_size: int | None = None
    play_length: int | None = None
    thumb_media: CDNMedia | None = None


@dataclass
class MessageItem:
    type: int | None = None
    create_time_ms: int | None = None
    update_time_ms: int | None = None
    is_completed: bool | None = None
    msg_id: str | None = None
    text_item: TextItem | None = None
    image_item: ImageItem | None = None
    voice_item: VoiceItem | None = None
    file_item: FileItem | None = None
    video_item: VideoItem | None = None


@dataclass
class WeixinMessage:
    seq: int | None = None
    message_id: int | None = None
    from_user_id: str | None = None
    to_user_id: str | None = None
    client_id: str | None = None
    create_time_ms: int | None = None
    update_time_ms: int | None = None
    session_id: str | None = None
    group_id: str | None = None
    message_type: int | None = None
    message_state: int | None = None
    item_list: list[MessageItem] = field(default_factory=list)
    context_token: str | None = None


@dataclass
class BaseInfo:
    channel_version: str | None = None


@dataclass
class GetUpdatesReq:
    get_updates_buf: str = ""
    base_info: BaseInfo | None = None


@dataclass
class GetUpdatesResp:
    ret: int | None = None
    errcode: int | None = None
    errmsg: str | None = None
    msgs: list[WeixinMessage] = field(default_factory=list)
    get_updates_buf: str | None = None
    longpolling_timeout_ms: int | None = None


@dataclass
class SendMessageReq:
    msg: WeixinMessage | None = None


@dataclass
class GetUploadUrlReq:
    filekey: str | None = None
    media_type: int | None = None
    to_user_id: str | None = None
    rawsize: int | None = None
    rawfilemd5: str | None = None
    filesize: int | None = None
    aeskey: str | None = None


@dataclass
class GetUploadUrlResp:
    upload_param: str | None = None
    upload_full_url: str | None = None


@dataclass
class GetConfigResp:
    ret: int | None = None
    errmsg: str | None = None
    typing_ticket: str | None = None


def msg_from_dict(d: dict) -> WeixinMessage:
    """Parse a WeixinMessage from a JSON dict (getUpdates response)."""
    items = []
    for raw_item in d.get("item_list", []):
        item = MessageItem(
            type=raw_item.get("type"),
            create_time_ms=raw_item.get("create_time_ms"),
            update_time_ms=raw_item.get("update_time_ms"),
            is_completed=raw_item.get("is_completed"),
            msg_id=raw_item.get("msg_id"),
        )
        if "text_item" in raw_item:
            item.text_item = TextItem(**raw_item["text_item"])
        if "image_item" in raw_item:
            img = raw_item["image_item"]
            item.image_item = ImageItem(
                media=CDNMedia(**img["media"]) if "media" in img else None,
                thumb_media=CDNMedia(**img["thumb_media"]) if "thumb_media" in img else None,
                aeskey=img.get("aeskey"),
                url=img.get("url"),
                mid_size=img.get("mid_size"),
                thumb_size=img.get("thumb_size"),
                thumb_height=img.get("thumb_height"),
                thumb_width=img.get("thumb_width"),
                hd_size=img.get("hd_size"),
            )
        if "voice_item" in raw_item:
            v = raw_item["voice_item"]
            item.voice_item = VoiceItem(
                media=CDNMedia(**v["media"]) if "media" in v else None,
                encode_type=v.get("encode_type"),
                playtime=v.get("playtime"),
                text=v.get("text"),
            )
        if "file_item" in raw_item:
            f = raw_item["file_item"]
            item.file_item = FileItem(
                media=CDNMedia(**f["media"]) if "media" in f else None,
                file_name=f.get("file_name"),
                md5=f.get("md5"),
                len=f.get("len"),
            )
        if "video_item" in raw_item:
            vid = raw_item["video_item"]
            item.video_item = VideoItem(
                media=CDNMedia(**vid["media"]) if "media" in vid else None,
                video_size=vid.get("video_size"),
                play_length=vid.get("play_length"),
                thumb_media=CDNMedia(**vid["thumb_media"]) if "thumb_media" in vid else None,
            )
        items.append(item)

    return WeixinMessage(
        seq=d.get("seq"),
        message_id=d.get("message_id"),
        from_user_id=d.get("from_user_id"),
        to_user_id=d.get("to_user_id"),
        client_id=d.get("client_id"),
        create_time_ms=d.get("create_time_ms"),
        update_time_ms=d.get("update_time_ms"),
        session_id=d.get("session_id"),
        group_id=d.get("group_id"),
        message_type=d.get("message_type"),
        message_state=d.get("message_state"),
        item_list=items,
        context_token=d.get("context_token"),
    )


def msg_to_dict(msg: WeixinMessage) -> dict:
    """Serialize a WeixinMessage to a JSON-compatible dict for sendMessage."""
    d: dict = {}
    for fld in ("seq", "message_id", "from_user_id", "to_user_id",
                "client_id", "create_time_ms", "update_time_ms",
                "session_id", "group_id", "message_type",
                "message_state", "context_token"):
        val = getattr(msg, fld, None)
        if val is not None:
            d[fld] = val

    if msg.item_list:
        items = []
        for item in msg.item_list:
            raw: dict = {}
            if item.type is not None:
                raw["type"] = item.type
            if item.text_item and item.text_item.text is not None:
                raw["text_item"] = {"text": item.text_item.text}
            # Image messages require mid_size (ciphertext byte count) for the
            # WeChat client to render; OpenClaw/Hermes both set it. Other media
            # types are serialized minimally for sends.
            if item.image_item and item.image_item.media:
                img: dict = {"media": _cdn_to_dict(item.image_item.media)}
                for fld in ("thumb_media", "aeskey", "url", "mid_size",
                            "thumb_size", "thumb_height", "thumb_width", "hd_size"):
                    val = getattr(item.image_item, fld, None)
                    if val is not None:
                        img[fld] = _cdn_to_dict(val) if fld == "thumb_media" else val
                raw["image_item"] = img
            if item.voice_item and item.voice_item.media:
                v: dict = {"media": _cdn_to_dict(item.voice_item.media)}
                for fld in ("encode_type", "playtime"):
                    val = getattr(item.voice_item, fld, None)
                    if val is not None:
                        v[fld] = val
                raw["voice_item"] = v
            if item.file_item and item.file_item.media:
                # OpenClaw sets file_item.len = str(plaintext size). Without
                # it, the WeChat client may accept the message but fail to
                # render or trigger the download dialog.
                f: dict = {"media": _cdn_to_dict(item.file_item.media)}
                for fld in ("file_name", "md5", "len"):
                    val = getattr(item.file_item, fld, None)
                    if val is not None:
                        f[fld] = val
                raw["file_item"] = f
            if item.video_item and item.video_item.media:
                # OpenClaw sets video_item.video_size = ciphertext byte count.
                # Without it, sendMessage returns ok but the client cannot
                # render the video preview/playback control.
                vd: dict = {"media": _cdn_to_dict(item.video_item.media)}
                for fld in ("video_size", "play_length"):
                    val = getattr(item.video_item, fld, None)
                    if val is not None:
                        vd[fld] = val
                if item.video_item.thumb_media is not None:
                    vd["thumb_media"] = _cdn_to_dict(item.video_item.thumb_media)
                raw["video_item"] = vd
            items.append(raw)
        d["item_list"] = items

    return d


def _cdn_to_dict(cdn: CDNMedia) -> dict:
    d: dict = {}
    for fld in ("encrypt_query_param", "aes_key", "encrypt_type", "full_url"):
        val = getattr(cdn, fld, None)
        if val is not None:
            d[fld] = val
    return d
