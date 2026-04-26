from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import requests

from .logger import LOGGER
from .utils import ensure_directories, require_env_var


class VideoFetcher:
    """Fetch portrait stock videos from Pexels and save locally."""

    def __init__(self, base_url: str | None = None, per_page: int = 20) -> None:
        self.api_key = require_env_var("PEXELS_API_KEY")
        self.base_url = base_url or "https://api.pexels.com/videos/search"
        self.per_page = per_page
        self.videos_dir = Path("assets/videos")
        ensure_directories([self.videos_dir])

    @staticmethod
    def _pick_video_file(video_files: list[dict[str, Any]]) -> str | None:
        mp4_files = [vf for vf in video_files if vf.get("file_type") == "video/mp4"]
        if not mp4_files:
            return None
        sorted_by_quality = sorted(
            mp4_files,
            key=lambda x: ((x.get("width") or 0) * (x.get("height") or 0)),
            reverse=True,
        )
        return sorted_by_quality[0].get("link")

    @staticmethod
    def _shorten_query(query: str, max_words: int = 8) -> str:
        cleaned = re.sub(r"[,;:]+", " ", query)
        words = [w for w in cleaned.split() if w]
        if len(words) <= max_words:
            return " ".join(words)
        return " ".join(words[:max_words])

    @staticmethod
    def _dedupe_queries(queries: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for q in queries:
            key = q.strip().casefold()
            if not key or key in seen:
                continue
            seen.add(key)
            result.append(q.strip())
        return result

    def _target_path(self, output_path: str) -> Path:
        candidate = Path(output_path)
        filename = candidate.name or "scene_1.mp4"
        if candidate.suffix.lower() != ".mp4":
            filename = f"{candidate.stem}.mp4"
        return self.videos_dir / filename

    def _search_videos(
        self,
        query: str,
        *,
        orientation: str | None,
        size: str,
    ) -> list[dict[str, Any]]:
        params: dict[str, str | int] = {
            "query": query,
            "size": size,
            "per_page": self.per_page,
        }
        if orientation:
            params["orientation"] = orientation
        headers = {"Authorization": self.api_key}
        response = requests.get(self.base_url, params=params, headers=headers, timeout=40)
        response.raise_for_status()
        payload = response.json()
        return payload.get("videos", [])

    def _pick_link_from_results(
        self,
        videos: list[dict[str, Any]],
        min_duration: int,
        max_duration: int,
    ) -> str | None:
        for video in videos:
            duration = int(video.get("duration") or 0)
            if min_duration <= duration <= max_duration:
                selected = self._pick_video_file(video.get("video_files", []))
                if selected:
                    return selected
        for video in videos:
            duration = int(video.get("duration") or 0)
            if duration >= 3:
                selected = self._pick_video_file(video.get("video_files", []))
                if selected:
                    return selected
        return None

    def fetch_video(
        self,
        query: str,
        output_path: str,
        fallback_queries: list[str] | None = None,
        target_scene_duration: float | None = None,
        short_project_mode: bool = False,
    ) -> Path:
        """
        Download one stock clip. Tries shortened queries, relaxed filters, and fallbacks
        before failing (long image_prompts often return no strict portrait 5–10s hits).
        """
        output_file = self._target_path(output_path)
        headers = {"Authorization": self.api_key}

        short = self._shorten_query(query, max_words=6)
        shorter = self._shorten_query(query, max_words=4)
        extra = fallback_queries or []
        all_queries = self._dedupe_queries(
            [query, short, shorter, *extra, "technology abstract", "city night vertical"]
        )

        scene_dur = float(target_scene_duration or 8.0)
        if scene_dur <= 0:
            scene_dur = 8.0
        soft_max = max(8, int(round(scene_dur * 1.8)))
        hard_max = max(12, int(round(scene_dur * 2.2)))

        attempts: list[tuple[str | None, str, int, int]]
        if short_project_mode:
            # Fast profile for short videos: avoid very long clips / broad fallback.
            attempts = [
                ("portrait", "medium", 3, soft_max),
                ("portrait", "large", 3, hard_max),
                (None, "medium", 3, hard_max),
            ]
        else:
            attempts = [
                ("portrait", "large", 4, 12),
                ("portrait", "large", 3, max(20, hard_max)),
                ("portrait", "medium", 3, max(25, hard_max)),
                (None, "large", 3, 60),
                (None, "medium", 3, 60),
            ]

        last_error: Exception | None = None
        for q in all_queries:
            for orientation, size, min_d, max_d in attempts:
                try:
                    videos = self._search_videos(q, orientation=orientation, size=size)
                    selected_link = self._pick_link_from_results(videos, min_d, max_d)
                    if not selected_link:
                        continue

                    with requests.get(selected_link, stream=True, timeout=90) as download_response:
                        download_response.raise_for_status()
                        with output_file.open("wb") as file_handle:
                            for chunk in download_response.iter_content(chunk_size=1024 * 128):
                                if chunk:
                                    file_handle.write(chunk)

                    LOGGER.info(
                        "Downloaded stock video: %s (query=%r orientation=%s size=%s)",
                        output_file,
                        q[:80] + ("…" if len(q) > 80 else ""),
                        orientation or "any",
                        size,
                    )
                    return output_file
                except requests.HTTPError as e:
                    details = ""
                    if e.response is not None:
                        details = e.response.text[:500]
                    LOGGER.warning("Pexels HTTP for query %r: %s", q[:60], details or e)
                    last_error = e
                except requests.RequestException as e:
                    LOGGER.warning("Pexels request error for query %r: %s", q[:60], e)
                    last_error = e
                except Exception as e:
                    LOGGER.warning("Pexels attempt failed for query %r: %s", q[:60], e)
                    last_error = e

        LOGGER.error("No suitable video found on Pexels after fallbacks (original query: %s)", query[:120])
        if last_error:
            raise RuntimeError(f"No suitable stock video found for scene after fallbacks: {query[:100]}") from last_error
        raise RuntimeError(f"No suitable stock video found for scene after fallbacks: {query[:100]}")
