"""Thin async AO3 client: search + work metadata + full-text download.

No official API exists, so this scrapes AO3's (very clean) HTML.
Politeness rules: one request at a time, min interval between requests,
honor Retry-After on 429. AO3 is volunteer-run — do not hammer it.

Transport is curl_cffi impersonating iOS Safari: AO3 sits behind a Cloudflare
managed challenge that blocks plain HTTP clients and desktop-browser TLS
fingerprints, but (as of 2026-07) waves the mobile Safari fingerprint through.
If requests start returning 403 with a `cf-mitigated: challenge` header,
try a different `IMPERSONATE` target.
"""

import asyncio
import json
import re
import time
from pathlib import Path

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession

BASE = "https://archiveofourown.org"
IMPERSONATE = "safari_ios"
CACHE_DIR = Path(__file__).parent / ".cache"
CACHE_TTL = 24 * 3600  # fic text barely changes; WIP updates show up next day

RATING_IDS = {
    "not rated": "9",
    "general": "10",
    "teen": "11",
    "mature": "12",
    "explicit": "13",
}

CATEGORY_IDS = {
    "gen": "21",
    "f/m": "22",
    "m/m": "23",
    "other": "24",
    "f/f": "116",
    "multi": "2246",
}

SORT_COLUMNS = {
    "relevance": "_score",
    "kudos": "kudos_count",
    "hits": "hits",
    "comments": "comments_count",
    "bookmarks": "bookmarks_count",
    "words": "word_count",
    "date_updated": "revised_at",
    "date_posted": "created_at",
}

MAX_PAGES = 5

# Transient failures worth one retry. 52x are Cloudflare<->origin errors
# (525 = SSL handshake failed, 522/524 = origin timeout) — AO3 under load, not us.
RETRYABLE_STATUS = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 525}
RETRY_WAIT = 5.0      # seconds between the two attempts
MAX_ATTEMPTS = 2      # try, wait 5s, try once more, then give up


def _num(text: str) -> int:
    try:
        return int(text.replace(",", "").strip())
    except (ValueError, AttributeError):
        return 0


