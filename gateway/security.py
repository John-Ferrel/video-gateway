"""Authentication, upload validation, content tokens and rate limiting."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import ipaddress
import json
import time
from collections import defaultdict, deque
from datetime import UTC, datetime
from threading import Lock
from urllib.parse import urlparse

from fastapi import HTTPException, Request, status
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_IMAGE_SIDE = 4096
MIN_IMAGE_SIDE = 64
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}


def _clean_key(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    return value or None


def extract_api_key(request: Request) -> str | None:
    key = _clean_key(request.headers.get("authorization"))
    if key:
        return key
    key = _clean_key(request.headers.get("x-api-key"))
    return key


class Auth:
    def __init__(self, settings):
        self.settings = settings

    def key_owner(self, request: Request) -> int | None:
        """Return the stable key slot, or None for an invalid/missing key.

        The slot is the position in ``API_KEYS`` rather than a hash of the key.
        That lets the operator replace a key value without losing that key's
        library. Keep existing keys in place and append new keys.
        """
        key = extract_api_key(request)
        if not key:
            return None
        matched: int | None = None
        for index, valid in enumerate(self.settings.api_keys):
            if hmac.compare_digest(key, valid):
                matched = index
        return matched

    def require(self, request: Request) -> str:
        key = extract_api_key(request)
        if self.key_owner(request) is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "error": {
                        "message": "Invalid or missing API key. Send Authorization: Bearer <key>.",
                        "type": "authentication_error",
                        "code": "invalid_api_key",
                    }
                },
                headers={"WWW-Authenticate": "Bearer"},
            )
        return key or ""

    def check_content_token(self, request: Request, video_id: str, purpose: str | None = None) -> bool:
        """Validate a capability token only; API-key media access is owner-checked."""
        token = request.query_params.get("token")
        return bool(token and verify_content_token(self.settings, video_id, token, purpose=purpose))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def make_content_token(settings, video_id: str, *, purpose: str = "content", ttl: int | None = None) -> str:
    expires = int(time.time()) + int(ttl or settings.content_token_ttl_seconds)
    payload = _b64(json.dumps({"v": video_id, "p": purpose, "e": expires}, separators=(",", ":")).encode())
    signature = hmac.new(settings.content_secret, payload.encode(), hashlib.sha256).digest()
    return payload + "." + _b64(signature)


def verify_content_token(settings, video_id: str, token: str, *, purpose: str | None = None) -> bool:
    try:
        payload, signature = token.split(".", 1)
        expected = hmac.new(settings.content_secret, payload.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_unb64(signature), expected):
            return False
        data = json.loads(_unb64(payload))
        if data.get("v") != video_id:
            return False
        if purpose and data.get("p") != purpose:
            return False
        return int(data.get("e", 0)) >= int(time.time())
    except Exception:
        return False


class RateLimiter:
    """Small in-memory sliding window limiter, sufficient for a personal gateway."""

    def __init__(self, limit: int = 60, window_seconds: int = 60):
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, identity: str) -> None:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[identity]
            while hits and hits[0] < now - self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail={
                        "error": {
                            "message": "Too many requests; slow down.",
                            "type": "rate_limit_error",
                            "code": "rate_limited",
                        }
                    },
                )
            hits.append(now)


def validate_image_bytes(data: bytes, declared_mime: str | None = None) -> tuple[bytes, str, int, int]:
    """Validate and normalize an uploaded reference image.

    Re-encoding strips EXIF/metadata and guarantees the worker receives a
    decodable RGB image. Returns (jpeg_bytes, mime, width, height).
    """
    if not data:
        raise ValueError("empty image")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in ALLOWED_FORMATS:
                raise ValueError(f"unsupported image format {image.format!r}; use JPEG, PNG or WEBP")
            image.load()
            image = ImageOps.exif_transpose(image)
            width, height = image.size
            if width < MIN_IMAGE_SIDE or height < MIN_IMAGE_SIDE:
                raise ValueError(f"image is too small ({width}x{height}); minimum {MIN_IMAGE_SIDE}px")
            if width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE:
                raise ValueError(f"image is too large ({width}x{height}); maximum {MAX_IMAGE_SIDE}px")
            if image.mode not in ("RGB", "L") or image.mode == "L":
                image = image.convert("RGB")
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=92, optimize=True)
            return buffer.getvalue(), "image/jpeg", width, height
    except UnidentifiedImageError as exc:
        raise ValueError("file is not a valid image") from exc
    except Exception as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError(f"invalid image: {exc}") from exc


def fetch_remote_image(url: str, *, max_bytes: int, timeout: float = 20.0) -> bytes:
    """Fetch a remote reference with SSRF protections.

    Remote references are disabled by default. When enabled, only public
    http(s) hosts are accepted and redirects are followed manually so every
    hop is checked.
    """
    import httpx

    current = url
    for _ in range(4):
        parsed = urlparse(current)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("reference URL must be http(s)")
        host = parsed.hostname
        try:
            addresses = {info[4][0] for info in __import__("socket").getaddrinfo(host, None)}
        except OSError as exc:
            raise ValueError(f"cannot resolve reference host: {exc}") from exc
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                raise ValueError("reference URL resolves to a private address")
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            response = client.get(current)
            if response.is_redirect:
                current = str(response.headers.get("location", ""))
                continue
            response.raise_for_status()
            if len(response.content) > max_bytes:
                raise ValueError("reference image exceeds size limit")
            return response.content
    raise ValueError("too many redirects")


def parse_data_uri(value: str, *, max_bytes: int) -> tuple[bytes, str | None]:
    if not value.startswith("data:"):
        raise ValueError("not a data URI")
    header, _, payload = value.partition(",")
    if ";base64" not in header:
        raise ValueError("only base64 data URIs are supported")
    mime = header[5:].split(";", 1)[0] or None
    data = base64.b64decode(payload, validate=True)
    if len(data) > max_bytes:
        raise ValueError("data URI exceeds size limit")
    return data, mime


def utc_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
