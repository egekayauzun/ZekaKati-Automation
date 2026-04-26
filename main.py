from __future__ import annotations

import json
import math
import os
import random
import shutil
import subprocess
import traceback
from pathlib import Path

from moviepy import AudioFileClip

from src.brain import BrainService, Scene, ShortsScript
from src.logger import LOGGER, setup_logging
from src.transcriber import VideoAudioTranscriber
from src.trends import TrendService
from src.utils import ensure_directories, load_environment
from src.video_engine import VideoEngine
from src.video_fetcher import VideoFetcher
from src.voice import VoiceGenerator


ROOT = Path(__file__).parent
ASSETS = ROOT / "assets"
AUDIO_DIR = ASSETS / "audio"
IMAGE_DIR = ASSETS / "images"
VIDEO_DIR = ASSETS / "videos"
MUSIC_DIR = ASSETS / "music"
OUTPUT_DIR = ASSETS / "output"
UPLOADS_DIR = ASSETS / "uploads"

DEFAULT_TOPICS = [
    "Teknoloji haberleri",
    "Ekonomi gündemi",
    "Spor gündemi",
    "Sağlık trendleri",
    "Bilim ve uzay gelişmeleri",
]

# Kategori → varsayılan konu (trend alınamazsa)
CATEGORY_DEFAULT_TOPICS: dict[str, list[str]] = {
    "gundem":    ["Gündemdeki önemli gelişmeler", "Bugünün öne çıkan haberleri"],
    "teknoloji": ["Yapay zeka gelişmeleri", "Son teknoloji haberleri"],
    "spor":      ["Spor dünyasından öne çıkan haberler", "Futbol gündemi"],
    "saglik":    ["Sağlıklı yaşam ipuçları", "Tıp dünyasından son gelişmeler"],
    "bilim":     ["Bilim ve uzay haberleri", "Keşifler ve buluşlar"],
    "ekonomi":   ["Ekonomi gündemi", "Piyasalarda son durum"],
}


# ── Duration helpers ──────────────────────────────────────────────────────────

def _clamp_duration(raw: int | float | None, default: int = 45) -> int:
    """Süreyi 10–180 saniye arasına sıkıştırır; geçersizse default döner."""
    if raw is None:
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    if v < 10 or v > 180:
        LOGGER.warning("duration_seconds=%d geçersiz; %d saniyeye ayarlandı.", v, default)
        return default
    return v


def _env_bool(name: str, default: bool = True) -> bool:
    raw = str(os.getenv(name, str(default))).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _cleanup_temp_scene_videos() -> None:
    if not _env_bool("CLEANUP_SCENE_VIDEOS", True):
        LOGGER.info("Temp scene cleanup disabled by CLEANUP_SCENE_VIDEOS")
        return
    if not VIDEO_DIR.exists():
        return
    files = sorted(VIDEO_DIR.glob("scene_*.mp4"))
    if not files:
        return
    LOGGER.info("Temp cleanup: %d scene files found", len(files))
    for fp in files:
        try:
            fp.unlink(missing_ok=True)
            LOGGER.info("Temp cleanup deleted: %s", fp.name)
        except Exception as e:
            LOGGER.warning("Temp cleanup failed for %s: %s", fp.name, e)


def _resolve_ffmpeg_executable() -> str:
    """
    Resolve ffmpeg executable robustly on Windows:
    1) FFMPEG_BIN env var
    2) PATH lookup
    3) local portable .tools/ffmpeg/**/bin/ffmpeg(.exe)
    """
    env_ffmpeg = os.getenv("FFMPEG_BIN", "").strip()
    if env_ffmpeg and Path(env_ffmpeg).exists():
        return env_ffmpeg

    from shutil import which

    path_hit = which("ffmpeg")
    if path_hit:
        return path_hit

    local_tools = ROOT / ".tools" / "ffmpeg"
    if local_tools.exists():
        patterns = ["**/bin/ffmpeg.exe", "**/bin/ffmpeg"]
        for pat in patterns:
            hits = sorted(local_tools.glob(pat))
            if hits:
                return str(hits[0])

    raise FileNotFoundError(
        "ffmpeg bulunamadı. PATH'e ekleyin veya FFMPEG_BIN env ile tam yol verin."
    )


# ── Topic selection ───────────────────────────────────────────────────────────

