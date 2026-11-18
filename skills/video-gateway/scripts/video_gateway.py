#!/usr/bin/env python3
"""Minimal, dependency-free client for the Video Gateway HTTP API.

Environment:
  VIDEO_GATEWAY_BASE_URL        required, e.g. https://video.example.com
  VIDEO_GATEWAY_API_KEY         required, the gateway Bearer key
  VIDEO_GATEWAY_API_KEY_FILE    optional, read the key from a file instead
  CF_ACCESS_CLIENT_ID           optional Cloudflare Access service token id
  CF_ACCESS_CLIENT_SECRET       optional Cloudflare Access service token secret

The client never retries a generation automatically. A retry is a new
billable job and must be an explicit user decision.

Examples:
  video_gateway.py models
  video_gateway.py enhance --model fasth3-v2 --prompt "..." --image first.png
  video_gateway.py create --prompt "..." --image first.png --wait --download-dir out
  video_gateway.py status --id video_...
  video_gateway.py cancel --id video_...
  video_gateway.py download --id video_... --out clip.mp4
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import secrets
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

TERMINAL_STATES = {"completed", "failed", "cancelled", "expired"}
IMAGE_LIMIT = 12 * 1024 * 1024
VIDEO_LIMIT = 32 * 1024 * 1024
AUDIO_LIMIT = 16 * 1024 * 1024


class GatewayError(Exception):
    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface redirects instead of silently following an Access login page."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, D102
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _die(message: str, code: int = 1) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(code)


def _base_url() -> str:
    value = os.environ.get("VIDEO_GATEWAY_BASE_URL", "").strip().rstrip("/")
    if not value:
        _die("VIDEO_GATEWAY_BASE_URL is not set", 2)
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        _die("VIDEO_GATEWAY_BASE_URL must be an http(s) URL", 2)
    return value


def _api_key() -> str:
    key = os.environ.get("VIDEO_GATEWAY_API_KEY", "").strip()
    if not key:
        path = os.environ.get("VIDEO_GATEWAY_API_KEY_FILE", "").strip()
        if path:
            try:
                key = Path(path).read_text(encoding="utf-8").strip()
            except OSError as exc:
                _die(f"cannot read VIDEO_GATEWAY_API_KEY_FILE: {exc}", 2)
    if not key:
        _die("VIDEO_GATEWAY_API_KEY is not set", 2)
    return key


def _headers() -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Accept": "application/json",
        "User-Agent": "video-gateway-skill/1.0",
    }
    access_id = os.environ.get("CF_ACCESS_CLIENT_ID", "").strip()
    access_secret = os.environ.get("CF_ACCESS_CLIENT_SECRET", "").strip()
    if bool(access_id) != bool(access_secret):
        _die("set both CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET, or neither", 2)
    if access_id:
        headers["CF-Access-Client-Id"] = access_id
        headers["CF-Access-Client-Secret"] = access_secret
    return headers


def _error_from_body(body: bytes) -> tuple[str, str | None]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body.decode("utf-8", "replace").strip()[:300] or "request failed", None
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or "request failed"), error.get("code")
    if isinstance(error, str):
        return error, None
    return str(payload)[:300], None


def request(
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    content_type: str | None = None,
    timeout: float = 180.0,
) -> tuple[dict[str, Any], dict[str, str]]:
    url = _base_url() + path
    headers = _headers()
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as response:
            raw = response.read()
            response_headers = dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        message, code = _error_from_body(raw)
        if exc.code in {301, 302, 303, 307, 308}:
            message = (
                "Cloudflare Access intercepted the request with a login redirect. "
                "Set CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET, or open an Access session."
            )
            code = "access_redirect"
        raise GatewayError(message, status=exc.code, code=code) from exc
    except urllib.error.URLError as exc:
        raise GatewayError(f"cannot reach {_base_url()}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise GatewayError(f"request timed out after {timeout:g}s") from exc
    if not raw:
        return {}, response_headers
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GatewayError("server returned a non-JSON response") from exc
    if not isinstance(payload, dict):
        raise GatewayError("server returned an unexpected response")
    return payload, response_headers


def _json_request(method: str, path: str, payload: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    data, _ = request(method, path, body=body, content_type="application/json" if body else None, timeout=timeout)
    return data


# --------------------------------------------------------------------- multipart
def _quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ").replace("\n", " ")


def _field_part(boundary: str, name: str, value: Any) -> bytes:
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{_quote(name)}"\r\n\r\n'
        f"{value}\r\n"
    ).encode()


def _file_part(boundary: str, name: str, path: Path, limit: int, kind: str) -> bytes:
    try:
        size = path.stat().st_size
    except OSError as exc:
        _die(f"cannot read {kind} {path}: {exc}", 2)
    if size > limit:
        _die(f"{kind} {path} is {size} bytes; the limit is {limit} bytes", 2)
    try:
        data = path.read_bytes()
    except OSError as exc:
        _die(f"cannot read {kind} {path}: {exc}", 2)
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{_quote(name)}"; filename="{_quote(path.name)}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode() + data + b"\r\n"


def _multipart(
    fields: dict[str, Any],
    files: list[tuple[str, Path, int, str]],
) -> tuple[bytes, str]:
    boundary = "----videogateway" + secrets.token_hex(16)
    chunks: list[bytes] = []
    for name, value in fields.items():
        if value is None:
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        chunks.append(_field_part(boundary, name, value))
    for name, path, limit, kind in files:
        chunks.append(_file_part(boundary, name, path, limit, kind))
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _build_create_payload(args: argparse.Namespace) -> tuple[dict[str, Any], list[tuple[str, Path, int, str]]]:
    fields: dict[str, Any] = {"prompt": args.prompt}
    if args.model:
        fields["model"] = args.model
    if args.size:
        fields["size"] = args.size
    if args.seconds is not None:
        fields["seconds"] = args.seconds
    if args.seed is not None:
        fields["seed"] = args.seed
    if args.enhance is not None:
        fields["enhance_prompt"] = args.enhance
    if args.aspect_ratio:
        fields["aspect_ratio"] = args.aspect_ratio

    files: list[tuple[str, Path, int, str]] = []
    if args.image:
        files.append(("input_reference", Path(args.image), IMAGE_LIMIT, "image"))
    if args.last_image:
        files.append(("last_reference", Path(args.last_image), IMAGE_LIMIT, "image"))
    for value in args.reference_image or []:
        files.append(("reference_images", Path(value), IMAGE_LIMIT, "image"))
    for value in args.reference_video or []:
        files.append(("reference_videos", Path(value), VIDEO_LIMIT, "video"))
    for value in args.reference_audio or []:
        files.append(("reference_audios", Path(value), AUDIO_LIMIT, "audio"))
    return fields, files


# ------------------------------------------------------------------------ render
def _print_models(data: dict[str, Any]) -> None:
    print(f"prompt enhancement: {'available' if data.get('prompt_enhancement_available') else 'unavailable'}")
    if not data.get("regenerate_hd_available"):
        print("regenerate HD: unavailable")
    print()
    for model in data.get("data", []):
        print(f"{model['id']}  ({model.get('label', '')})")
        print(f"  default: {model.get('default_size')} · {model.get('default_duration')}s · {model.get('fps')} fps")
        print(f"  durations: {', '.join(str(x) for x in model.get('durations', []))} seconds")
        if model.get("supports_references"):
            caps = [
                (
                    f"ordered refs {model.get('max_reference_images')}i/"
                    f"{model.get('max_reference_videos')}v/{model.get('max_reference_audios')}a"
                )
            ]
        else:
            caps = []
            if model.get("image_to_video"):
                caps.append("first-frame")
            if model.get("supports_last_frame"):
                caps.append("last-frame")
        if model.get("adaptive_aspect_ratio"):
            caps.append("adaptive aspect")
        print(f"  capabilities: {', '.join(caps) or 'text only'}")
        cost = model.get("estimated_cost_usd", {})
        print(f"  estimate: ${cost.get('warm', 0):.3f} warm / ${cost.get('cold', 0):.3f} cold")
        print()


def _print_status(video: dict[str, Any], *, stream=sys.stdout) -> None:
    usage = video.get("usage") or {}
    print(f"id       {video.get('id')}", file=stream)
    print(f"status   {video.get('status')} ({video.get('progress')}%)", file=stream)
    print(f"model    {video.get('model')}", file=stream)
    print(f"format   {video.get('size')} · {video.get('seconds')}s · {video.get('fps')} fps", file=stream)
    print(f"cost     ${float(video.get('cost_usd') or 0):.4f}", file=stream)
    if video.get("stage"):
        print(f"stage    {video.get('stage')}", file=stream)
    if video.get("error"):
        print(f"error    {video.get('error')}", file=stream)
    metrics = [
        f"load {usage['model_load_s']:.1f}s" if usage.get("model_load_s") is not None else None,
        f"inference {usage['inference_s']:.1f}s" if usage.get("inference_s") is not None else None,
        f"encode {usage['encode_s']:.1f}s" if usage.get("encode_s") is not None else None,
        f"peak VRAM {usage['peak_vram_mb']:.0f} MB" if usage.get("peak_vram_mb") is not None else None,
    ]
    metrics = [item for item in metrics if item]
    if metrics:
        print(f"usage    {' · '.join(metrics)}", file=stream)


def _get_video(video_id: str, timeout: float) -> dict[str, Any]:
    return _json_request("GET", f"/v1/videos/{urllib.parse.quote(video_id)}", None, timeout)


def _wait(video_id: str, *, interval: float, timeout: float, quiet: bool) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last = None
    while True:
        video = _get_video(video_id, 60.0)
        status = video.get("status")
        if status in TERMINAL_STATES:
            if not quiet:
                print(file=sys.stderr)
            return video
        marker = (status, video.get("progress"), video.get("stage"))
        if not quiet and marker != last:
            stage = video.get("stage") or status
            print(
                f"{status} {video.get('progress', 0):>3}%  {stage}",
                file=sys.stderr,
            )
            last = marker
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GatewayError(f"wait timed out after {timeout:g}s; job may still be running")
        time.sleep(min(interval, remaining))


def _content_token(video: dict[str, Any]) -> str:
    url = video.get("download_url") or video.get("content_url")
    if not url:
        raise GatewayError("video has no media URL yet")
    token = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("token", [""])[0]
    if not token:
        raise GatewayError("video media URL has no token")
    return token


def _download(video: dict[str, Any], destination: Path, timeout: float) -> Path:
    video_id = video["id"]
    token = _content_token(video)
    path = (
        f"/v1/videos/{urllib.parse.quote(video_id)}/content?"
        + urllib.parse.urlencode({"download": 1, "token": token})
    )
    url = _base_url() + path
    destination = destination.expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise GatewayError(f"refusing to overwrite {destination}; choose another path")
    temporary = destination.with_name(f".{destination.name}.part")
    req = urllib.request.Request(url, headers=_headers(), method="GET")
    try:
        with _OPENER.open(req, timeout=timeout) as response, temporary.open("wb") as handle:
            shutil.copyfileobj(response, handle, length=1024 * 1024)
    except urllib.error.HTTPError as exc:
        message, code = _error_from_body(exc.read())
        temporary.unlink(missing_ok=True)
        raise GatewayError(message, status=exc.code, code=code) from exc
    except urllib.error.URLError as exc:
        temporary.unlink(missing_ok=True)
        raise GatewayError(f"download failed: {exc.reason}") from exc
    finally:
        if temporary.exists() and temporary.stat().st_size == 0:
            temporary.unlink(missing_ok=True)
    os.replace(temporary, destination)
    return destination


def _default_download_path(video: dict[str, Any], directory: str | None) -> Path:
    name = f"{video['id']}.mp4"
    return Path(directory).expanduser() / name if directory else Path(name)


# ------------------------------------------------------------------------ commands
def cmd_models(args: argparse.Namespace) -> int:
    data = _json_request("GET", "/v1/models", None, args.timeout)
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        _print_models(data)
    return 0


def cmd_enhance(args: argparse.Namespace) -> int:
    fields, files = _build_create_payload(args)
    if files:
        body, content_type = _multipart(fields, files)
    else:
        body = json.dumps(fields).encode("utf-8")
        content_type = "application/json"
    data, _ = request("POST", "/v1/prompts/enhance", body=body, content_type=content_type, timeout=args.timeout)
    if args.out:
        destination = Path(args.out).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(data.get("enhanced") or "", encoding="utf-8")
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(
            f"{data.get('mode')} · {data.get('size')} · {data.get('seconds')}s · "
            f"enhancer {data.get('model') or 'unavailable'} · {data.get('images_sent', 0)} image(s)",
            file=sys.stderr,
        )
        if data.get("error"):
            print(f"note: {data['error']}", file=sys.stderr)
        print(data.get("enhanced") or data.get("original") or "")
    return 0


def cmd_create(args: argparse.Namespace) -> int:
    fields, files = _build_create_payload(args)
    if files:
        body, content_type = _multipart(fields, files)
    else:
        body = json.dumps(fields).encode("utf-8")
        content_type = "application/json"
    video, _ = request("POST", "/v1/videos", body=body, content_type=content_type, timeout=args.timeout)
    if args.json and not args.wait:
        print(json.dumps(video, indent=2, ensure_ascii=False))
    else:
        print(f"submitted {video.get('id')} ({video.get('status')})", file=sys.stderr)
        print(f"{video.get('size')} · {video.get('seconds')}s · reserved ${float(video.get('reserved_usd') or 0):.4f}", file=sys.stderr)
    if not args.wait:
        if not args.json:
            print(video.get("id"))
        return 0

    video = _wait(video["id"], interval=args.interval, timeout=args.wait_timeout, quiet=args.quiet)
    if args.json:
        print(json.dumps(video, indent=2, ensure_ascii=False))
    else:
        _print_status(video, stream=sys.stderr)
    if video.get("status") != "completed":
        print(f"job ended as {video.get('status')}: {video.get('error') or 'no error detail'}", file=sys.stderr)
        return 3
    if args.download_dir or args.out:
        destination = Path(args.out) if args.out else _default_download_path(video, args.download_dir)
        saved = _download(video, destination, args.download_timeout)
        print(str(saved))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    video = _get_video(args.id, args.timeout)
    if args.json:
        print(json.dumps(video, indent=2, ensure_ascii=False))
    else:
        _print_status(video)
    return 0


def cmd_wait(args: argparse.Namespace) -> int:
    video = _wait(args.id, interval=args.interval, timeout=args.wait_timeout, quiet=args.quiet)
    if args.json:
        print(json.dumps(video, indent=2, ensure_ascii=False))
    else:
        _print_status(video)
    if video.get("status") != "completed":
        print(f"job ended as {video.get('status')}: {video.get('error') or 'no error detail'}", file=sys.stderr)
        return 3
    if args.download_dir or args.out:
        destination = Path(args.out) if args.out else _default_download_path(video, args.download_dir)
        saved = _download(video, destination, args.download_timeout)
        print(str(saved))
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    video = _get_video(args.id, args.timeout)
    if video.get("status") != "completed":
        _die(f"video is {video.get('status')}, not completed", 3)
    destination = Path(args.out) if args.out else _default_download_path(video, args.download_dir)
    saved = _download(video, destination, args.download_timeout)
    print(str(saved))
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    video = _json_request(
        "POST",
        f"/v1/videos/{urllib.parse.quote(args.id)}/cancel",
        None,
        args.timeout,
    )
    if args.json:
        print(json.dumps(video, indent=2, ensure_ascii=False))
    else:
        _print_status(video)
    return 0


# -------------------------------------------------------------------------- parser
def _prompt_from_file(value: str | None) -> str | None:
    if value is None:
        return None
    if value == "-":
        return sys.stdin.read()
    path = Path(value)
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dependency-free Video Gateway client. Credentials come from the environment.",
    )
    parser.add_argument("--timeout", type=float, default=180.0, help="HTTP timeout for one request (default 180s)")
    parser.add_argument("--json", action="store_true", help="print the raw JSON response")
    sub = parser.add_subparsers(dest="command", required=True)

    models = sub.add_parser("models", help="list models and capabilities")
    models.set_defaults(func=cmd_models)

    def add_prompt_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--model", help="model id (default fasth3-v2)")
        p.add_argument("--prompt", required=True, help="prompt text, or a file path, or - for stdin")
        p.add_argument("--size", help="auto or WxH (default: server adaptive default)")
        p.add_argument("--aspect-ratio", help="e.g. 16:9 with --size auto")
        p.add_argument("--seconds", type=float, help="5, 10 or 15 (default 10)")
        p.add_argument("--seed", type=int)
        p.add_argument(
            "--enhance",
            action=argparse.BooleanOptionalAction,
            default=None,
            help="preview-enhance before generation (server default true); use --no-enhance for a final H3 prompt",
        )
        p.add_argument("--image", help="first-frame image")
        p.add_argument("--last-image", help="last-frame image")
        p.add_argument("--reference-image", action="append", help="ordered OmniRef image (repeatable)")
        p.add_argument("--reference-video", action="append", help="ordered OmniRef video (repeatable)")
        p.add_argument("--reference-audio", action="append", help="ordered OmniRef audio (repeatable)")

    enhance = sub.add_parser("enhance", help="preview an H3 prompt without a GPU job")
    add_prompt_args(enhance)
    enhance.add_argument("--out", help="write the enhanced prompt to this file")
    enhance.set_defaults(func=cmd_enhance)

    create = sub.add_parser("create", help="submit exactly one video job")
    add_prompt_args(create)
    create.add_argument("--wait", action="store_true", help="poll until the job reaches a terminal state")
    create.add_argument("--interval", type=float, default=4.0, help="poll interval in seconds (default 4)")
    create.add_argument("--wait-timeout", type=float, default=1800.0, help="wait timeout in seconds (default 1800)")
    create.add_argument("--download-dir", help="after completion, save <id>.mp4 here")
    create.add_argument("--out", help="after completion, save to this exact path")
    create.add_argument("--download-timeout", type=float, default=600.0)
    create.add_argument("--quiet", action="store_true", help="suppress progress lines")
    create.set_defaults(func=cmd_create)

    status = sub.add_parser("status", help="show one job")
    status.add_argument("--id", required=True)
    status.set_defaults(func=cmd_status)

    wait = sub.add_parser("wait", help="poll one existing job")
    wait.add_argument("--id", required=True)
    wait.add_argument("--interval", type=float, default=4.0)
    wait.add_argument("--wait-timeout", type=float, default=1800.0)
    wait.add_argument("--download-dir")
    wait.add_argument("--out")
    wait.add_argument("--download-timeout", type=float, default=600.0)
    wait.add_argument("--quiet", action="store_true")
    wait.set_defaults(func=cmd_wait)

    download = sub.add_parser("download", help="download a completed job")
    download.add_argument("--id", required=True)
    download.add_argument("--out")
    download.add_argument("--download-dir")
    download.add_argument("--download-timeout", type=float, default=600.0)
    download.set_defaults(func=cmd_download)

    cancel = sub.add_parser("cancel", help="cancel a queued or running job")
    cancel.add_argument("--id", required=True)
    cancel.set_defaults(func=cmd_cancel)

    # Accept the global flags after the subcommand too, e.g. "status --json".
    for command in sub.choices.values():
        command.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        command.add_argument("--timeout", type=float, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.prompt = _prompt_from_file(getattr(args, "prompt", None))
    try:
        return int(args.func(args))
    except GatewayError as exc:
        suffix = f" [{exc.code}]" if exc.code else ""
        status = f" (HTTP {exc.status})" if exc.status else ""
        print(f"error{suffix}{status}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted; the submitted job is unchanged", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
