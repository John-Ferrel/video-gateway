"""FFmpeg post-processing: normalize, faststart, poster and metadata probe."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_STREAM = re.compile(r"Stream #\d+:\d+.*?Video:.*?(\d{2,5})x(\d{2,5})")
_AUDIO_STREAM = re.compile(r"Stream #\d+:\d+.*?Audio:")
_VIDEO_CODEC = re.compile(r"Stream #\d+:\d+.*?Video:\s*([A-Za-z0-9_]+)")
_PIX_FMT = re.compile(r"Stream #\d+:\d+.*?Video:.*?,\s*(yuv[a-z0-9]+)")


class FFmpegError(RuntimeError):
    pass


def ffmpeg_bin() -> str:
    import os

    configured = os.getenv("FFMPEG_BIN", "").strip()
    if configured:
        return configured
    system = shutil.which("ffmpeg")
    if system:
        return system
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise FFmpegError("ffmpeg is not installed and imageio-ffmpeg has no binary") from exc


def _run(args: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            args,
            check=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", "replace")[-4000:]
        raise FFmpegError(f"ffmpeg failed: {stderr}") from exc
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError("ffmpeg timed out") from exc


def probe(path: Path) -> dict:
    """Probe duration and dimensions using ffmpeg's stderr (no ffprobe dependency)."""
    binary = ffmpeg_bin()
    process = subprocess.run(
        [binary, "-hide_banner", "-i", str(path)],
        capture_output=True,
        check=False,
        timeout=60,
    )
    text = process.stderr.decode("utf-8", "replace")
    result: dict = {
        "duration_s": None,
        "width": None,
        "height": None,
        "has_audio": False,
        "video_codec": None,
        "pix_fmt": None,
    }
    match = _DURATION.search(text)
    if match:
        hours, minutes, seconds = match.groups()
        result["duration_s"] = round(int(hours) * 3600 + int(minutes) * 60 + float(seconds), 3)
    match = _VIDEO_STREAM.search(text)
    if match:
        result["width"], result["height"] = int(match.group(1)), int(match.group(2))
    result["has_audio"] = bool(_AUDIO_STREAM.search(text))
    codec = _VIDEO_CODEC.search(text)
    if codec:
        result["video_codec"] = codec.group(1).lower()
    pix_fmt = _PIX_FMT.search(text)
    if pix_fmt:
        result["pix_fmt"] = pix_fmt.group(1).lower()
    return result


def transcode(src: Path, dst: Path, thumb: Path) -> dict:
    """Normalize the worker's MP4 for browser playback and make a poster.

    The worker's H.264/yuv420p video stream is copied unchanged, so the
    generated visual bitstream is preserved. Audio is loudness-normalized and
    re-encoded to AAC; non-H.264 sources use a high-quality CRF 16 fallback.
    """
    binary = ffmpeg_bin()
    dst.parent.mkdir(parents=True, exist_ok=True)
    thumb.parent.mkdir(parents=True, exist_ok=True)
    source_info = probe(src)
    args = [
        binary,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
    ]
    # The worker already emits browser-compatible H.264 yuv420p. Stream-copy
    # it so the visual output is bit-for-bit the generated bitstream; only the
    # audio is re-encoded for loudness normalization. Fall back to a high-quality
    # CRF 16 encode for any other source.
    if source_info.get("video_codec") == "h264" and source_info.get("pix_fmt") == "yuv420p":
        args += ["-c:v", "copy"]
    else:
        args += [
            "-c:v", "libx264",
            "-preset", "slow",
            "-crf", "16",
            "-pix_fmt", "yuv420p",
        ]
    if source_info.get("has_audio"):
        # Native model audio can be mixed quietly. Normalize to a comfortable
        # browser playback level without changing the video stream's visual
        # quality. H3 emits 96 kHz stereo; FFmpeg preserves the rate.
        args += ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"]
    args += [
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        "-shortest",
        str(dst),
    ]
    _run(args, timeout=300)
    try:
        _run(
            [
                binary,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                "0.7",
                "-i",
                str(dst),
                "-frames:v",
                "1",
                "-vf",
                "scale='min(640,iw)':-2",
                "-q:v",
                "3",
                str(thumb),
            ],
            timeout=60,
        )
    except FFmpegError:
        # A poster is nice-to-have; never fail a completed video over it.
        pass
    info = probe(dst)
    info["source_duration_s"] = source_info.get("duration_s")
    info["bytes"] = dst.stat().st_size if dst.exists() else 0
    return info


def extract_audio_levels(path: Path) -> dict | None:
    """Optional diagnostics used by the benchmark report."""
    try:
        binary = ffmpeg_bin()
        process = subprocess.run(
            [binary, "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True,
            check=False,
            timeout=120,
        )
        text = process.stderr.decode("utf-8", "replace")
        mean = re.search(r"mean_volume:\s*(-?[\d.]+) dB", text)
        peak = re.search(r"max_volume:\s*(-?[\d.]+) dB", text)
        return {
            "mean_volume_db": float(mean.group(1)) if mean else None,
            "max_volume_db": float(peak.group(1)) if peak else None,
        }
    except Exception:
        return None


def write_sidecar(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, default=str))