def _pick_topic(
    topic: str | None,
    use_trends: bool,
    trend_category: str | None = None,
    trend_source_preference: str = "reddit_first",
) -> str:
    """Konu seçer: verilen topic > trend > varsayılan liste."""
    selected_topic = topic
    if not selected_topic and use_trends:
        LOGGER.info("Trend konuları çekiliyor (kategori=%s)", trend_category or "varsayılan")
        trend_service = TrendService()
        try:
            trends = trend_service.top_trends(
                limit=8,
                category=trend_category,
                source_preference=trend_source_preference,
            )
        except Exception:
            trends = []
        clean = [t for t in trends if t and str(t).strip()]
        if clean:
            selected_topic = random.choice(clean)
        else:
            # Reddit + Google başarısız; kategoriye göre varsayılan
            if trend_category:
                key = trend_category.strip().lower()
                fallbacks = CATEGORY_DEFAULT_TOPICS.get(key, DEFAULT_TOPICS)
            else:
                fallbacks = DEFAULT_TOPICS
            selected_topic = random.choice(fallbacks)
    elif not selected_topic:
        selected_topic = random.choice(DEFAULT_TOPICS)
    return str(selected_topic)


# ── Scene rescale ─────────────────────────────────────────────────────────────

def rescale_scenes_to_audio_duration(script: ShortsScript, audio_duration: float) -> ShortsScript:
    """Stretch scene windows so their total length matches the generated audio."""
    if not script.scenes or audio_duration <= 0:
        return script
    chunks = [max(0.1, float(s.end_second) - float(s.start_second)) for s in script.scenes]
    old_sum = sum(chunks)
    if old_sum < 0.01:
        return script
    scale = audio_duration / old_sum
    t = 0.0
    new_scenes: list[Scene] = []
    for scene, d in zip(script.scenes, chunks):
        new_d = d * scale
        t_end = t + new_d
        new_scenes.append(
            Scene(
                start_second=int(round(t)),
                end_second=int(round(t_end)),
                image_prompt=scene.image_prompt,
                caption=scene.caption,
            )
        )
        t = t_end
    if new_scenes:
        last0 = new_scenes[-1]
        new_scenes[-1] = Scene(
            start_second=last0.start_second,
            end_second=int(round(audio_duration)),
            image_prompt=last0.image_prompt,
            caption=last0.caption,
        )
    return ShortsScript(title=script.title, voiceover_text=script.voiceover_text, scenes=new_scenes)


# ── Draft & Run (Auto/Manual) ─────────────────────────────────────────────────

def generate_draft_script(
    topic: str | None = None,
    use_trends: bool = True,
    trend_category: str | None = None,
    trend_source_preference: str = "reddit_first",
    duration_seconds: int | None = None,
) -> tuple[ShortsScript, str]:
    """
    Create a script from topic (or trend). Returns (script, selected_topic) for API/UI editing.

    trend_category: 'gundem' | 'teknoloji' | 'spor' | 'saglik' | 'bilim' | 'ekonomi' | None
    duration_seconds: hedef video süresi saniye cinsinden (10–180). None → ~45s.
    """
    setup_logging()
    load_environment()
    ensure_directories([AUDIO_DIR, IMAGE_DIR, VIDEO_DIR, MUSIC_DIR, OUTPUT_DIR])

    dur = _clamp_duration(duration_seconds)
    selected_topic = _pick_topic(topic, use_trends, trend_category, trend_source_preference)
    LOGGER.info(
        "Draft: konu=%s kategori=%s süre=%ds",
        selected_topic, trend_category or "—", dur,
    )
    brain = BrainService()
    script: ShortsScript = brain.generate_script(selected_topic, duration_seconds=dur)
    return script, selected_topic


