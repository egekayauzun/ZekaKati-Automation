from __future__ import annotations

import json
import os
import re
from typing import List

import anthropic
from pydantic import BaseModel, ValidationError

from .logger import LOGGER
from .utils import require_env_var


class Scene(BaseModel):
    start_second: int
    end_second: int
    image_prompt: str
    caption: str


class ShortsScript(BaseModel):
    title: str
    voiceover_text: str
    scenes: List[Scene]


_SYSTEM_PROMPT = (
    "You are a viral YouTube Shorts expert. "
    "Your task is to generate compelling short-form video scripts. "
    "Respond ONLY with valid JSON matching the exact structure provided — "
    "no markdown, no explanations, no extra text outside the JSON. "
    "In JSON string values, escape any double quote as \\\" and newlines as \\n."
)


def _strip_markdown_json_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t, count=1, flags=re.IGNORECASE)
        t = re.sub(r"\s*```$", "", t, count=1)
    return t.strip()


def _extract_json_object(text: str) -> str:
    """
    Return the first balanced top-level JSON object. Handles model preamble / fences.
    """
    t = _strip_markdown_json_fence(text)
    start = t.find("{")
    if start == -1:
        raise ValueError("Yanıtta JSON objesi ( { ) yok")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return t[start : i + 1]
    raise ValueError("JSON yarım veya sınıra takıldı; token veya tırnak kaçışı hatası olabilir.")


def _parse_shorts_response(raw_text: str) -> ShortsScript:
    try:
        blob = _extract_json_object(raw_text)
    except ValueError as e:
        raise ValueError(str(e)) from e
    try:
        return ShortsScript.model_validate_json(blob)
    except json.JSONDecodeError as e:
        LOGGER.error("JSON ayrıştırma: %s | metin[0:500]=%r", e, (blob or "")[:500])
        raise
    except ValidationError as e:
        LOGGER.error("Şema doğrulama: %s", e)
        raise


def _duration_prompt_params(duration_seconds: int | None) -> tuple[int, int, int]:
    """
    Hedef süreye göre (saniye) sahne sayısı aralığı ve gerçek hedef döndürür.
    Returns (target_dur, min_scenes, max_scenes).
    """
    target = max(10, min(180, int(duration_seconds))) if duration_seconds else 45
    # ~1 sahne per 7-8 saniye, min 3, max 12
    min_scenes = max(3, target // 10)
    max_scenes = max(min_scenes + 2, target // 7)
    max_scenes = min(max_scenes, 12)
    return target, min_scenes, max_scenes


class BrainService:
    """Handles Shorts script generation using Claude."""

    def __init__(self, model: str | None = None) -> None:
        api_key = require_env_var("ANTHROPIC_API_KEY")
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model or os.getenv("ANTHROPIC_MODEL") or "claude-sonnet-4-6"
        self.fallback_models = ["claude-sonnet-4-6", "claude-3-5-sonnet-latest"]

    def generate_script(
        self,
        topic: str,
        duration_seconds: int | None = None,
    ) -> ShortsScript:
        """
        Generates a ShortsScript for the given topic.

        duration_seconds: istenen video süresi (saniye). None ise ~45s varsayılan.
        Senaryo ve sahne sayısı bu süreye göre ölçeklenir.
        """
        t = (topic or "").strip()
        if not t or t.lower() in ("none", "null", "undefined"):
            t = "Dikkat çekici güncel bir gündem kısa video konusu (sen seç)"

        target_dur, min_scenes, max_scenes = _duration_prompt_params(duration_seconds)
        LOGGER.info("Senaryo oluşturuluyor: konu=%r hedef=%ds sahne=%d-%d", t[:60], target_dur, min_scenes, max_scenes)

        candidate_models = [self.model] + [m for m in self.fallback_models if m != self.model]

        try:
            prompt = (
                f"Topic: {t}\n\n"
                f"Create a {target_dur}-second YouTube Shorts script in Turkish. "
                "Include a catchy title, full voiceover text in Turkish, "
                f"and {min_scenes}–{max_scenes} scenes each with an English image_prompt for the visual "
                "and a Turkish caption displayed on screen.\n\n"
                f"IMPORTANT: Target exactly {target_dur} seconds total. "
                "The voiceover text must be the right length to fill that duration when read at a natural pace. "
                f"The sum of all scene durations (end_second - start_second) must equal {target_dur}.\n\n"
                "Write voiceover_text in natural Turkish, following Turkish spelling and punctuation rules. "
                "Do not use ALL CAPS for emphasis. "
                "Avoid dramatic punctuation such as repeated exclamation/question marks, ellipsis, or dash-based pauses. "
                "Use fluent broadcaster-style sentences, and at most one exclamation mark in the whole text only if necessary.\n\n"
                "Keep image_prompt lines short (2–6 plain keywords) so stock video search works; "
                "avoid long cinematic sentences in image_prompt.\n\n"
                "Respond only with valid JSON matching this schema: "
                '{"title":"string","voiceover_text":"string","scenes":[{"start_second":0,"end_second":0,"image_prompt":"string","caption":"string"}]}'
            )

            last_not_found_error: anthropic.APIStatusError | None = None
            raw_text: str | None = None

            for model_name in candidate_models:
                try:
                    LOGGER.info("Trying Claude model: %s", model_name)
                    response = self.client.messages.create(
                        model=model_name,
                        max_tokens=8192,
                        system=_SYSTEM_PROMPT,
                        messages=[{"role": "user", "content": prompt}],
                    )
                    raw_text = response.content[0].text
                    break
                except anthropic.APIStatusError as e:
                    if e.status_code == 404:
                        last_not_found_error = e
                        LOGGER.warning("Model '%s' not found, trying fallback...", model_name)
                        continue
                    raise

            if raw_text is None:
                if last_not_found_error is not None:
                    raise last_not_found_error
                raise RuntimeError("No Claude response was generated.")

            return _parse_shorts_response(raw_text)
        except anthropic.APIConnectionError as e:
            LOGGER.error("Connection error while generating script for '%s': %s", topic, e)
            raise
        except anthropic.RateLimitError as e:
            LOGGER.error("Rate limit exceeded while generating script for '%s': %s", topic, e)
            raise
        except anthropic.APIStatusError as e:
            LOGGER.error(
                "API error %s while generating script for '%s': %s",
                e.status_code,
                topic,
                e.message,
            )
            raise
        except Exception as e:
            LOGGER.error("Unexpected error while generating script for '%s': %s", topic, e)
            raise


class Director(BrainService):
    """Backward-compatible alias for legacy imports/usages."""