class AO3Client:
    def __init__(self, min_interval: float = 0.6):
        self._client = AsyncSession(impersonate=IMPERSONATE, timeout=30)
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self.min_interval = min_interval

    async def close(self):
        await self._client.close()

    async def _get(self, url: str, params: dict | list | None = None) -> str:
        """Serialized, throttled GET. Retries ONCE (5s gap) on a transient
        failure — network/timeout error, 429, or a 5xx incl. Cloudflare 52x
        (525 SSL handshake, 522/524 origin timeout) — then gives up rather than
        hanging. A Cloudflare bot-challenge (403) is NOT retried: it needs a
        fingerprint change, not a wait."""
        last_error = "unknown error"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            resp = None
            async with self._lock:
                wait = self._last_request + self.min_interval - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    resp = await self._client.get(url, params=params)
                except Exception as exc:  # curl timeout / transient network error
                    last_error = f"network/timeout error ({type(exc).__name__})"
                self._last_request = time.monotonic()

            if resp is None:
                if attempt < MAX_ATTEMPTS:
                    await asyncio.sleep(RETRY_WAIT)
                    continue
                raise RuntimeError(f"AO3 unreachable after {MAX_ATTEMPTS} tries: {last_error}")

            if resp.status_code == 403 and resp.headers.get("cf-mitigated") == "challenge":
                raise RuntimeError(
                    "Cloudflare challenged this request — AO3 tightened bot rules. "
                    f"Try changing IMPERSONATE (currently {IMPERSONATE!r}) in ao3.py."
                )

            if resp.status_code in RETRYABLE_STATUS:
                last_error = f"HTTP {resp.status_code} from AO3/Cloudflare"
                if attempt < MAX_ATTEMPTS:
                    wait_s = RETRY_WAIT
                    if resp.status_code == 429:  # honor Retry-After, but stay snappy
                        wait_s = min(int(resp.headers.get("retry-after", "5") or "5"), 60)
                    await asyncio.sleep(wait_s)
                    continue
                raise RuntimeError(f"AO3 request failed after {MAX_ATTEMPTS} tries: {last_error}")

            resp.raise_for_status()
            return resp.text
        raise RuntimeError(f"AO3 request failed: {last_error}")

    # ------------------------------------------------------------- search

    async def search(
        self,
        query: str | None = None,
        title: str | None = None,
        author: str | None = None,
        fandom: str | None = None,
        relationship: str | None = None,
        character: str | None = None,
        tags: str | None = None,
        rating: str | None = None,
        categories: str | None = None,
        complete_only: bool = False,
        word_count: str | None = None,
        crossovers: bool | None = None,
        language: str | None = None,
        sort_by: str = "relevance",
        page: int = 1,
        pages: int = 1,
    ) -> dict:
        """Search works. Each page is 20 results; fetches `pages` pages starting
        at `page` (capped at MAX_PAGES per call)."""
        pages = max(1, min(pages, MAX_PAGES))
        start_page = max(1, page)
        params: dict[str, str] = {"work_search[sort_direction]": "desc"}

        def put(key: str, value: str | None):
            if value:
                params[f"work_search[{key}]"] = value

        put("query", query)
        put("title", title)
        put("creators", author)
        put("fandom_names", fandom)
        put("relationship_names", relationship)
        put("character_names", character)
        put("freeform_names", tags)
        put("word_count", word_count)
        put("language_id", language)
        if rating:
            put("rating_ids", RATING_IDS.get(rating.lower().strip(), rating))
        if complete_only:
            put("complete", "T")
        if crossovers is not None:
            put("crossover", "T" if crossovers else "F")
        put("sort_column", SORT_COLUMNS.get(sort_by, "_score"))

        # category_ids is a repeated key — needs a list of tuples, not a dict
        param_pairs = list(params.items())
        for cat in (categories or "").split(","):
            cat_id = CATEGORY_IDS.get(cat.strip().lower())
            if cat_id:
                param_pairs.append(("work_search[category_ids][]", cat_id))

        results: list[dict] = []
        total = None
        for p in range(start_page, start_page + pages):
            html = await self._get(
                f"{BASE}/works/search", params=[*param_pairs, ("page", str(p))]
            )
            page_results, total = self._parse_search_page(html)
            results.extend(page_results)
            if not page_results:
                break
        return {
            "total_found": total,
            "returned": len(results),
            "start_page": start_page,
            "works": results,
        }

    async def autocomplete(self, term: str, kind: str = "tag") -> list[str]:
        """Resolve fuzzy wording to canonical AO3 tag names via the live
        autocomplete endpoint. kind: tag | fandom | relationship | character."""
        if kind not in ("tag", "fandom", "relationship", "character"):
            kind = "tag"
        text = await self._get(
            f"{BASE}/autocomplete/{kind}.json", params={"term": term}
        )
        return [item["name"] for item in json.loads(text)]

    def _parse_search_page(self, html: str) -> tuple[list[dict], int | None]:
        soup = BeautifulSoup(html, "html.parser")
        total = None
        heading = soup.select_one("h3.heading")
        if heading:
            m = re.search(r"([\d,]+)\s+Found", heading.get_text())
            if m:
                total = _num(m.group(1))

        works = []
        for blurb in soup.select("li.work.blurb.group"):
            works.append(self._parse_blurb(blurb))
        return works, total

    def _parse_blurb(self, blurb) -> dict:
        title_a = blurb.select_one("h4.heading a[href^='/works/']")
        work_id = ""
        if title_a:
            m = re.search(r"/works/(\d+)", title_a["href"])
            work_id = m.group(1) if m else ""
        authors = [a.get_text(strip=True) for a in blurb.select("h4.heading a[rel='author']")]
        fandoms = [a.get_text(strip=True) for a in blurb.select("h5.fandoms a.tag")]

        rating_el = blurb.select_one("ul.required-tags span.rating")
        category_el = blurb.select_one("ul.required-tags span.category")
        warnings = [a.get_text(strip=True) for a in blurb.select("li.warnings a.tag")]
        relationships = [a.get_text(strip=True) for a in blurb.select("li.relationships a.tag")]
        characters = [a.get_text(strip=True) for a in blurb.select("li.characters a.tag")]
        freeforms = [a.get_text(strip=True) for a in blurb.select("li.freeforms a.tag")]

        summary_el = blurb.select_one("blockquote.userstuff.summary")
        date_el = blurb.select_one("p.datetime")

        def stat(cls: str) -> str:
            el = blurb.select_one(f"dd.{cls}")
            return el.get_text(strip=True) if el else ""

        chapters = stat("chapters")
        return {
            "work_id": work_id,
            "title": title_a.get_text(strip=True) if title_a else "",
            "authors": authors or ["Anonymous"],
            "fandoms": fandoms,
            "rating": rating_el.get_text(strip=True) if rating_el else "",
            "category": category_el.get_text(strip=True) if category_el else "",
            "warnings": warnings,
            "relationships": relationships,
            "characters": characters[:8],
            "tags": freeforms[:12],
            "summary": summary_el.get_text(" ", strip=True) if summary_el else "",
            "language": stat("language"),
            "words": _num(stat("words")),
            "chapters": chapters,
            "complete": bool(re.match(r"(\d+)/\1$", chapters)),
            "kudos": _num(stat("kudos")),
            "hits": _num(stat("hits")),
            "updated": date_el.get_text(strip=True) if date_el else "",
            "url": f"{BASE}/works/{work_id}",
        }

    # --------------------------------------------------------------- work

    async def work_meta(self, work_id: str) -> dict:
        html = await self._get(f"{BASE}/works/{work_id}", params={"view_adult": "true"})
        soup = BeautifulSoup(html, "html.parser")

        def tags_of(cls: str) -> list[str]:
            return [a.get_text(strip=True) for a in soup.select(f"dd.{cls}.tags a.tag")]

        def stat(cls: str) -> str:
            el = soup.select_one(f"dl.stats dd.{cls}")
            return el.get_text(strip=True) if el else ""

        title_el = soup.select_one("h2.title")
        authors = [a.get_text(strip=True) for a in soup.select("h3.byline a[rel='author']")]
        summary_el = soup.select_one("div.summary blockquote.userstuff")
        series_el = soup.select_one("dd.series span.position")
        chapters = stat("chapters")
        return {
            "work_id": work_id,
            "title": title_el.get_text(strip=True) if title_el else "",
            "authors": authors or ["Anonymous"],
            "rating": ", ".join(tags_of("rating")),
            "warnings": tags_of("warning"),
            "fandoms": tags_of("fandom"),
            "relationships": tags_of("relationship"),
            "characters": tags_of("character"),
            "tags": tags_of("freeform"),
            "language": stat("language") or (soup.select_one("dd.language").get_text(strip=True) if soup.select_one("dd.language") else ""),
            "series": series_el.get_text(" ", strip=True) if series_el else None,
            "published": stat("published"),
            "updated": stat("status"),
            "words": _num(stat("words")),
            "chapters": chapters,
            "complete": bool(re.match(r"(\d+)/\1$", chapters)),
            "kudos": _num(stat("kudos")),
            "bookmarks": _num(stat("bookmarks")),
            "hits": _num(stat("hits")),
            "summary": summary_el.get_text(" ", strip=True) if summary_el else "",
            "url": f"{BASE}/works/{work_id}",
        }

    async def download_text(self, work_id: str) -> dict:
        """Fetch the full work text via AO3's single-file HTML download endpoint.

        Results are cached on disk for CACHE_TTL so repeat reads (new query,
        same fic) skip AO3 entirely.
        """
        cache_file = CACHE_DIR / f"{work_id}.json"
        if cache_file.exists() and time.time() - cache_file.stat().st_mtime < CACHE_TTL:
            try:
                return json.loads(cache_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass  # corrupt/unreadable cache entry — refetch

        html = await self._get(f"{BASE}/downloads/{work_id}/work.html")
        soup = BeautifulSoup(html, "html.parser")

        preface = soup.select_one("#preface")
        title = ""
        byline = ""
        preface_meta: dict[str, str] = {}
        if preface:
            h1 = preface.select_one("h1")
            title = h1.get_text(strip=True) if h1 else ""
            by = preface.select_one("div.byline")
            byline = by.get_text(strip=True) if by else ""
            # The download preface carries the full tag block (rating, warnings,
            # fandom, ships, tags, stats) — saves a separate /works/{id} request.
            for dl in preface.select("dl.tags"):
                for dt, dd in zip(dl.find_all("dt"), dl.find_all("dd")):
                    key = dt.get_text(strip=True).rstrip(":")
                    preface_meta[key] = dd.get_text(" ", strip=True)

        chapters_div = soup.find("div", id="chapters") or soup.body or soup
        parts = []
        for el in chapters_div.find_all(["h2", "p", "blockquote", "li", "hr"]):
            if el.name == "hr":
                parts.append("---")
            else:
                text = el.get_text(" ", strip=True)
                if text:
                    parts.append(text)
        full_text = "\n\n".join(parts)
        result = {
            "work_id": work_id,
            "title": title,
            "byline": byline,
            "meta": preface_meta,
            "text": full_text,
            "char_count": len(full_text),
        }
        try:
            CACHE_DIR.mkdir(exist_ok=True)
            cache_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass  # cache is best-effort
        return result