def run(
    topic: str | None = None,
    voice_id: str | None = None,
    use_trends: bool = True,
    script: ShortsScript | None = None,
    trend_category: str | None = None,
    trend_source_preference: str = "reddit_first",
    duration_seconds: int | None = None,
    source_video_paths: list[str] | None = None,
    use_video_audio_subtitles: bool = False,
    tts_stability: float | None = None,
    tts_similarity_boost: float | None = None,
    tts_speed: float | None = None,
    voice_volume: float = 1.30,
) -> Path:
    """Ana otomasyon akışı (Otomatik / Manuel mod)."""
    setup_logging()
    LOGGER.info("Otomasyon başlatıldı")

    load_environment()
    ensure_directories([AUDIO_DIR, IMAGE_DIR, VIDEO_DIR, MUSIC_DIR, OUTPUT_DIR])

    dur = _clamp_duration(duration_seconds)

    source_paths = [Path(p) for p in (source_video_paths or []) if str(p).strip()]
    for p in source_paths:
        if not p.exists():
            raise FileNotFoundError(f"Kaynak video bulunamadı: {p}")

    output_path = OUTPUT_DIR / "final_video.mp4"
    try:
        selected_topic: str
        if script is not None:
            selected_topic = (topic or script.title or "").strip() or _pick_topic(None, use_trends=False)
            LOGGER.info("Önceden onaylanmış senaryo kullanılıyor; konu: %s", selected_topic)
        else:
            selected_topic = _pick_topic(topic, use_trends, trend_category, trend_source_preference)
            LOGGER.info("Seçilen konu: %s (kategori=%s, süre=%ds)", selected_topic, trend_category or "—", dur)
            brain = BrainService()
            script = brain.generate_script(selected_topic, duration_seconds=dur)
            LOGGER.info("Senaryo oluşturuldu")

        audio_path = AUDIO_DIR / "voiceover.mp3"
        timestamps: list[dict[str, float | str]] = []
        audio_dur = 0.0

        if use_video_audio_subtitles:
            if not source_paths:
                raise ValueError("Videonun sesinden altyazı modu için en az bir video gerekli.")
            LOGGER.info("Videonun sesinden altyazı modu aktif")
            transcriber = VideoAudioTranscriber(model_size=os.getenv("WHISPER_MODEL_SIZE", "small"))
            transcript, timestamps = transcriber.transcribe_video(source_paths[0], language="tr")
            ac: AudioFileClip | None = None
            try:
                ac = AudioFileClip(str(source_paths[0]))
                audio_dur = float(ac.duration or 0.0)
            finally:
                if ac is not None:
                    ac.close()
            script = ShortsScript(
                title=topic or "Sesden Altyazı",
                voiceover_text=transcript,
                scenes=[Scene(start_second=0, end_second=int(max(1.0, round(audio_dur))), image_prompt="uploaded video", caption="")],
            )
            audio_path = source_paths[0]
        else:
            LOGGER.info("ElevenLabs ile seslendirme oluşturuluyor")
            voice = VoiceGenerator(voice_id=voice_id)
            audio_path, timestamps = voice.generate_audio(
                script.voiceover_text,
                str(audio_path),
                stability=tts_stability,
                similarity_boost=tts_similarity_boost,
                speed=tts_speed,
            )
            LOGGER.info("Seslendirme kaydedildi: %s", audio_path)
            ac: AudioFileClip | None = None
            try:
                ac = AudioFileClip(str(audio_path))
                audio_dur = float(ac.duration or 0.0)
            finally:
                if ac is not None:
                    ac.close()
            if audio_dur > 0:
                script = rescale_scenes_to_audio_duration(script, audio_dur)

        LOGGER.info("Sahne videoları hazırlanıyor")
        video_fetcher = VideoFetcher()
        short_project_mode = dur <= 20 or (audio_dur > 0 and audio_dur <= 20.0)
        max_unique_fetches = 2 if short_project_mode else 999
        downloaded_scene_files: list[Path] = []
        if short_project_mode:
            LOGGER.info("Kısa video modu aktif: maksimum %d yeni stok klip indirilecek", max_unique_fetches)
        for idx, scene in enumerate(script.scenes):
            scene_output_path = VIDEO_DIR / f"scene_{idx + 1}.mp4"
            if idx < len(source_paths):
                scene_output_path.unlink(missing_ok=True)
                try:
                    os.link(str(source_paths[idx]), str(scene_output_path))
                except OSError:
                    shutil.copy2(str(source_paths[idx]), str(scene_output_path))
                LOGGER.info("Kaynak video sahneye bağlandı: %s", scene_output_path.name)
                continue
            if short_project_mode and len(downloaded_scene_files) >= max_unique_fetches and downloaded_scene_files:
                reuse_src = downloaded_scene_files[idx % len(downloaded_scene_files)]
                scene_output_path.unlink(missing_ok=True)
                try:
                    os.link(str(reuse_src), str(scene_output_path))
                except OSError:
                    shutil.copy2(str(reuse_src), str(scene_output_path))
                LOGGER.info(
                    "Kısa video modu: yeni indirme yerine %s tekrar kullanıldı -> %s",
                    reuse_src.name,
                    scene_output_path.name,
                )
                continue
            scene_query = scene.image_prompt or scene.caption or selected_topic
            fallbacks = [q for q in (scene.caption, selected_topic) if q and q.strip() != scene_query.strip()]
            scene_duration = max(0.1, float(scene.end_second) - float(scene.start_second))
            fetched = video_fetcher.fetch_video(
                scene_query,
                str(scene_output_path),
                fallback_queries=fallbacks,
                target_scene_duration=scene_duration,
                short_project_mode=short_project_mode,
            )
            downloaded_scene_files.append(fetched)

        LOGGER.info("Final video render ediliyor")
        video_engine = VideoEngine()
        video_engine.create_shorts(
            script_data=script,
            audio_path=audio_path,
            timestamps=timestamps,
            output_path=output_path,
            voice_volume=voice_volume,
        )

        LOGGER.info("Video oluşturuldu: %s", output_path)
        return output_path
    finally:
        _cleanup_temp_scene_videos()


