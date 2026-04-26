from __future__ import annotations

from pathlib import Path
from typing import Any
import re

from moviepy import (
    AudioFileClip,
    ColorClip,
    CompositeAudioClip,
    CompositeVideoClip,
    TextClip,
    VideoFileClip,
    afx,
    concatenate_videoclips,
)

from .logger import LOGGER


class VideoEngine:
    """Builds TikTok/Shorts style videos with dynamic word subtitles."""

    def __init__(
        self,
        width: int = 1080,
        height: int = 1920,
        fps: int = 24,
        video_dir: str = "assets/videos",
        music_dir: str = "assets/music",
    ) -> None:
        self.width = width
        self.height = height
        self.fps = fps
        self.video_dir = Path(video_dir)
        self.music_dir = Path(music_dir)
        self.subtitle_safe_height = int(self.height * 0.24)
        # Slightly above bottom safe area so altyazı+stroke+descender kesilmesin
        self.subtitle_y = int(self.height * 0.63)
        # Uzun kelimelerde satır dışa taşmasın diye _build_word_subtitles max genişliğe ölçekler
        self._subtitle_word_font = 76
        self._word_gap = 8
        self._subtitle_max_line_width = int(self.width * 0.90)

    @staticmethod
    def _get_value(item: Any, key: str, default: Any = None) -> Any:
        if isinstance(item, dict):
            return item.get(key, default)
        return getattr(item, key, default)

    @staticmethod
    def _sanitize_subtitle_word(text: str) -> str:
        # Hide punctuation marks in on-screen word subtitles.
        cleaned = re.sub(r"[^\wçğıöşüÇĞİÖŞÜ]", "", str(text), flags=re.UNICODE)
        return cleaned.strip()

    def _fit_to_vertical(self, clip: Any) -> Any:
        ratio = clip.w / clip.h if clip.h else (self.width / self.height)
        target_ratio = self.width / self.height
        fitted = clip.resized(height=self.height) if ratio > target_ratio else clip.resized(width=self.width)
        return fitted.cropped(
            x_center=fitted.w / 2,
            y_center=fitted.h / 2,
            width=self.width,
            height=self.height,
        )

    def _loop_or_trim_video(self, video_clip: Any, scene_duration: float) -> Any:
        if video_clip.duration <= 0:
            return video_clip.with_duration(scene_duration)
        if video_clip.duration >= scene_duration:
            return video_clip.subclipped(0, scene_duration)

        pieces: list[Any] = []
        remaining = scene_duration
        while remaining > 0:
            part_duration = min(video_clip.duration, remaining)
            pieces.append(video_clip.subclipped(0, part_duration))
            remaining -= part_duration
        return concatenate_videoclips(pieces, method="compose").with_duration(scene_duration)

    def _build_scene_clip(self, index: int, scene_duration: float) -> Any:
        scene_file = self.video_dir / f"scene_{index + 1}.mp4"
        if scene_file.exists():
            base_video = VideoFileClip(str(scene_file))
            duration_fitted = self._loop_or_trim_video(base_video, scene_duration)
            return self._fit_to_vertical(duration_fitted).with_duration(scene_duration)

        # Fallback: if scene image is missing, keep rendering with dark background.
        return ColorClip(
            size=(self.width, self.height),
            color=(14, 14, 14),
            duration=scene_duration,
        )

    def _make_subtitle_word(self, text: str, color: str, font_value: str | None) -> Any:
        # "label" sizes the clip to the text (tight), unlike "caption" with a fixed width box
        # which made huge gaps between visually centered short words.
        tkw: dict[str, Any] = {
            "text": str(text).upper(),
            "font_size": self._subtitle_word_font,
            "color": color,
            "stroke_color": "#111111",
            "stroke_width": 2,
            "method": "label",
            "size": (None, None),
            # (h_pad, v_pad) — extra vertical so g,y,p and stroke are not cut off
            "margin": (10, 22),
            "interline": 0,
            "text_align": "center",
            "horizontal_align": "center",
            "vertical_align": "center",
        }
        if font_value:
            tkw["font"] = font_value
        try:
            return TextClip(**tkw)
        except Exception:
            tkw.pop("font", None)
            return TextClip(**tkw)

    def _build_word_subtitles(self, timestamps: list[dict[str, float | str]]) -> list[Any]:
        font_path = Path("C:/Windows/Fonts/arialbd.ttf")
        font_value = str(font_path) if font_path.exists() else None
        clean: list[dict[str, float | str]] = []
        for item in timestamps:
            w = self._sanitize_subtitle_word(str(item.get("word", "")))
            start = float(item.get("start", 0.0))
            end = float(item.get("end", start + 0.15))
            if w and end > start:
                clean.append({"word": w, "start": start, "end": end})
        if not clean:
            return []
        n = len(clean)
        clips: list[Any] = []
        highlight = "#FFDD2E"
        base = "#FFFFFF"
        for i in range(n):
            group0 = (i // 3) * 3
            group = clean[group0 : min(group0 + 3, n)]
            gidx = i - group0
            t0 = float(clean[i]["start"])
            t1 = float(clean[i]["end"])
            # Prevent subtitle flicker between words:
            # keep current word clip visible until the next word starts.
            if i < n - 1:
                next_start = float(clean[i + 1]["start"])
                if next_start > t1:
                    t1 = next_start
            if t1 <= t0:
                t1 = t0 + 0.12
            line_parts: list[Any] = []
            total_w = 0.0
            sizes: list[tuple[Any, float]] = []
            for j, seg in enumerate(group):
                c = base if j != gidx else highlight
                wclip = self._make_subtitle_word(str(seg["word"]), c, font_value)
                sizes.append((wclip, float(wclip.w)))
                total_w += wclip.w
            total_w += self._word_gap * max(0, len(sizes) - 1)
            max_w = self._subtitle_max_line_width
            if total_w > max_w and total_w > 0:
                s = max_w / total_w
                scaled: list[tuple[Any, float]] = []
                for wclip, _w in sizes:
                    rc = wclip.resized(s)
                    scaled.append((rc, float(rc.w)))
                sizes = scaled
                total_w = sum(w for _, w in sizes) + self._word_gap * max(0, len(sizes) - 1)
            x = (self.width - total_w) / 2.0
            # Keep a fixed line anchor to avoid per-word vertical wobble.
            line_y = int(self.subtitle_y)
            for wclip, w in sizes:
                line_parts.append(wclip.with_position((int(x), line_y)))
                x += w + self._word_gap
            comp = CompositeVideoClip(line_parts, size=(self.width, self.height))
            comp = comp.with_start(t0).with_end(t1)
            clips.append(comp)
        return clips

    def _find_background_music_file(self) -> Path | None:
        if not self.music_dir.is_dir():
            return None
        mp3s = sorted(self.music_dir.glob("*.mp3"))
        if not mp3s:
            return None
        return mp3s[0]

    def _mix_voice_with_background(
        self,
        voice_clip: Any,
        target_duration: float,
        voice_volume: float = 1.0,
    ) -> tuple[Any, Any | None]:
        """
        Mix main voice with optional looped background music at 15% volume.
        Returns (audio_clip_to_use, bg_music_source_clip_or_none to close later).
        """
        voice_gain = max(0.0, float(voice_volume))
        voice_for_mix = voice_clip.with_volume_scaled(voice_gain)
        music_path = self._find_background_music_file()
        if music_path is None:
            LOGGER.warning(
                "No background music found in '%s' (*.mp3). Voice-only export.",
                self.music_dir,
            )
            return voice_for_mix, None

        music_clip = None
        try:
            music_clip = AudioFileClip(str(music_path))
            looped = music_clip.with_effects([afx.AudioLoop(duration=target_duration)])
            quiet = looped.with_volume_scaled(0.15)
            mixed = CompositeAudioClip([voice_for_mix, quiet]).with_duration(target_duration)
            LOGGER.info("Mixed background music: %s at 15%% volume (looped)", music_path.name)
            return mixed, music_clip
        except Exception as e:
            LOGGER.error("Background music mix failed, using voice only: %s", e)
            if music_clip is not None:
                try:
                    music_clip.close()
                except Exception:
                    pass
            return voice_for_mix, None

    def create_shorts(
        self,
        script_data: Any,
        audio_path: str | Path,
        timestamps: list[dict[str, float | str]],
        output_path: str | Path,
        voice_volume: float = 1.0,
    ) -> Path:
        audio_file = Path(audio_path)
        output_file = Path(output_path)
        if not output_file.is_absolute() and output_file.parent == Path("."):
            output_file = Path("assets/output") / output_file
        if not output_file.is_absolute() and "assets\\output" not in str(output_file).replace("/", "\\"):
            output_file = Path("assets/output") / output_file
        output_file.parent.mkdir(parents=True, exist_ok=True)

        audio_clip = None
        bg_music_source: Any | None = None
        scene_timeline = None
        final_clip = None
        subtitle_clips: list[Any] = []
        scene_clips: list[Any] = []
        try:
            audio_clip = AudioFileClip(str(audio_file))
            scenes = self._get_value(script_data, "scenes", [])
            if not scenes:
                raise RuntimeError("script_data.scenes is required to create shorts video.")

            total_scene_duration = 0.0

            for idx, scene in enumerate(scenes):
                start_second = float(self._get_value(scene, "start_second", 0))
                end_second = float(self._get_value(scene, "end_second", start_second + 1))
                scene_duration = max(0.1, end_second - start_second)
                total_scene_duration += scene_duration
                scene_clips.append(self._build_scene_clip(idx, scene_duration))

            scene_timeline = concatenate_videoclips(scene_clips, method="compose")
            target_duration = audio_clip.duration if audio_clip.duration > 0 else total_scene_duration
            if abs(scene_timeline.duration - target_duration) > 0.15:
                scene_timeline = scene_timeline.with_duration(target_duration)

            subtitle_clips = self._build_word_subtitles(timestamps=timestamps)
            final_clip = CompositeVideoClip([scene_timeline, *subtitle_clips], size=(self.width, self.height))
            target_audio_duration = float(scene_timeline.duration or audio_clip.duration or 0.0)
            final_audio, bg_music_source = self._mix_voice_with_background(
                audio_clip,
                target_duration=target_audio_duration,
                voice_volume=voice_volume,
            )
            final_clip = final_clip.with_duration(scene_timeline.duration).with_audio(final_audio)

            final_clip.write_videofile(
                str(output_file),
                fps=self.fps,
                codec="libx264",
                audio_codec="aac",
            )
            return output_file
        except Exception as e:
            LOGGER.error("Video render failed for '%s': %s", output_file, e)
            raise
        finally:
            if final_clip is not None:
                final_clip.close()
            if scene_timeline is not None:
                scene_timeline.close()
            for clip in subtitle_clips:
                clip.close()
            for clip in scene_clips:
                clip.close()
            if bg_music_source is not None:
                try:
                    bg_music_source.close()
                except Exception:
                    pass
            if audio_clip is not None:
                audio_clip.close()

    def build_basic_video(self, audio_path: Path, output_path: Path) -> Path:
        """
        Backward-compatible wrapper for existing callers.

        Creates one full-length fallback scene and no dynamic subtitles.
        """
        script_stub = {"scenes": [{"start_second": 0, "end_second": 60, "caption": ""}]}
        return self.create_shorts(
            script_data=script_stub,
            audio_path=audio_path,
            timestamps=[],
            output_path=output_path,
        )
