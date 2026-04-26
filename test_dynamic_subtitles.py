from __future__ import annotations

import json
from pathlib import Path

try:
    # MoviePy v1.x
    from moviepy.editor import AudioFileClip, CompositeVideoClip, ImageClip, TextClip
except Exception:
    # MoviePy v2.x
    from moviepy import AudioFileClip, CompositeVideoClip, ImageClip, TextClip


def test_dynamic_subtitles() -> None:
    print("Dinamik altyazi testi basliyor...")

    image_path = Path("assets/images/test_scene_1.jpg")
    audio_path = Path("assets/audio/test_voice.mp3")
    timestamps_path = Path("assets/audio/test_voice_timestamps.json")
    output_path = Path("assets/output/subtitle_test.mp4")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not image_path.exists() or not audio_path.exists():
        print("Hata: test gorseli veya test sesi bulunamadi.")
        return

    # Once gercek timestamp dosyasini kullan; yoksa ornek veriye dus.
    if timestamps_path.exists():
        timestamps = json.loads(timestamps_path.read_text(encoding="utf-8"))
    else:
        print("Uyari: Gercek timestamp dosyasi bulunamadi, ornek veri kullaniliyor.")
        timestamps = [
            {"word": "Yapay", "start": 0.0, "end": 0.5},
            {"word": "Zeka", "start": 0.5, "end": 1.0},
            {"word": "Gelecegi", "start": 1.0, "end": 1.8},
            {"word": "Degistiriyor!", "start": 1.8, "end": 2.8},
        ]

    bg_clip = None
    raw_audio_clip = None
    audio_clip = None
    final_video = None
    word_clips: list = []
    try:
        target_duration = 3.0
        if timestamps:
            target_duration = max(target_duration, float(timestamps[-1]["end"]) + 0.2)

        # 9:16 hedef kadraj (1080x1920 sabit)
        bg_clip = (
            ImageClip(str(image_path))
            .with_duration(target_duration)
            .resized(height=1920)
            .cropped(x_center=540, y_center=960, width=1080, height=1920)
        )
        raw_audio_clip = AudioFileClip(str(audio_path))
        target_duration = min(target_duration, raw_audio_clip.duration)
        audio_clip = raw_audio_clip.subclipped(0, target_duration)

        font_path = Path("C:/Windows/Fonts/arialbd.ttf")
        font_value = str(font_path) if font_path.exists() else None

        for item in timestamps:
            text_kwargs = {}
            if font_value:
                text_kwargs["font"] = font_value
            txt_clip = TextClip(
                text=str(item["word"]).upper(),
                font_size=80,
                color="yellow",
                stroke_color="black",
                stroke_width=2,
                method="caption",
                size=(900, 220),
                text_align="center",
                **text_kwargs,
            ).with_start(float(item["start"])).with_end(min(float(item["end"]), target_duration)).with_position(("center", 1360))
            word_clips.append(txt_clip)

        final_video = CompositeVideoClip([bg_clip, *word_clips], size=(1080, 1920)).with_audio(audio_clip)

        print("Altyazili video render ediliyor...")
        final_video.write_videofile(str(output_path), fps=24, codec="libx264", audio_codec="aac")
        print(f"Bitti! Video hazir: {output_path}")
    except Exception as e:
        print(f"Render sirasinda hata olustu: {e}")
        raise
    finally:
        if final_video is not None:
            final_video.close()
        if audio_clip is not None:
            audio_clip.close()
        if raw_audio_clip is not None:
            raw_audio_clip.close()
        if bg_clip is not None:
            bg_clip.close()
        for clip in word_clips:
            clip.close()


if __name__ == "__main__":
    test_dynamic_subtitles()