# ── Semi-Auto helpers ─────────────────────────────────────────────────────────

def _parse_semi_auto_scenes_input(
    raw: str | list | None,
    audio_duration: float,
) -> tuple[list[Scene], int]:
    """
    Yarı Otomatik mod sahne girişini ayrıştırır.

    raw:
      - None / boş → sesle eşleşen tek sahne
      - JSON dizisi → her eleman dict veya string olabilir
      - Düz metin → her satır bir sahne başlığı (süre eşit paylaştırılır)

    Returns (scenes, original_count) where original_count scenes before filler.
    """
    if not raw:
        # Sahne belirtilmemiş: tüm ses için tek sahne
        return [
            Scene(
                start_second=0,
                end_second=int(math.ceil(audio_duration)),
                image_prompt="cinematic background footage",
                caption="",
            )
        ], 1

    if isinstance(raw, list):
        scenes_data: list = raw
    else:
        raw_str = str(raw).strip()
        try:
            parsed = json.loads(raw_str)
            if not isinstance(parsed, list):
                raise ValueError("JSON bir dizi (array) olmalı")
            scenes_data = parsed
        except (json.JSONDecodeError, ValueError):
            # Düz metin: her satır = bir sahne
            lines = [ln.strip() for ln in raw_str.splitlines() if ln.strip()]
            if not lines:
                raise ValueError("Sahne listesi boş. En az bir satır veya JSON array girin.")
            n = len(lines)
            per_scene = audio_duration / n if audio_duration > 0 else 5.0
            scenes_data = [
                {
                    "start_second": round(per_scene * i, 3),
                    "end_second": round(per_scene * (i + 1), 3),
                    "caption": line,
                    "image_prompt": line,
                }
                for i, line in enumerate(lines)
            ]

    scenes: list[Scene] = []
    t = 0.0
    for raw_scene in scenes_data:
        if isinstance(raw_scene, dict):
            start = float(raw_scene.get("start_second", t))
            end = float(raw_scene.get("end_second", start + 5.0))
            caption = str(raw_scene.get("caption", ""))
            image_prompt = str(raw_scene.get("image_prompt", caption or "background"))
        else:
            start = t
            end = t + 5.0
            caption = str(raw_scene)
            image_prompt = caption or "background"

        if end <= start:
            end = start + 5.0
        scenes.append(
            Scene(
                start_second=int(round(start)),
                end_second=int(round(end)),
                image_prompt=image_prompt,
                caption=caption,
            )
        )
        t = float(end)

    original_count = len(scenes)

    # Ses süresi > kompozisyon süresi → filler sahne ekle
    if scenes:
        composition_end = float(max(s.end_second for s in scenes))
        if audio_duration > composition_end + 0.5:
            LOGGER.info(
                "Yarı Otomatik: ses=%.1fs > kompozisyon=%.1fs; filler sahne ekleniyor (%d–%d)",
                audio_duration, composition_end,
                int(composition_end), int(math.ceil(audio_duration)),
            )
            scenes.append(
                Scene(
                    start_second=int(composition_end),
                    end_second=int(math.ceil(audio_duration)),
                    image_prompt="cinematic background footage",
                    caption="",
                )
            )

    return scenes, original_count


