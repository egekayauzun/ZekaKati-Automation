from __future__ import annotations

import os
import re
from typing import List

import requests
from pytrends.request import TrendReq

from .logger import LOGGER

REDDIT_HEADERS = {
    "User-Agent": "YoutubeOtomation/1.0 (local script; not a bot index)",
    "Accept": "application/json",
}
DEFAULT_REDDIT_SUBS = "turkey,TurkeyJerky,worldnews"

# Kategori → subreddit eşlemesi.
# Her kategori için env değişkeni ile override edilebilir: TREND_CATEGORY_SUBS_<KATEGORI_BÜYÜK>
CATEGORY_SUBREDDITS: dict[str, list[str]] = {
    "gundem":    ["turkey", "TurkeyJerky"],
    "teknoloji": ["Turkey", "teknoloji", "TurkeyJerky"],
    "spor":      ["superlig", "galatasaray", "besiktas", "FenerbahceSK", "turkey"],
    "saglik":    ["Turkey", "turkey"],
    "bilim":     ["Turkey", "bilim", "turkey"],
    "ekonomi":   ["Turkey", "TurkeyJerky", "turkey"],
}


class TrendService:
    """Trend konularını çeker: önce Reddit sıcak başlıkları, olmazsa Google Trends."""

    def __init__(self, hl: str = "tr-TR", tz: int = 180) -> None:
        try:
            self.client = TrendReq(hl=hl, tz=tz)
        except Exception as e:
            LOGGER.error("Trend service initialization failed: %s", e)
            raise

    @staticmethod
    def _normalize_keywords(values: list[object], limit: int) -> List[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            keyword = str(value).strip()
            if not keyword:
                continue
            lowered = keyword.casefold()
            if lowered in seen:
                continue
            seen.add(lowered)
            result.append(keyword)
            if len(result) >= limit:
                break
        return result

    @staticmethod
    def _title_for_topic(title: str) -> str:
        t = re.sub(r"\[.*?\]", " ", title).replace("\n", " ")
        t = re.sub(r"\s+", " ", t).strip()
        if len(t) > 140:
            t = t[:140].rsplit(" ", 1)[0] + "…"
        return t

    @staticmethod
    def _is_turkish_like(text: str) -> bool:
        t = text.strip().lower()
        if not t:
            return False
        if re.search(r"[çğıöşü]", t):
            return True
        # Common Turkish stop words / suffix fragments.
        hints = (
            " ve ", " ile ", " için ", " ama ", " ancak ", "son dakika", "türkiye", "gündem",
            " nasıl ", " neden ", " bugün ", " yarın ", " yeni ", " olacak", " açıklama",
        )
        if any(h in f" {t} " for h in hints):
            return True
        return False

    def _subreddits_for_category(self, category: str | None) -> list[str]:
        """Kategoriye göre subreddit listesi döndürür. Env override destekler."""
        if category:
            key = category.strip().lower()
            # Env override: TREND_CATEGORY_SUBS_TEKNOLOJI gibi
            env_key = f"TREND_CATEGORY_SUBS_{key.upper()}"
            env_val = os.getenv(env_key, "").strip()
            if env_val:
                return [s.strip() for s in env_val.split(",") if s.strip()][:6]
            if key in CATEGORY_SUBREDDITS:
                return CATEGORY_SUBREDDITS[key]
        # Varsayılan subredditler
        subs_raw = (os.getenv("REDDIT_TREND_SUBS") or DEFAULT_REDDIT_SUBS).strip()
        return [s.strip() for s in subs_raw.split(",") if s.strip()][:6]

    def _from_reddit(self, limit: int, category: str | None = None) -> list[str]:
        """
        Sıcak (hot) post başlıklarını alır.
        category parametresi ile kategori odaklı subreddit seti kullanılır.
        """
        subreddits = self._subreddits_for_category(category)
        collected: list[str] = []
        fallback_non_tr: list[str] = []
        for sub in subreddits:
            if len(collected) >= limit:
                break
            url = f"https://www.reddit.com/r/{sub}/hot.json"
            try:
                r = requests.get(
                    url,
                    params={"limit": 15, "raw_json": 1},
                    headers=REDDIT_HEADERS,
                    timeout=18,
                )
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                LOGGER.warning("Reddit r/%s okunamadı: %s", sub, e)
                continue
            children = (data.get("data") or {}).get("children") or []
            for child in children:
                if len(collected) >= limit:
                    break
                p = (child or {}).get("data") or {}
                if p.get("stickied") or p.get("over_18"):
                    continue
                title = p.get("title")
                if not title or not str(title).strip():
                    continue
                topic = self._title_for_topic(str(title).strip())
                if len(topic) < 8:
                    continue
                if self._is_turkish_like(topic):
                    collected.append(topic)
                else:
                    fallback_non_tr.append(topic)
        if collected:
            LOGGER.info(
                "Reddit'ten %s konu (kategori=%s): %s",
                len(collected), category or "varsayılan", collected[:3],
            )
            return self._normalize_keywords(collected, limit=limit)
        if fallback_non_tr:
            LOGGER.info(
                "Reddit'te Türkçe başlık az bulundu (kategori=%s), Google Trends fallback kullanılacak.",
                category or "varsayılan",
            )
        return []

    def top_trends(
        self,
        country: str = "turkey",
        limit: int = 5,
        category: str | None = None,
        source_preference: str = "reddit_first",
    ) -> List[str]:
        """
        Trend konuları döndürür.

        Önce Reddit'i dener; boş veya erişilemez ise Google Trends'e geçer.
        category parametresi ile kategori odaklı subreddit seti kullanılır.
        """
        def _google_topics() -> list[str]:
            try:
                data = self.client.trending_searches(pn=country)
                keywords = data.iloc[:, 0].dropna().astype(str).tolist()
                normalized = self._normalize_keywords(keywords, limit=limit)
                if normalized:
                    return normalized
            except Exception as e:
                LOGGER.warning("trending_searches failed for '%s': %s", country, e)

            for realtime_country in ("TR", "turkey", "US"):
                try:
                    realtime_data = self.client.realtime_trending_searches(pn=realtime_country)
                    for column_name in ("title", "entityNames"):
                        if column_name in realtime_data.columns:
                            keywords = realtime_data[column_name].dropna().astype(str).tolist()
                            normalized = self._normalize_keywords(keywords, limit=limit)
                            if normalized:
                                return normalized
                except Exception as e:
                    LOGGER.warning(
                        "realtime_trending_searches failed for '%s' (requested: '%s'): %s",
                        realtime_country,
                        country,
                        e,
                    )
            return []

        pref = (source_preference or "reddit_first").strip().lower()
        if pref not in {"reddit_first", "google_first", "reddit_only", "google_only"}:
            pref = "reddit_first"

        if pref == "reddit_only":
            return self._from_reddit(limit=limit, category=category)
        if pref == "google_only":
            return _google_topics()
        if pref == "google_first":
            g = _google_topics()
            if g:
                return g
            LOGGER.warning("Google Trends boş, Reddit'e geçiliyor (kategori=%s)", category or "belirtilmedi")
            return self._from_reddit(limit=limit, category=category)

        # default: reddit_first
        reddit_topics = self._from_reddit(limit=limit, category=category)
        if reddit_topics:
            return reddit_topics
        LOGGER.warning("Reddit boş veya erişilemez, Google Trends'e geçiliyor (kategori=%s)", category or "belirtilmedi")
        g = _google_topics()
        if g:
            return g

        LOGGER.error(
            "Tüm kaynaklardan trend alınamadı (country=%s, category=%s).",
            country, category or "belirtilmedi",
        )
        return []
