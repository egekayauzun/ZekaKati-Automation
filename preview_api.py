from __future__ import annotations

import cgi
import io
import json
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from main import generate_draft_script, run as run_pipeline, run_revoice, run_semi_auto
from main import UPLOADS_DIR
from src.brain import ShortsScript
from src.logger import LOGGER, setup_logging
from src.trends import TrendService
from src.utils import load_environment
from src.voice import VoiceGenerator


# ── Helpers ───────────────────────────────────────────────────────────────────

def _validate_duration(raw, default: int = 45) -> int:
    """duration_seconds değerini 10–180 aralığına sıkıştırır."""
    if raw is None:
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    if v < 10 or v > 180:
        LOGGER.warning("Geçersiz duration_seconds=%s; %d kullanılıyor.", raw, default)
        return default
    return v


def _valid_category(raw) -> str | None:
    """Kategori değerini normalize eder; geçersizse None döner."""
    if not raw:
        return None
    v = str(raw).strip().lower()
    valid = {"gundem", "teknoloji", "spor", "saglik", "bilim", "ekonomi"}
    return v if v in valid else None


def _valid_trend_source_preference(raw) -> str:
    v = str(raw or "reddit_first").strip().lower()
    valid = {"reddit_first", "google_first", "reddit_only", "google_only"}
    return v if v in valid else "reddit_first"


def _resolve_uploaded_files(file_ids: list[str]) -> list[Path]:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for file_id in file_ids:
        fid = str(file_id or "").strip()
        if not fid:
            continue
        matches = list(UPLOADS_DIR.glob(f"{fid}.*"))
        if not matches:
            raise ValueError(f"file_id '{fid}' için yüklü dosya bulunamadı.")
        paths.append(matches[0])
    return paths


def _clamp_float(raw, default: float, low: float, high: float) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, v))


# ── Clip Job Helpers ──────────────────────────────────────────────────────────

CLIPS_DIR = Path("assets/clips")
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _hhmmss_to_seconds(t: str) -> int:
    """HH:MM:SS → toplam saniye."""
    try:
        parts = t.split(":")
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except Exception:
        return 0


def _valid_hhmmss(t: str) -> bool:
    """HH:MM:SS formatını doğrular."""
    m = re.fullmatch(r"\d{2}:\d{2}:\d{2}", t)
    if not m:
        return False
    parts = t.split(":")
    mm, ss = int(parts[1]), int(parts[2])
    return mm < 60 and ss < 60


