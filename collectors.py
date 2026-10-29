"""수집 모듈 — 경로가 다른 소스(RSS·Atom·GitHub Releases·HN API·arXiv API)를
같은 모양의 기사 dict 로 바꾸고, 시간 창·제외 도메인·중복을 걸러낸다.

기사 dict 키: id, title, url, source, source_id, route, tier, lang,
              published_at(ISO 문자열 또는 None), summary(≤600자), inline_body(≤8000자)
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser

TRACKING_PARAMS = {"ref", "ref_src", "source", "fbclid", "gclid", "mc_cid", "mc_eid"}
SUMMARY_MAX, INLINE_MAX = 600, 8000
FUTURE_TOLERANCE = timedelta(minutes=5)   # 이보다 미래 날짜는 시계 오류로 보고 버린다(강의 코드에는 상한이 없었다)


def strip_tags(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def normalize_url(url: str) -> str:
    """추적용 꼬리표와 조각(#)만 떼고 비교용 주소를 만든다. 기사를 가르는 쿼리(?id=)는 남긴다."""
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def article_id(url: str) -> str:
    return hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()[:10]


def host_excluded(url: str, excluded_hosts: list[str]) -> bool:
    host = urlsplit(url).netloc.lower()
    return any(host == h or host.endswith("." + h) for h in excluded_hosts)


def _iso(struct_time) -> str | None:
    if not struct_time:
        return None
    return datetime(*struct_time[:6], tzinfo=timezone.utc).isoformat()


def _make(source: dict, title: str, url: str, published: str | None, summary: str, inline: str) -> dict:
    return {
        "id": article_id(url), "title": strip_tags(title), "url": url.strip(),
        "source": source["name"], "source_id": source["id"], "route": source["route"],
        "tier": source.get("tier", 2), "lang": source.get("lang", "en"),
        "published_at": published, "summary": summary[:SUMMARY_MAX], "inline_body": inline[:INLINE_MAX],
    }


def parse_feed(content: bytes, source: dict) -> list[dict]:
    """RSS·Atom·GitHub Releases·arXiv(Atom)를 읽는다."""
    items = []
    for e in feedparser.parse(content).entries:
        link = e.get("link", "")
        if not link:
            continue
        summary = strip_tags(e.get("summary", ""))
        body = strip_tags(e["content"][0].get("value", "")) if e.get("content") else ""
        inline = body or (summary if source["route"] == "api_arxiv" else "")   # arXiv 는 초록이 곧 본문이다
        items.append(_make(source, e.get("title", ""), link,
                           _iso(e.get("published_parsed") or e.get("updated_parsed")), summary, inline))
    return items


def parse_hn(content: bytes, source: dict) -> list[dict]:
    """Hacker News Algolia API. 외부 글이 없는 항목(Ask HN 등)은 본문이 없어 뺀다."""
    items = []
    for hit in json.loads(content).get("hits", []):
        if not hit.get("url") or not hit.get("title"):
            continue
        created = hit.get("created_at_i")
        published = datetime.fromtimestamp(created, tz=timezone.utc).isoformat() if created else None
        points = hit.get("points") or 0
        items.append(_make(source, hit["title"], hit["url"], published,
                           f"Hacker News {points}점 · 댓글 {hit.get('num_comments') or 0}개", ""))
    return items


def parse_content(content: bytes, source: dict) -> list[dict]:
    return parse_hn(content, source) if source["route"] == "api_hn" else parse_feed(content, source)


def _dedupe(items: list[dict]) -> tuple[list[dict], int]:
    """같은 주소·같은 제목을 한 건으로 줄인다. 우선순위는 1차 소스(tier 낮음), 그다음 최신."""
    def sort_key(it):
        ts = datetime.fromisoformat(it["published_at"]).timestamp() if it["published_at"] else 0
        return (it["tier"], -ts)

    seen_urls, seen_titles, kept = set(), set(), []
    for it in sorted(items, key=sort_key):
        url_key = normalize_url(it["url"])
        title_key = re.sub(r"\W+", "", it["title"].casefold())
        if url_key in seen_urls or (title_key and title_key in seen_titles):
            continue
        seen_urls.add(url_key)
        seen_titles.add(title_key)
        kept.append(it)
    return kept, len(items) - len(kept)


def collect(collect_cfg: dict, http, now: datetime, hours: int) -> tuple[list[dict], dict]:
    """모든 소스를 돌아 시간 창 안의 기사를 모은다. (기사 목록, 소스별 통계)를 돌려준다.

    한 소스가 실패해도 나머지는 계속한다. 실패는 통계의 error 에 남겨 '조용히 빠지는' 일을 막는다.
    """
    cutoff, upper = now - timedelta(hours=hours), now + FUTURE_TOLERANCE
    excluded, cap = collect_cfg.get("excluded_hosts", []), collect_cfg["per_source_cap"]
    pooled, stats = [], {}
    for src in collect_cfg["sources"]:
        st = {"status": None, "fetched": 0, "undated": 0, "in_window": 0, "excluded_host": 0,
              "kept": 0, "error": None}
        stats[src["id"]] = st
        fetched = []
        for url in src.get("urls") or [src["url"]]:
            resp = http.get(url)
            st["status"] = resp["status"]
            if resp["status"] != 200:
                st["error"] = resp["error"] or f"HTTP {resp['status']}"
                continue
            try:
                fetched += parse_content(resp["content"], src)
            except Exception as exc:                       # 파싱 오류도 소스 단위로 가둔다
                st["error"] = f"parse_error: {type(exc).__name__}"
        st["fetched"] = len(fetched)
        in_window = []
        for it in fetched:
            if not it["published_at"]:
                st["undated"] += 1
                continue
            when = datetime.fromisoformat(it["published_at"])
            if not (cutoff <= when <= upper):
                continue
            if host_excluded(it["url"], excluded):
                st["excluded_host"] += 1
                continue
            in_window.append(it)
        st["in_window"] = len(in_window)
        in_window.sort(key=lambda it: it["published_at"], reverse=True)
        pooled += in_window[:cap]
    kept, dup = _dedupe(pooled)
    for it in kept:
        stats[it["source_id"]]["kept"] += 1
    stats["_dedupe_removed"] = dup
    return kept, stats
