"""H3-structured prompt rewriting with the official MiniMax prompt skill.

The official `skills/h3-prompt-writing` guide is vendored under
``gateway/prompts/h3``. Base text/keyframe modes use ``base-en.txt`` and
full-reference OmniRef uses ``ref-en.txt``. The enhancer is a single
OpenAI-compatible chat call; it is optional and falls back to the original
prompt on any failure.
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

logger = logging.getLogger(__name__)

_GUIDE_DIR = Path(__file__).parent / "prompts" / "h3"
_GUIDE_FILES = {"base": "base-en.txt", "ref": "ref-en.txt"}
_GUIDES: dict[str, str] = {}

_GATEWAY_RULES = """\
Gateway constraints (these override any broader editorial permission in the guide):
- Use the exact requested mode and the exact requested duration. Do not write a
  different number of seconds.
- Preserve the user's subject, action, setting, artistic style and intent.
- The supplied images are visual source-of-truth frames or references. Preserve
  the character identity, art style (animation/cutout/illustration/live action),
  clothing, palette and existing background. Do not photorealize or restyle them.
- Do not invent a location, room, lighting setup, wardrobe change, extra person,
  object, story beat or camera move that the user did not ask for.
- Preserve all dialogue, lyrics and visible scene text verbatim in their original
  language inside the H3 tags. Do not translate or paraphrase them.
- Unless the user explicitly asks for cuts or multiple shots, keep one continuous
  shot for the whole clip. Do not invent shot changes.
- Keep reference labels consistent (<Picture N>, <Video N>, <Audio N>,
  <Subject N>) across every section.
- Return only the final prompt; no markdown fences, preface or explanation.
"""


def _guide(mode: str) -> str:
    kind = "ref" if mode == "ref2va" else "base"
    if kind not in _GUIDES:
        path = _GUIDE_DIR / _GUIDE_FILES[kind]
        _GUIDES[kind] = path.read_text(encoding="utf-8")
    return _GUIDES[kind]


def image_data_uri(data: bytes, *, max_side: int = 512) -> str | None:
    """Downscale an image for the text model; returns a JPEG data URI."""
    try:
        image = Image.open(io.BytesIO(data)).convert("RGB")
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=88, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    except Exception:
        return None


@dataclass(frozen=True)
class EnhancementResult:
    prompt: str
    applied: bool
    model: str | None = None
    error: str | None = None
    mode: str | None = None
    images_sent: int = 0


class PromptEnhancer:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        opencode_session: str = "",
        timeout_seconds: int = 60,
        enabled: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.opencode_session = opencode_session
        self.timeout_seconds = timeout_seconds
        self.enabled = enabled
        self._transport = transport
        self._cache: dict[str, EnhancementResult] = {}

    @property
    def available(self) -> bool:
        return bool(self.enabled and self.base_url and self.model and (self.api_key or self.opencode_session))

    @staticmethod
    def mode_for(
        *,
        has_first_frame: bool,
        has_last_frame: bool,
        references: list[dict[str, Any]],
    ) -> str:
        if references:
            return "ref2va"
        if has_first_frame and has_last_frame:
            return "fl2va"
        if has_first_frame:
            return "i2va"
        if has_last_frame:
            return "l2va"
        return "t2va"

    async def enhance(
        self,
        prompt: str,
        *,
        has_first_frame: bool,
        has_last_frame: bool,
        references: list[dict[str, Any]],
        seconds: int,
        width: int,
        height: int,
        images: list[dict[str, str]] | None = None,
    ) -> EnhancementResult:
        mode = self.mode_for(
            has_first_frame=has_first_frame,
            has_last_frame=has_last_frame,
            references=references,
        )
        images = images or []
        if not self.available:
            return EnhancementResult(
                prompt, False, error="prompt enhancer is not configured", mode=mode
            )

        image_key = tuple((item.get("label", ""), hashlib.sha256(item.get("data_uri", "").encode()).hexdigest())
                          for item in images)
        key = hashlib.sha256(
            repr((prompt, mode, seconds, width, height, references, image_key)).encode()
        ).hexdigest()
        if key in self._cache:
            return self._cache[key]

        facts = [
            f"Requested H3 mode: {mode.upper()}.",
            f"Duration: {seconds:.2f} seconds at 24 fps.",
            f"Canvas: {width}x{height}.",
            f"First frame supplied: {'yes' if has_first_frame else 'no'}.",
            f"Last frame supplied: {'yes' if has_last_frame else 'no'}.",
        ]
        if references:
            labels = []
            for ref in references:
                label = str(ref.get("label") or "")
                kind = str(ref.get("type") or "image")
                if not label:
                    label = f"<{kind.capitalize()} 1>"
                labels.append(f"{label} ({kind})")
            facts.append("Ordered references: " + ", ".join(labels) + ".")
        if images:
            facts.append("Attached images, in order: " + ", ".join(
                str(item.get("label") or "image") for item in images
            ) + ".")
        user_text = "\n".join(facts) + "\n\nUser request:\n" + prompt

        system = (
            "You are executing the official MiniMax H3 prompt-writing skill. "
            "Follow the guide below exactly, including field names, section order, "
            "timing notation, reference labels and dialogue tags.\n\n"
            f"<official_h3_prompt_writing_guide>\n{_guide(mode)}\n</official_h3_prompt_writing_guide>\n\n"
            f"{_GATEWAY_RULES}"
        )
        if images:
            content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
            for image in images:
                content.append({"type": "text", "text": str(image.get("label") or "Image") + ":"})
                content.append({
                    "type": "image_url",
                    "image_url": {"url": str(image.get("data_uri")), "detail": "low"},
                })
        else:
            content = [{"type": "text", "text": user_text}]

        url = self.base_url
        if not url.endswith("/chat/completions"):
            url = url + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.opencode_session:
            headers["x-opencode-session"] = self.opencode_session
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content if images else user_text},
            ],
            "temperature": 0.2,
            "max_tokens": 4096,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self._transport) as client:
                response = await client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()
            text = data["choices"][0]["message"]["content"].strip()
            if text.startswith("```"):
                lines = text.splitlines()
                if lines and lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].startswith("```"):
                    lines = lines[:-1]
                text = "\n".join(lines).strip()
            if not text:
                raise ValueError("empty enhancement")
            result = EnhancementResult(
                text, True, self.model, mode=mode, images_sent=len(images)
            )
        except Exception as exc:  # noqa: BLE001 - enhancement is best-effort
            logger.warning("H3 prompt enhancement failed: %s", exc)
            message = str(exc).strip() or type(exc).__name__
            result = EnhancementResult(
                prompt, False, self.model, message[:500], mode=mode, images_sent=len(images)
            )
        self._cache[key] = result
        if len(self._cache) > 128:
            self._cache.pop(next(iter(self._cache)))
        return result


__all__ = ["EnhancementResult", "PromptEnhancer", "image_data_uri"]