def _extract_youtube_id(url: str) -> str | None:
    """YouTube URL'sinden video ID'sini çıkarır."""
    patterns = [
        r"[?&]v=([a-zA-Z0-9_-]{11})",
        r"youtu\.be/([a-zA-Z0-9_-]{11})",
        r"youtube\.com/shorts/([a-zA-Z0-9_-]{11})",
        r"youtube\.com/embed/([a-zA-Z0-9_-]{11})",
        r"youtube\.com/v/([a-zA-Z0-9_-]{11})",
        r"youtube\.com/live/([a-zA-Z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def _sanitize_clip_filename(name: str) -> str:
    """Dosya adını path traversal ve özel karakterlerden temizler."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.strip("_. ")
    if not name:
        name = "clip"
    return name[:64]


def _unique_clip_path(directory: Path, filename: str, ext: str = ".mp4") -> Path:
    """Benzersiz dosya yolu oluşturur; çakışmada _N soneki ekler."""
    base = directory / f"{filename}{ext}"
    if not base.exists():
        return base
    i = 1
    while True:
        candidate = directory / f"{filename}_{i}{ext}"
        if not candidate.exists():
            return candidate
        i += 1


def _get_ffmpeg_dir() -> str | None:
    """ffmpeg klasörünü bulur: FFMPEG_BIN env → .tools/ffmpeg → None."""
    import os
    from shutil import which

    ffmpeg_bin = os.environ.get("FFMPEG_BIN", "").strip()
    if ffmpeg_bin:
        p = Path(ffmpeg_bin)
        if p.exists():
            return str(p.parent if p.is_file() else p)

    path_hit = which("ffmpeg")
    if path_hit:
        return str(Path(path_hit).parent)

    for candidate in [
        Path(".tools/ffmpeg/ffmpeg.exe"),
        Path(".tools/ffmpeg/ffmpeg"),
        Path(".tools/ffmpeg/bin/ffmpeg.exe"),
        Path(".tools/ffmpeg/bin/ffmpeg"),
    ]:
        if candidate.exists():
            return str(candidate.parent)

    tools_root = Path(".tools/ffmpeg")
    if tools_root.exists():
        # Destek: .tools/ffmpeg/ffmpeg-8.1-full_build/bin/ffmpeg.exe
        for hit in sorted(tools_root.glob("**/bin/ffmpeg.exe")):
            return str(hit.parent)
        for hit in sorted(tools_root.glob("**/bin/ffmpeg")):
            return str(hit.parent)
    return None


def _get_ffmpeg_executable() -> str | None:
    """ffmpeg binary yolunu döndürür."""
    ffmpeg_dir = _get_ffmpeg_dir()
    if ffmpeg_dir:
        folder = Path(ffmpeg_dir)
        for name in ["ffmpeg.exe", "ffmpeg"]:
            candidate = folder / name
            if candidate.exists():
                return str(candidate)
    from shutil import which
    return which("ffmpeg")


def _process_clip_job(job_id: str, url: str, start: str, end: str, filename: str) -> None:
    """Arka planda yt-dlp ile tam video indirir, ffmpeg ile keser."""
    tmp_dir: Path | None = None

    def upd(**kw: object) -> None:
        with _jobs_lock:
            _jobs[job_id].update(kw)

    try:
        CLIPS_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = _sanitize_clip_filename(filename or "clip")
        out_path = _unique_clip_path(CLIPS_DIR, safe_name)

        tmp_dir = Path(tempfile.mkdtemp(prefix="yt_clip_"))

        LOGGER.info("Clip job %s başladı: url=%s %s→%s", job_id, url, start, end)

        # ── ffmpeg tespiti ────────────────────────────────────────────────────
        ffmpeg_dir = _get_ffmpeg_dir()
        ffmpeg_exe = _get_ffmpeg_executable()
        LOGGER.info("Clip job %s ffmpeg_dir=%s ffmpeg_exe=%s", job_id, ffmpeg_dir, ffmpeg_exe)

        if not ffmpeg_exe:
            raise RuntimeError(
                "ffmpeg bulunamadı. Lütfen ffmpeg'i .tools/ffmpeg/ klasörüne kopyalayın "
                "veya FFMPEG_BIN ortam değişkenini ayarlayıp API'yi yeniden başlatın.\n"
                "İndirme: https://www.gyan.dev/ffmpeg/builds/"
            )

        # ── yt-dlp: tam video indir ───────────────────────────────────────────
        # player_client=android → Node.js / Deno JS runtime ihtiyacını ortadan kaldırır
        upd(status="downloading", progress=10, message="Video indiriliyor…")

        # android_vr: JS/Node.js gerektirmez, 1080p'ye kadar format verir
        # android fallback: android_vr başarısız olursa düşük kalite ama garantili
        cmd = [
            "yt-dlp",
            "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "--merge-output-format", "mp4",
            "--no-playlist",
            "--extractor-args", "youtube:player_client=android_vr,android",
            "--ffmpeg-location", ffmpeg_dir,
            "-o", str(tmp_dir / "full.%(ext)s"),
            url,
        ]

        LOGGER.info("Clip job %s yt-dlp: %s", job_id, " ".join(cmd))
        upd(progress=15, message="yt-dlp başlatıldı, indiriliyor…")

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        stdout_tail = (proc.stdout or "")[-600:]
        stderr_tail = (proc.stderr or "")[-1000:]

        LOGGER.info("Clip job %s yt-dlp stdout: %s", job_id, stdout_tail)
        if proc.returncode != 0:
            LOGGER.error("Clip job %s yt-dlp stderr: %s", job_id, stderr_tail)
            # Kullanıcı dostu hata mesajı
            hint = ""
            if "Sign in" in stderr_tail or "login" in stderr_tail.lower():
                hint = "\nİpucu: Video üye girişi gerektiriyor olabilir."
            elif "Private video" in stderr_tail:
                hint = "\nİpucu: Video özel (private) olarak ayarlanmış."
            elif "not available" in stderr_tail.lower():
                hint = "\nİpucu: Video bu bölgede kullanılamıyor olabilir."
            raise RuntimeError(
                f"yt-dlp indirme hatası (kod {proc.returncode}).{hint}\n\nDetay:\n{stderr_tail[-500:]}"
            )

        # ── İndirilen dosyayı bul ─────────────────────────────────────────────
        upd(status="cutting", progress=72, message="Video kesiliyor…")

        mp4_files = sorted(tmp_dir.glob("*.mp4"))
        if not mp4_files:
            all_files = [f for f in tmp_dir.iterdir() if f.is_file()]
            if not all_files:
                raise RuntimeError(
                    "İndirilen video dosyası bulunamadı.\nyt-dlp çıktısı: " + stdout_tail[-300:]
                )
            mp4_files = all_files  # mp4 dışı format fallback

        src = mp4_files[0]
        final_path = out_path.with_suffix(".mp4")

        # ── ffmpeg: stream-copy ile kes (hızlı) ──────────────────────────────
        start_sec = _hhmmss_to_seconds(start)
        end_sec   = _hhmmss_to_seconds(end)
        duration  = end_sec - start_sec

        cut_cmd = [
            ffmpeg_exe, "-y",
            "-ss", str(start_sec),       # input seek (hızlı, keyframe'e atlar)
            "-i",  str(src),
            "-t",  str(duration),        # süre = bitiş - başlangıç
            "-c:v", "copy",
            "-c:a", "copy",
            "-avoid_negative_ts", "make_zero",  # timestamp temizliği
            str(final_path),
        ]
        LOGGER.info("Clip job %s ffmpeg kesme: %s", job_id, " ".join(cut_cmd))
        cut_proc = subprocess.run(cut_cmd, capture_output=True, text=True, timeout=300)

        if cut_proc.returncode != 0:
            # Fallback: stream-copy başarısız → re-encode (yavaş ama garantili)
            LOGGER.warning(
                "Clip job %s stream-copy başarısız (kod %d), re-encode deneniyor:\n%s",
                job_id, cut_proc.returncode, (cut_proc.stderr or "")[-400:],
            )
            upd(progress=78, message="Re-encode yapılıyor (yavaş mod, lütfen bekleyin)…")

            reenc_cmd = [
                ffmpeg_exe, "-y",
                "-ss", str(start_sec),
                "-i",  str(src),
                "-t",  str(duration),
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-avoid_negative_ts", "make_zero",
                str(final_path),
            ]
            reenc_proc = subprocess.run(reenc_cmd, capture_output=True, text=True, timeout=600)
            if reenc_proc.returncode != 0:
                err = (reenc_proc.stderr or "")[-800:]
                LOGGER.error("Clip job %s re-encode de başarısız: %s", job_id, err)
                raise RuntimeError(
                    f"ffmpeg kesme hatası (stream-copy ve re-encode denendi).\nDetay:\n{err[-500:]}"
                )

        shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir = None

        clip_rel = str(final_path).replace("\\", "/")
        dl_url   = f"/api/clips/{final_path.name}/download"

        upd(
            status="done",
            progress=100,
            message="Hazır!",
            clip_path=clip_rel,
            download_url=dl_url,
        )
        LOGGER.info("Clip job %s tamamlandı: %s", job_id, final_path.name)

    except Exception as exc:
        LOGGER.error("Clip job %s başarısız: %s", job_id, exc, exc_info=True)
        upd(
            status="error",
            progress=0,
            message=str(exc),
            error_code="PROCESSING_ERROR",
            details=str(exc),
        )
    finally:
        if tmp_dir and tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ── Handler ───────────────────────────────────────────────────────────────────

class PreviewHandler(BaseHTTPRequestHandler):
    server_version = "VoicePreviewHTTP/1.0"

    def log_message(self, format: str, *args) -> None:
        """
        Varsayilan stderr loglamasi bazi Windows terminal oturumlarinda
        OSError(22) uretebildigi icin istek akisini kesebiliyor.
        Bu nedenle loglari kendi LOGGER uzerinden guvenli sekilde yazariz.
        """
        try:
            LOGGER.info("HTTP %s - %s", self.address_string(), format % args)
        except Exception:
            # Log hatasi istek akisini asla bozmamali.
            pass

    def _set_headers(self, status: int, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _norm_path(self) -> str:
        base = self.path.split("?", 1)[0]
        if base != "/":
            base = base.rstrip("/")
        return base

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._set_headers(204)

    def do_GET(self) -> None:  # noqa: N802
        path = self._norm_path()
        if path.startswith("/assets/"):
            self._serve_static()
        elif path.startswith("/api/jobs/"):
            self._handle_job_status()
        elif path.startswith("/api/clips/") and path.endswith("/download"):
            self._handle_clip_download()
        else:
            self._set_headers(404)
            self.wfile.write(json.dumps({"error": "Not found"}).encode("utf-8"))

    def _serve_static(self) -> None:
        rel = self.path.lstrip("/").replace("/", "\\")
        file_path = Path(rel)
        if not file_path.exists() or not file_path.is_file():
            self._set_headers(404)
            self.wfile.write(json.dumps({"error": "File not found"}).encode("utf-8"))
            return
        ext = file_path.suffix.lower()
        mime = {".mp4": "video/mp4", ".mp3": "audio/mpeg"}.get(ext, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(file_path.stat().st_size))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        with open(file_path, "rb") as f:
            while chunk := f.read(1024 * 256):
                self.wfile.write(chunk)

    def do_POST(self) -> None:  # noqa: N802
        path = self._norm_path()
        routes = {
            "/api/voice-preview":  self._handle_voice_preview,
            "/api/generate":       self._handle_generate,
            "/api/revoice":        self._handle_revoice,
            "/api/draft":          self._handle_draft,
            "/api/trends":         self._handle_trends,
            "/api/upload":         self._handle_upload,
            "/api/semi-auto":      self._handle_semi_auto,
            "/api/download_clip":  self._handle_download_clip,
        }
        handler = routes.get(path)
        if handler:
            handler()
        else:
            self._set_headers(404)
            self.wfile.write(json.dumps({"error": "Not found"}).encode("utf-8"))

    def _read_payload(self) -> dict:
        raw_len = self.headers.get("Content-Length", "0")
        body = self.rfile.read(int(raw_len))
        return json.loads(body.decode("utf-8") or "{}")

    # ── /api/voice-preview ────────────────────────────────────────────────────

    def _handle_voice_preview(self) -> None:
        try:
            payload = self._read_payload()
            voice_id = str(payload.get("voice_id", "")).strip()
            text = str(payload.get("text", "Bu bir ses önizlemesidir.")).strip()
            tts_stability = _clamp_float(payload.get("stability"), 0.36, 0.0, 1.0)
            tts_similarity = _clamp_float(payload.get("similarity_boost"), 0.85, 0.0, 1.0)
            tts_speed = _clamp_float(payload.get("speed"), 0.95, 0.7, 1.3)
            if not voice_id:
                self._set_headers(400)
                self.wfile.write(json.dumps({"error": "voice_id is required"}).encode("utf-8"))
                return

            with tempfile.TemporaryDirectory() as tmp_dir:
                output_file = Path(tmp_dir) / "preview.mp3"
                generator = VoiceGenerator(voice_id=voice_id)
                audio_path, _ = generator.generate_audio(
                    text=text,
                    output_path=str(output_file),
                    stability=tts_stability,
                    similarity_boost=tts_similarity,
                    speed=tts_speed,
                )
                audio_bytes = audio_path.read_bytes()

            self._set_headers(200, "audio/mpeg")
            self.wfile.write(audio_bytes)
        except Exception as e:
            LOGGER.error("Voice preview failed: %s", e)
            self._set_headers(500)
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))

    # ── /api/draft ────────────────────────────────────────────────────────────

    def _handle_draft(self) -> None:
        try:
            payload = self._read_payload()
            topic = str(payload.get("topic", "")).strip() or None
            use_trends = bool(payload.get("use_trends", True))
            trend_category = _valid_category(payload.get("trend_category"))
            trend_source_preference = _valid_trend_source_preference(payload.get("trend_source_preference"))
            duration_seconds = _validate_duration(payload.get("duration_seconds"))

            LOGGER.info(
                "Draft API: konu=%s trend=%s kategori=%s süre=%ds",
                topic, use_trends, trend_category or "—", duration_seconds,
            )
            script, selected_topic = generate_draft_script(
                topic=topic,
                use_trends=use_trends,
                trend_category=trend_category,
                trend_source_preference=trend_source_preference,
                duration_seconds=duration_seconds,
            )
            self._set_headers(200)
            self.wfile.write(
                json.dumps(
                    {
                        "ok": True,
                        "topic": selected_topic,
                        "script": script.model_dump(),
                        "duration_seconds": duration_seconds,
                    }
                ).encode("utf-8")
            )
        except Exception as e:
            LOGGER.error("Draft API failed: %s", e)
            self._set_headers(500)
            self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))

    # ── /api/generate ─────────────────────────────────────────────────────────

    def _handle_generate(self) -> None:
        try:
            payload = self._read_payload()
            topic = str(payload.get("topic", "")).strip() or None
            voice_id = str(payload.get("voice_id", "")).strip() or None
            use_trends = bool(payload.get("use_trends", True))
            trend_category = _valid_category(payload.get("trend_category"))
            trend_source_preference = _valid_trend_source_preference(payload.get("trend_source_preference"))
            duration_seconds = _validate_duration(payload.get("duration_seconds"))
            tts_stability = _clamp_float(payload.get("stability"), 0.36, 0.0, 1.0)
            tts_similarity = _clamp_float(payload.get("similarity_boost"), 0.85, 0.0, 1.0)
            tts_speed = _clamp_float(payload.get("speed"), 0.95, 0.7, 1.3)
            voice_volume = _clamp_float(payload.get("voice_volume"), 1.30, 0.0, 2.0)
            script_raw = payload.get("script")
            uploaded_file_ids = payload.get("uploaded_file_ids") or []
            use_video_audio_subtitles = bool(payload.get("use_video_audio_subtitles", False))
            if isinstance(uploaded_file_ids, str):
                uploaded_file_ids = [uploaded_file_ids]
            source_video_paths = [str(p) for p in _resolve_uploaded_files(uploaded_file_ids)]

            prebuilt: ShortsScript | None = None
            if script_raw and isinstance(script_raw, dict):
                prebuilt = ShortsScript.model_validate(script_raw)
                if topic is None and prebuilt.title:
                    topic = prebuilt.title

            LOGGER.info(
                "Generate API: konu=%s trend=%s kategori=%s süre=%ds senaryo=%s",
                topic, use_trends and prebuilt is None, trend_category or "—",
                duration_seconds, "önceden hazır" if prebuilt else "yeni",
            )
            output_path = run_pipeline(
                topic=topic,
                voice_id=voice_id,
                use_trends=use_trends and prebuilt is None,
                script=prebuilt,
                trend_category=trend_category,
                trend_source_preference=trend_source_preference,
                duration_seconds=duration_seconds,
                source_video_paths=source_video_paths,
                use_video_audio_subtitles=use_video_audio_subtitles,
                tts_stability=tts_stability,
                tts_similarity_boost=tts_similarity,
                tts_speed=tts_speed,
                voice_volume=voice_volume,
            )
            self._set_headers(200)
            self.wfile.write(
                json.dumps(
                    {
                        "ok": True,
                        "output_path": str(output_path).replace("\\", "/"),
                        "video_url": "/assets/output/final_video.mp4",
                    }
                ).encode("utf-8")
            )
        except Exception as e:
            LOGGER.error("Generate API failed: %s", e)
            self._set_headers(500)
            self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))

    # ── /api/revoice ──────────────────────────────────────────────────────────

    def _handle_revoice(self) -> None:
        try:
            payload = self._read_payload()
            text = str(payload.get("text", "")).strip()
            voice_id = str(payload.get("voice_id", "")).strip() or None
            tts_stability = _clamp_float(payload.get("stability"), 0.36, 0.0, 1.0)
            tts_similarity = _clamp_float(payload.get("similarity_boost"), 0.85, 0.0, 1.0)
            tts_speed = _clamp_float(payload.get("speed"), 0.95, 0.7, 1.3)
            voice_volume = _clamp_float(payload.get("voice_volume"), 1.30, 0.0, 2.0)
            refresh_subtitles = bool(payload.get("refresh_subtitles", False))
            output_path = run_revoice(
                text=text,
                voice_id=voice_id,
                tts_stability=tts_stability,
                tts_similarity_boost=tts_similarity,
                tts_speed=tts_speed,
                voice_volume=voice_volume,
                refresh_subtitles=refresh_subtitles,
            )
            self._set_headers(200)
            self.wfile.write(
                json.dumps(
                    {
                        "ok": True,
                        "output_path": str(output_path).replace("\\", "/"),
                        "video_url": "/assets/output/final_video.mp4",
                    }
                ).encode("utf-8")
            )
        except Exception as e:
            LOGGER.error("Revoice API failed: %s", e)
            self._set_headers(500)
            self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))

    # ── /api/trends ───────────────────────────────────────────────────────────

    def _handle_trends(self) -> None:
        """Belirli bir kategori için trend konuları döndürür."""
        try:
            payload = self._read_payload()
            category = _valid_category(payload.get("category"))
            source_preference = _valid_trend_source_preference(payload.get("trend_source_preference"))
            limit = min(int(payload.get("limit", 8)), 20)

            LOGGER.info("Trends API: kategori=%s limit=%d", category or "varsayılan", limit)
            trend_service = TrendService()
            topics = trend_service.top_trends(
                limit=limit,
                category=category,
                source_preference=source_preference,
            )

            self._set_headers(200)
            self.wfile.write(
                json.dumps({"ok": True, "topics": topics, "category": category}).encode("utf-8")
            )
        except Exception as e:
            LOGGER.error("Trends API failed: %s", e)
            self._set_headers(500)
            self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))

    # ── /api/upload ───────────────────────────────────────────────────────────

    def _handle_upload(self) -> None:
        """
        Multipart form-data video yükleme. Field name: 'file' (single/multiple).
        Returns { ok, files:[{file_id, filename, path}], ... }.
        """
        try:
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type:
                self._set_headers(400)
                self.wfile.write(
                    json.dumps({"error": "Content-Type: multipart/form-data bekleniyor"}).encode()
                )
                return

            content_len = int(self.headers.get("Content-Length", "0"))
            if content_len > 600 * 1024 * 1024:  # 600 MB limit
                self._set_headers(413)
                self.wfile.write(json.dumps({"error": "Dosya boyutu 600 MB'yi aşıyor"}).encode())
                return

            raw_body = self.rfile.read(content_len)
            environ = {
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": content_type,
                "CONTENT_LENGTH": str(content_len),
            }
            fs = cgi.FieldStorage(
                fp=io.BytesIO(raw_body),
                environ=environ,
                keep_blank_values=True,
            )

            if "file" not in fs:
                self._set_headers(400)
                self.wfile.write(json.dumps({"error": "Form alanı 'file' bulunamadı"}).encode())
                return

            UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
            items = fs["file"]
            if not isinstance(items, list):
                items = [items]
            uploaded_files: list[dict[str, str]] = []
            for item in items:
                if not item.filename:
                    continue
                file_id = uuid.uuid4().hex
                suffix = Path(item.filename).suffix.lower() or ".mp4"
                dest = UPLOADS_DIR / f"{file_id}{suffix}"
                with open(dest, "wb") as fh:
                    fh.write(item.file.read())
                LOGGER.info("Dosya yüklendi: %s (%s)", dest.name, item.filename)
                uploaded_files.append(
                    {"file_id": file_id, "filename": item.filename, "path": str(dest)}
                )
            if not uploaded_files:
                self._set_headers(400)
                self.wfile.write(json.dumps({"error": "Yüklenecek dosya bulunamadı"}).encode())
                return
            self._set_headers(200)
            self.wfile.write(
                json.dumps({"ok": True, "files": uploaded_files, **uploaded_files[0]}).encode("utf-8")
            )
        except Exception as e:
            LOGGER.error("Upload failed: %s", e)
            self._set_headers(500)
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))

    # ── /api/semi-auto ────────────────────────────────────────────────────────

    def _handle_semi_auto(self) -> None:
        """
        Yarı Otomatik mod video üretimi.

        Payload:
          file_id        (str, zorunlu) – /api/upload'dan dönen ID
          voiceover_text (str, zorunlu) – seslendirme metni
          scenes         (str | array, opsiyonel) – sahne listesi veya JSON array
          voice_id       (str, opsiyonel) – ElevenLabs voice_id
          topic          (str, opsiyonel) – video başlığı / stok klip arama terimi
        """
        try:
            payload = self._read_payload()

            raw_ids = payload.get("file_ids") or payload.get("file_id") or []
            if isinstance(raw_ids, str):
                raw_ids = [raw_ids]
            if not raw_ids:
                self._set_headers(400)
                self.wfile.write(
                    json.dumps({"ok": False, "error": "file_ids zorunludur. Önce /api/upload ile video yükleyin."}).encode()
                )
                return

            voiceover_text = str(payload.get("voiceover_text", "")).strip()
            if not voiceover_text:
                self._set_headers(400)
                self.wfile.write(
                    json.dumps({"ok": False, "error": "voiceover_text zorunludur."}).encode()
                )
                return

            uploaded_paths = _resolve_uploaded_files(raw_ids)

            scenes_input = payload.get("scenes")  # str | list | None
            voice_id = str(payload.get("voice_id", "")).strip() or None
            topic = str(payload.get("topic", "")).strip() or None
            tts_stability = _clamp_float(payload.get("stability"), 0.36, 0.0, 1.0)
            tts_similarity = _clamp_float(payload.get("similarity_boost"), 0.85, 0.0, 1.0)
            tts_speed = _clamp_float(payload.get("speed"), 0.95, 0.7, 1.3)
            voice_volume = _clamp_float(payload.get("voice_volume"), 1.30, 0.0, 2.0)

            LOGGER.info(
                "Semi-auto API: file=%s konu=%s ses=%s",
                ",".join([p.name for p in uploaded_paths[:3]]), topic or "—", voice_id or "varsayılan",
            )

            output_path = run_semi_auto(
                uploaded_video_paths=uploaded_paths,
                scenes_input=scenes_input,
                voiceover_text=voiceover_text,
                voice_id=voice_id,
                topic=topic,
                tts_stability=tts_stability,
                tts_similarity_boost=tts_similarity,
                tts_speed=tts_speed,
                voice_volume=voice_volume,
            )

            self._set_headers(200)
            self.wfile.write(
                json.dumps(
                    {
                        "ok": True,
                        "output_path": str(output_path).replace("\\", "/"),
                        "video_url": "/assets/output/final_video.mp4",
                    }
                ).encode("utf-8")
            )
        except ValueError as e:
            LOGGER.error("Semi-auto validation error: %s", e)
            self._set_headers(400)
            self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))
        except Exception as e:
            LOGGER.error("Semi-auto API failed: %s", e)
            self._set_headers(500)
            self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))

    # ── /api/download_clip ────────────────────────────────────────────────────

    def _handle_download_clip(self) -> None:
        """
        YouTube videosundan klip oluşturma işi başlatır.

        Payload:
          url        (str, zorunlu) – YouTube URL
          start_time (str, zorunlu) – HH:MM:SS
          end_time   (str, zorunlu) – HH:MM:SS
          filename   (str, opsiyonel) – çıktı dosya adı (uzantısız)

        Döner:
          { ok, job_id, status, message }
        """
        try:
            payload = self._read_payload()
            url = str(payload.get("url", "")).strip()
            start = str(payload.get("start_time", "")).strip()
            end = str(payload.get("end_time", "")).strip()
            filename = str(payload.get("filename", "")).strip() or "clip"

            # URL doğrulama
            if not _extract_youtube_id(url):
                self._set_headers(400)
                self.wfile.write(json.dumps({
                    "ok": False, "error": "Geçersiz YouTube URL",
                    "error_code": "INVALID_URL",
                }).encode())
                return

            # Zaman doğrulama
            if not _valid_hhmmss(start):
                self._set_headers(400)
                self.wfile.write(json.dumps({
                    "ok": False, "error": "Başlangıç zamanı geçersiz (HH:MM:SS formatı bekleniyor)",
                    "error_code": "INVALID_START",
                }).encode())
                return

            if not _valid_hhmmss(end):
                self._set_headers(400)
                self.wfile.write(json.dumps({
                    "ok": False, "error": "Bitiş zamanı geçersiz (HH:MM:SS formatı bekleniyor)",
                    "error_code": "INVALID_END",
                }).encode())
                return

            start_sec = _hhmmss_to_seconds(start)
            end_sec = _hhmmss_to_seconds(end)
            if start_sec >= end_sec:
                self._set_headers(400)
                self.wfile.write(json.dumps({
                    "ok": False, "error": "Başlangıç zamanı bitiş zamanından önce olmalı",
                    "error_code": "INVALID_RANGE",
                }).encode())
                return

            # İş oluştur
            job_id = uuid.uuid4().hex[:14]
            with _jobs_lock:
                _jobs[job_id] = {
                    "job_id": job_id,
                    "status": "pending",
                    "progress": 0,
                    "message": "Kuyruğa alındı…",
                    "clip_path": None,
                    "download_url": None,
                    "error_code": None,
                    "details": None,
                }

            # Arka planda işle
            t = threading.Thread(
                target=_process_clip_job,
                args=(job_id, url, start, end, filename),
                daemon=True,
            )
            t.start()

            LOGGER.info("Clip job %s kuyruğa alındı: url=%s %s→%s", job_id, url, start, end)

            self._set_headers(200)
            self.wfile.write(json.dumps({
                "ok": True,
                "job_id": job_id,
                "status": "pending",
                "message": "İş başlatıldı",
            }).encode("utf-8"))

        except Exception as e:
            LOGGER.error("download_clip handler hatası: %s", e)
            self._set_headers(500)
            self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))

    # ── GET /api/jobs/:id ─────────────────────────────────────────────────────

    def _handle_job_status(self) -> None:
        """İş durumunu döndürür: GET /api/jobs/<job_id>"""
        try:
            # Path: /api/jobs/<job_id>
            parts = [p for p in self.path.split("/") if p]
            if len(parts) < 3:
                self._set_headers(400)
                self.wfile.write(json.dumps({"error": "job_id gerekli"}).encode())
                return

            job_id = parts[2]

            with _jobs_lock:
                job = dict(_jobs.get(job_id, {}))

            if not job:
                self._set_headers(404)
                self.wfile.write(json.dumps({"error": "İş bulunamadı"}).encode())
                return

            self._set_headers(200)
            self.wfile.write(json.dumps({"ok": True, **job}).encode("utf-8"))

        except Exception as e:
            LOGGER.error("job_status handler hatası: %s", e)
            self._set_headers(500)
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))

    # ── GET /api/clips/:name/download ─────────────────────────────────────────

    def _handle_clip_download(self) -> None:
        """Klip dosyasını Content-Disposition: attachment ile indirir."""
        try:
            # Path: /api/clips/<filename>/download
            parts = [p for p in self.path.split("/") if p]
            # parts: ['api', 'clips', 'FILENAME', 'download']
            if len(parts) < 4:
                self._set_headers(400)
                self.wfile.write(json.dumps({"error": "Geçersiz istek"}).encode())
                return

            filename = parts[2]

            # Güvenlik: path traversal engelle
            if ".." in filename or "/" in filename or "\\" in filename:
                self._set_headers(400)
                self.wfile.write(json.dumps({"error": "Geçersiz dosya adı"}).encode())
                return

            file_path = CLIPS_DIR / filename
            if not file_path.exists() or not file_path.is_file():
                self._set_headers(404)
                self.wfile.write(json.dumps({"error": "Dosya bulunamadı"}).encode())
                return

            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(file_path.stat().st_size))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            with open(file_path, "rb") as f:
                while chunk := f.read(1024 * 256):
                    self.wfile.write(chunk)

        except Exception as e:
            LOGGER.error("clip_download handler hatası: %s", e)
            self._set_headers(500)
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))


# ── Server ────────────────────────────────────────────────────────────────────

def run(host: str = "127.0.0.1", port: int = 8001) -> None:
    setup_logging()
    load_environment()
    server = ThreadingHTTPServer((host, port), PreviewHandler)
    LOGGER.info("API sunucusu başlatıldı: http://%s:%s", host, port)
    server.serve_forever()


if __name__ == "__main__":
    run()