def _prepare_semi_auto_video_files(
    uploaded_video_paths: list[str | Path],
    scenes: list[Scene],
    original_count: int,
    topic: str,
) -> None:
    """
    Sahne video dosyalarını hazırlar:
    - original_count'a kadar olan sahneler: yüklenen video (hardlink veya kopyala)
    - Geri kalan filler sahneler: Pexels'ten stok klip
    """
    ensure_directories([VIDEO_DIR, UPLOADS_DIR])
    uploaded_list = [Path(p) for p in uploaded_video_paths]
    if not uploaded_list:
        raise ValueError("Yarı Otomatik: en az bir yüklenen video gerekli.")
    video_fetcher = VideoFetcher()

    for idx, scene in enumerate(scenes):
        dest = VIDEO_DIR / f"scene_{idx + 1}.mp4"
        if idx < original_count:
            uploaded = uploaded_list[idx % len(uploaded_list)]
            if not uploaded.exists():
                raise FileNotFoundError(f"Yüklenen video bulunamadı: {uploaded}")
            dest.unlink(missing_ok=True)
            try:
                os.link(str(uploaded), str(dest))
                LOGGER.info("Yarı Otomatik: sahne %d hardlink oluşturuldu: %s", idx + 1, dest.name)
            except OSError:
                shutil.copy2(str(uploaded), str(dest))
                LOGGER.info("Yarı Otomatik: sahne %d kopyalandı: %s", idx + 1, dest.name)
        else:
            query = scene.image_prompt or topic
            fallbacks = [topic] if query.strip() != topic.strip() else []
            LOGGER.info(
                "Yarı Otomatik: sahne %d için stok klip çekiliyor (sorgu: %s)",
                idx + 1, query[:60],
            )
            video_fetcher.fetch_video(query, str(dest), fallback_queries=fallbacks)


# ── Semi-Auto entry point ─────────────────────────────────────────────────────

def run_semi_auto(
    uploaded_video_paths: list[str | Path],
    scenes_input: str | list | None,
    voiceover_text: str,
    voice_id: str | None = None,
    topic: str | None = None,
    tts_stability: float | None = None,
    tts_similarity_boost: float | None = None,
    tts_speed: float | None = None,
    voice_volume: float = 1.30,
) -> Path:
    """
    Yarı Otomatik mod akışı.

    Kullanıcı: yüklenen video + sahne listesi + seslendirme metni sağlar.
    Backend: TTS üretir, kompozisyon süresi yetersizse Pexels'ten klip ekler, final video oluşturur.
    """
    setup_logging()
    LOGGER.info("Yarı Otomatik mod başlatıldı")
    load_environment()
    ensure_directories([AUDIO_DIR, IMAGE_DIR, VIDEO_DIR, MUSIC_DIR, OUTPUT_DIR, UPLOADS_DIR])
    try:
        vt = str(voiceover_text or "").strip()
        if not vt:
            raise ValueError("Yarı Otomatik modda 'voiceover_text' zorunludur.")

        LOGGER.info("TTS seslendirme oluşturuluyor")
        voice = VoiceGenerator(voice_id=voice_id)
        audio_path = AUDIO_DIR / "voiceover.mp3"
        audio_path, timestamps = voice.generate_audio(
            vt,
            str(audio_path),
            stability=tts_stability,
            similarity_boost=tts_similarity_boost,
            speed=tts_speed,
        )
        LOGGER.info("TTS ses kaydedildi: %s", audio_path)

        ac: AudioFileClip | None = None
        try:
            ac = AudioFileClip(str(audio_path))
            audio_duration = float(ac.duration or 0.0)
        finally:
            if ac is not None:
                ac.close()
        LOGGER.info("Ses süresi: %.2f saniye", audio_duration)

        scenes, original_count = _parse_semi_auto_scenes_input(scenes_input, audio_duration)
        LOGGER.info(
            "Toplam sahne: %d (%d kullanıcı + %d filler)",
            len(scenes), original_count, len(scenes) - original_count,
        )

        if not uploaded_video_paths:
            raise ValueError("Yarı Otomatik mod için en az bir video yüklenmelidir.")
        _prepare_semi_auto_video_files(
            uploaded_video_paths=uploaded_video_paths,
            scenes=scenes,
            original_count=original_count,
            topic=topic or "cinematic background footage",
        )

        script = ShortsScript(
            title=topic or "Yarı Otomatik Video",
            voiceover_text=vt,
            scenes=scenes,
        )

        LOGGER.info("Final video render ediliyor")
        video_engine = VideoEngine()
        output_path = OUTPUT_DIR / "final_video.mp4"
        video_engine.create_shorts(
            script_data=script,
            audio_path=audio_path,
            timestamps=timestamps,
            output_path=output_path,
            voice_volume=voice_volume,
        )

        LOGGER.info("Yarı Otomatik mod tamamlandı: %s", output_path)
        return output_path
    finally:
        _cleanup_temp_scene_videos()


