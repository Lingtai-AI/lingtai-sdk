"""Tests for magic-byte validation of downloaded WeChat attachments.

WeChat attachments can arrive under normal-looking `.pdf` / `.zip` / `.jpg`
names while the saved bytes are encrypted / cache / private-container data
rather than a real exported file. The addon must detect the
extension-vs-content mismatch and surface a structured warning instead of
silently presenting the file as a normal attachment.

Ported from Lingtai-AI/lingtai-wechat#15 into the in-kernel WeChat addon.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from lingtai.mcp_servers.wechat import media


# ── Sample bytes ───────────────────────────────────────────────────────────

# A minimal-but-real PDF (starts with %PDF-).
VALID_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
# A minimal real ZIP (PK\x03\x04 local file header).
VALID_ZIP = b"PK\x03\x04" + b"\x00" * 26 + b"PK\x05\x06" + b"\x00" * 18
VALID_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
VALID_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 16
VALID_WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 16
VALID_GIF = b"GIF89a" + b"\x00" * 16
VALID_BMP = b"BM" + b"\x00" * 16

# An encrypted/cache-like header for a bogus ".zip" attachment.
ENCRYPTED_ZIP_LIKE = bytes.fromhex(
    "241f07f6bcab69005d87b05f3c095e27"
) + b"\x00" * 32
# A bogus ".pdf" header.
ENCRYPTED_PDF_LIKE = bytes.fromhex("bb1d453de956099e") + b"\x00" * 32


def _write(tmp_path: Path, name: str, data: bytes) -> str:
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


# ── Valid files validate as OK ─────────────────────────────────────────────

@pytest.mark.parametrize("name,data", [
    ("doc.pdf", VALID_PDF),
    ("archive.zip", VALID_ZIP),
    ("pic.png", VALID_PNG),
    ("pic.jpg", VALID_JPEG),
    ("pic.jpeg", VALID_JPEG),
    ("pic.webp", VALID_WEBP),
    ("pic.gif", VALID_GIF),
    ("pic.bmp", VALID_BMP),
])
def test_valid_files_report_ok(tmp_path, name, data):
    path = _write(tmp_path, name, data)
    result = media.validate_media_bytes(path)
    assert result.status == "ok", result
    assert result.warning is None
    assert result.hint is None


# ── Mismatched encrypted/cache bytes are flagged ───────────────────────────

def test_encrypted_zip_like_bytes_flagged(tmp_path):
    path = _write(tmp_path, "archive.zip", ENCRYPTED_ZIP_LIKE)
    result = media.validate_media_bytes(path)
    assert result.status == "mismatch"
    assert result.declared_ext == ".zip"
    # Warning + actionable recovery hint must be present.
    assert result.warning and "zip" in result.warning.lower()
    assert result.hint and "save as" in result.hint.lower()


def test_encrypted_pdf_like_bytes_flagged(tmp_path):
    path = _write(tmp_path, "report.pdf", ENCRYPTED_PDF_LIKE)
    result = media.validate_media_bytes(path)
    assert result.status == "mismatch"
    assert result.declared_ext == ".pdf"
    assert result.warning and "pdf" in result.warning.lower()
    assert result.hint


def test_non_image_bytes_under_jpg_flagged(tmp_path):
    path = _write(tmp_path, "screenshot.jpg", ENCRYPTED_PDF_LIKE)
    result = media.validate_media_bytes(path)
    assert result.status == "mismatch"
    assert result.declared_ext == ".jpg"


# ── Inbound IMAGE items use generated .jpg names, so validate as any image ──

@pytest.mark.parametrize("data", [VALID_JPEG, VALID_PNG, VALID_WEBP, VALID_GIF, VALID_BMP])
def test_validate_image_bytes_accepts_any_known_image_signature(tmp_path, data):
    path = _write(tmp_path, "generated.jpg", data)
    result = media.validate_image_bytes(path)
    assert result.status == "ok", result
    assert result.declared_ext == "image"
    assert result.render_suffix() == ""


def test_validate_image_bytes_flags_non_image_bytes(tmp_path):
    path = _write(tmp_path, "generated.jpg", ENCRYPTED_ZIP_LIKE)
    result = media.validate_image_bytes(path)
    assert result.status == "mismatch"
    assert result.declared_ext == "image"
    assert result.warning and "recognized image" in result.warning
    assert result.hint and "save as" in result.hint.lower()


# ── Extensions we don't have a signature for: unknown, never a false alarm ──

def test_unknown_extension_is_not_flagged(tmp_path):
    path = _write(tmp_path, "note.txt", b"hello world")
    result = media.validate_media_bytes(path)
    assert result.status == "unknown"
    assert result.warning is None


def test_no_extension_is_unknown(tmp_path):
    path = _write(tmp_path, "blob", ENCRYPTED_PDF_LIKE)
    result = media.validate_media_bytes(path)
    assert result.status == "unknown"


def test_missing_file_is_unknown_not_error(tmp_path):
    # A signature is known for .pdf, but the file does not exist: never raise,
    # never flag — report unknown so the download path can't break.
    result = media.validate_media_bytes(str(tmp_path / "absent.pdf"))
    assert result.status == "unknown"
    assert result.warning is None


# ── Cross-type confusion: a real PNG saved as .pdf is a mismatch ───────────

def test_real_png_saved_as_pdf_is_mismatch(tmp_path):
    path = _write(tmp_path, "fake.pdf", VALID_PNG)
    result = media.validate_media_bytes(path)
    assert result.status == "mismatch"


# ── A jpg signature variant (EXIF, \xff\xd8\xff\xe1) also validates ────────

def test_jpeg_exif_variant_ok(tmp_path):
    data = b"\xff\xd8\xff\xe1\x00\x10Exif" + b"\x00" * 16
    path = _write(tmp_path, "photo.jpg", data)
    result = media.validate_media_bytes(path)
    assert result.status == "ok"


# ── render_suffix(): a compact tag agents can read inline ──────────────────

def test_mismatch_render_suffix_contains_warning_and_hint(tmp_path):
    path = _write(tmp_path, "archive.zip", ENCRYPTED_ZIP_LIKE)
    result = media.validate_media_bytes(path)
    rendered = result.render_suffix()
    assert rendered  # non-empty for a mismatch
    assert "⚠" in rendered or "WARNING" in rendered.upper()
    assert result.hint in rendered


def test_ok_render_suffix_is_empty(tmp_path):
    path = _write(tmp_path, "doc.pdf", VALID_PDF)
    result = media.validate_media_bytes(path)
    assert result.render_suffix() == ""
