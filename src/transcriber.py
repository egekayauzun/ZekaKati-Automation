from __future__ import annotations

from pathlib import Path
from typing import Any

from .logger import LOGGER


class VideoAudioTranscriber:
    """Transcribe a video audio track and produce word timestamps."""

    def __init__(self, model_size: str = "small") -> None:
        self.model_size = model_size

    def transcribe_video(self, video_path: str | Path, language: str = "tr") -> tuple[str, list[dict[str, float | str]]]:
        path = Path(video_path)
        if not path.exists():
            raise FileNotFoundError(f"Video not found for transcription: {path}")
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except Exception as e:
            raise RuntimeError(
                "Sesden altyazi modu icin 'faster-whisper' gerekli. "
                "Lutfen `pip install faster-whisper` kur."
            ) from e

        LOGGER.info("Transcribing video audio: %s", path)
        model = WhisperModel(self.model_size)
        segments, _ = model.transcribe(str(path), language=language, word_timestamps=True)

        words_out: list[dict[str, float | str]] = []
        chunks: list[str] = []
        for segment in segments:
            text = (segment.text or "").strip()
            if text:
                chunks.append(text)
            for w in getattr(segment, "words", []) or []:
                word = str(getattr(w, "word", "")).strip()
                start = float(getattr(w, "start", 0.0) or 0.0)
                end = float(getattr(w, "end", start + 0.15) or (start + 0.15))
                if not word or end <= start:
                    continue
                words_out.append({"word": word, "start": start, "end": end})

        transcript = " ".join(chunks).strip()
        if not transcript:
            raise RuntimeError("Video sesinden metin cikaramadi.")
        return transcript, words_out
