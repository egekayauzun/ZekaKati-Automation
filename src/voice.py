from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

from elevenlabs import VoiceSettings, save
from elevenlabs.client import ElevenLabs

from .logger import LOGGER
from .utils import require_env_var


class VoiceGenerator:
    """Generate speech audio and per-word timestamps with ElevenLabs."""

    def __init__(self, voice_id: str | None = None) -> None:
        try:
            api_key = require_env_var("ELEVENLABS_API_KEY")
            self.client = ElevenLabs(api_key=api_key)
            self.voice_id = voice_id or os.getenv("ELEVENLABS_VOICE_ID") or "pMQM2vAjnEa9PmfDvgkY"
        except Exception as e:
            LOGGER.error("Voice generator initialization failed: %s", e)
            raise

    @staticmethod
    def _merge_character_alignment_to_words(
        characters: list[Any],
        starts: list[Any],
        ends: list[Any],
    ) -> list[dict[str, float | str]]:
        timestamps: list[dict[str, float | str]] = []
        current_word = ""
        current_start: float | None = None
        current_end: float | None = None

        for ch, start, end in zip(characters, starts, ends):
            char = str(ch)
            start_f = float(start)
            end_f = float(end)

            if char.isspace():
                if current_word and current_start is not None and current_end is not None:
                    timestamps.append({"word": current_word, "start": current_start, "end": current_end})
                current_word = ""
                current_start = None
                current_end = None
                continue

            if current_word == "":
                current_start = start_f
            current_word += char
            current_end = end_f

        if current_word and current_start is not None and current_end is not None:
            timestamps.append({"word": current_word, "start": current_start, "end": current_end})

        return timestamps

    @staticmethod
    def _extract_audio_and_timestamps(response: Any) -> tuple[Any, list[dict[str, float | str]]]:
        """
        Normalize SDK response to (audio_bytes_or_stream, timestamps).

        ElevenLabs SDK v1.x may return either:
        - (audio, alignment) tuple
        - dict-like/object response that contains audio and alignment fields
        """
        audio_data: Any = response
        alignment_data: Any = None

        if isinstance(response, tuple) and len(response) >= 2:
            audio_data, alignment_data = response[0], response[1]
        elif isinstance(response, dict):
            audio_data = response.get("audio_base64") or response.get("audio") or response.get("audio_data")
            alignment_data = (
                response.get("normalized_alignment")
                or response.get("alignment")
                or response.get("timestamps")
            )
        else:
            maybe_audio = (
                getattr(response, "audio", None)
                or getattr(response, "audio_base64", None)
                or getattr(response, "audio_base_64", None)
            )
            maybe_alignment = (
                getattr(response, "normalized_alignment", None)
                or getattr(response, "alignment", None)
                or getattr(response, "timestamps", None)
            )
            if maybe_audio is not None:
                audio_data = maybe_audio
            alignment_data = maybe_alignment

        # ElevenLabs SDK can return base64-encoded audio in timestamp responses.
        if isinstance(audio_data, str):
            try:
                audio_data = base64.b64decode(audio_data)
            except Exception:
                pass

        if hasattr(alignment_data, "model_dump"):
            alignment_data = alignment_data.model_dump()
        elif hasattr(alignment_data, "dict"):
            alignment_data = alignment_data.dict()

        if not isinstance(alignment_data, dict):
            return audio_data, []

        characters = alignment_data.get("characters") or []
        char_starts = alignment_data.get("character_start_times_seconds") or []
        char_ends = alignment_data.get("character_end_times_seconds") or []

        if characters and char_starts and char_ends:
            return audio_data, VoiceGenerator._merge_character_alignment_to_words(
                characters=characters,
                starts=char_starts,
                ends=char_ends,
            )

        words = alignment_data.get("words") or []
        starts = alignment_data.get("start_times_seconds") or []
        ends = alignment_data.get("end_times_seconds") or []

        timestamps: list[dict[str, float | str]] = []
        for word, start, end in zip(words, starts, ends):
            if isinstance(word, str) and word.strip():
                timestamps.append({"word": word, "start": float(start), "end": float(end)})
        return audio_data, timestamps

    def generate_audio(
        self,
        text: str,
        output_path: str,
        stability: float | None = None,
        similarity_boost: float | None = None,
        speed: float | None = None,
    ) -> tuple[Path, list[dict[str, float | str]]]:
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        stability_v = float(0.36 if stability is None else stability)
        similarity_v = float(0.85 if similarity_boost is None else similarity_boost)
        speed_v = float(0.95 if speed is None else speed)
        tts_settings = VoiceSettings(
            stability=stability_v,
            similarity_boost=similarity_v,
            speed=speed_v,
        )
        try:
            # SDK v1.x method name
            if hasattr(self.client.text_to_speech, "convert_with_timestamps"):
                response = self.client.text_to_speech.convert_with_timestamps(
                    voice_id=self.voice_id,
                    output_format="mp3_44100_128",
                    text=text,
                    model_id="eleven_multilingual_v2",
                    voice_settings=tts_settings,
                )
                audio, timestamps = self._extract_audio_and_timestamps(response)
                save(audio, str(output_file))
                return output_file, timestamps

            # Fallback for older SDKs
            audio = self.client.text_to_speech.convert(
                voice_id=self.voice_id,
                output_format="mp3_44100_128",
                text=text,
                model_id="eleven_multilingual_v2",
                voice_settings=tts_settings,
            )
            save(audio, str(output_file))
            return output_file, []
        except (ConnectionError, TimeoutError, OSError) as e:
            LOGGER.error("ElevenLabs API connection error while generating '%s': %s", output_file, e)
            raise
        except Exception as e:
            LOGGER.error("Voice generation failed for '%s': %s", output_file, e)
            raise


class VoiceService(VoiceGenerator):
    """Backward-compatible alias for legacy imports/usages."""

    def synthesize(self, text: str, output_path: Path) -> Path:
        audio_path, _ = self.generate_audio(text=text, output_path=str(output_path))
        return audio_path
