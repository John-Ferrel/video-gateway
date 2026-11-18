"""Filesystem layout and safe storage helpers."""

from __future__ import annotations

import os
from pathlib import Path

from .config import Settings


class Storage:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.data_dir
        self.uploads = settings.uploads_dir
        self.outputs = settings.outputs_dir
        self.thumbs = settings.thumbs_dir
        self.tmp = settings.tmp_dir

    def upload_path(self, video_id: str) -> Path:
        return self.uploads / f"{video_id}.jpg"

    def output_path(self, video_id: str) -> Path:
        return self.outputs / f"{video_id}.mp4"

    def thumb_path(self, video_id: str) -> Path:
        return self.thumbs / f"{video_id}.jpg"

    def raw_path(self, video_id: str) -> Path:
        return self.tmp / f"{video_id}.raw.mp4"

    def atomic_write(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        with open(temp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(path)

    def save_upload(self, video_id: str, data: bytes) -> Path:
        path = self.upload_path(video_id)
        self.atomic_write(path, data)
        return path

    def remove(self, path: Path | None) -> None:
        if not path:
            return
        try:
            Path(path).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def remove_video_files(self, video: dict) -> None:
        for key in ("input_image_path", "output_path", "thumb_path"):
            value = video.get(key)
            if not value:
                continue
            path = Path(value)
            # Only delete files under the data directory, even if the DB was
            # tampered with.
            try:
                path.resolve().relative_to(self.root.resolve())
            except ValueError:
                continue
            self.remove(path)

    def usage(self) -> dict:
        total = 0
        files = 0
        for directory in (self.outputs, self.thumbs, self.uploads):
            for path in directory.iterdir() if directory.exists() else []:
                if path.is_file():
                    total += path.stat().st_size
                    files += 1
        return {
            "bytes": total,
            "gb": round(total / 1e9, 3),
            "files": files,
            "max_gb": self.settings.max_storage_gb,
            "root": str(self.root),
        }

    def cleanup_orphans(self) -> int:
        """Remove files with no DB reference (best-effort)."""
        keep = set()
        # Imported lazily to avoid a circular import.
        from .db import Database

        db = Database(self.settings.db_path)
        for video in db.list_videos(limit=200, include_deleted=True):
            for key in ("input_image_path", "output_path", "thumb_path"):
                if video.get(key):
                    keep.add(Path(video[key]).name)
        removed = 0
        for directory in (self.outputs, self.thumbs, self.uploads, self.tmp):
            if not directory.exists():
                continue
            for path in directory.iterdir():
                if path.is_file() and path.name not in keep and not path.name.endswith(".tmp"):
                    try:
                        path.unlink()
                        removed += 1
                    except OSError:
                        pass
        return removed
