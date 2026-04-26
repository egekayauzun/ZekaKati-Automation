from pathlib import Path

try:
    # MoviePy v1.x
    from moviepy.editor import AudioFileClip, CompositeVideoClip, ImageClip, TextClip
except Exception:
    # MoviePy v2.x
    from moviepy import AudioFileClip, CompositeVideoClip, ImageClip, TextClip


# DIKKAT: Windows kullanicisi oldugun icin ImageMagick yolu belirtmen gerekebilir.
# Eger altyazi kisminda hata alirsan bu ayari aktif edecegiz.


def test_video_render() -> None:
    print("MoviePy render testi baslatiliyor...")

    image_path = Path("assets/images/test_scene_1.jpg")
    audio_path = Path("assets/audio/test_voice.mp3")
    output_path = Path("assets/output/test_render.mp4")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not image_path.exists() or not audio_path.exists():
        print("Hata: Test gorseli veya ses dosyasi bulunamadi! Once image/voice testlerini yap.")
        return

    img_clip = None
    audio_clip = None
    txt_clip = None
    video = None
    try:
        img_clip = ImageClip(str(image_path)).with_duration(5).resized(height=1920)
        audio_clip = AudioFileClip(str(audio_path)).subclipped(0, 5)

        font_path = Path("C:/Windows/Fonts/arialbd.ttf")
        font_value = str(font_path) if font_path.exists() else None

        try:
            text_kwargs = {}
            if font_value:
                text_kwargs["font"] = font_value
            txt_clip = TextClip(
                text="BU BIR TEST VIDEOSUDUR",
                font_size=70,
                color="yellow",
                stroke_color="black",
                stroke_width=2,
                method="label",
                **text_kwargs,
            )
        except TypeError:
            # MoviePy v1.x argument naming fallback
            text_kwargs = {}
            if font_value:
                text_kwargs["font"] = font_value
            txt_clip = TextClip(
                "BU BIR TEST VIDEOSUDUR",
                fontsize=70,
                color="yellow",
                stroke_color="black",
                stroke_width=2,
                **text_kwargs,
            )

        txt_clip = txt_clip.with_position("center").with_duration(5)
        video = CompositeVideoClip([img_clip, txt_clip]).with_audio(audio_clip)

        print("Render islemi basladi (bu biraz surebilir)...")
        video.write_videofile(str(output_path), fps=24, codec="libx264", audio_codec="aac")
        print(f"Basarili! Video burada: {output_path}")
    except Exception as e:
        print(f"Render sirasinda hata olustu: {e}")
        if "ImageMagick" in str(e):
            print("IPUCU: ImageMagick kurulu degil veya MoviePy tarafindan bulunamiyor.")
            print("Cozum: ImageMagick indir, kur ve 'Install legacy utilities' secenegini isaretle.")
    finally:
        for clip in (video, txt_clip, audio_clip, img_clip):
            if clip is not None:
                clip.close()


if __name__ == "__main__":
    test_video_render()