def run_revoice(
    text: str,
    voice_id: str | None = None,
    tts_stability: float | None = None,
    tts_similarity_boost: float | None = None,
    tts_speed: float | None = None,
    voice_volume: float = 1.30,
    refresh_subtitles: bool = False,
) -> Path:
    """
    Fast re-voice flow:
    - Default: swap audio quickly with ffmpeg stream-copy (no full re-render).
    - Optional: refresh subtitles too (slower full re-render).
    """
    setup_logging()
    load_environment()
    ensure_directories([AUDIO_DIR, VIDEO_DIR, OUTPUT_DIR, MUSIC_DIR])
    source_video = OUTPUT_DIR / "final_video.mp4"
    if not source_video.exists():
        raise FileNotFoundError("Hızlı yeniden seslendirme için önce bir final_video.mp4 üretin.")
    trimmed = str(text or "").strip()
    if not trimmed:
        raise ValueError("Yeniden seslendirme metni boş olamaz.")
    voice = VoiceGenerator(voice_id=voice_id)
    revoice_audio = AUDIO_DIR / "revoice.mp3"
    revoice_audio, _ = voice.generate_audio(
        trimmed,
        str(revoice_audio),
        stability=tts_stability,
        similarity_boost=tts_similarity_boost,
        speed=tts_speed,
    )

    # Fast path: replace audio track only, keep video stream intact (no frame re-encode).
    if not refresh_subtitles:
        tmp_out = OUTPUT_DIR / "final_video_revoice_tmp.mp4"
        ffmpeg_exec = _resolve_ffmpeg_executable()
        ffmpeg_cmd = [
            ffmpeg_exec,
            "-y",
            "-i",
            str(source_video),
            "-i",
            str(revoice_audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-shortest",
            str(tmp_out),
        ]
        try:
            subprocess.run(ffmpeg_cmd, check=True, capture_output=True, text=True)
            tmp_out.replace(source_video)
            LOGGER.info("Hızlı revoice tamamlandı (audio-only, no re-render).")
            return source_video
        except Exception as e:
            LOGGER.error("ffmpeg hızlı revoice başarısız: %s", e)
            tmp_out.unlink(missing_ok=True)
            raise RuntimeError(
                "Hızlı ses değiştirme başarısız. ffmpeg PATH içinde değil veya erişilemiyor. "
                "Lütfen ffmpeg kurulumunu/PATH'i doğrulayıp API'yi yeniden başlatın."
            ) from e

    # Slow path (subtitle refresh): avoid source/target collision by using a temporary source copy.
    source_copy = OUTPUT_DIR / "final_video_revoice_source.mp4"
    if source_copy.exists():
        source_copy.unlink(missing_ok=True)
    shutil.copy2(source_video, source_copy)
    try:
        out = run(
            topic="Hızlı Yeniden Seslendirme",
            voice_id=voice_id,
            use_trends=False,
            script=ShortsScript(
                title="Hızlı Yeniden Seslendirme",
                voiceover_text=trimmed,
                scenes=[Scene(start_second=0, end_second=60, image_prompt="revoice", caption="")],
            ),
            source_video_paths=[str(source_copy)],
            tts_stability=tts_stability,
            tts_similarity_boost=tts_similarity_boost,
            tts_speed=tts_speed,
            voice_volume=voice_volume,
        )
        return out
    finally:
        source_copy.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        setup_logging()
        LOGGER.error("Otomasyon başarısız: %s", exc)
        LOGGER.debug(traceback.format_exc())
        raise
