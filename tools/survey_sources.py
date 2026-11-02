"""소스 후보 실측 스크립트.

읽기 전용 HTTP GET 만 보낸다. 후보마다 응답 상태·발행량·최신성·본문 추출률·
robots.txt 허용 여부를 재서 store/source_survey.json 에 저장한다.
채택·탈락은 이 숫자를 보고 사람이 정하고, 관문(G1~G3) 판정은 참고용으로 함께 계산한다.

사용법
  python tools/survey_sources.py --dry            # 네트워크 없이 후보와 예상 요청 수만 출력
  python tools/survey_sources.py                  # 전체 실측
  python tools/survey_sources.py --only hf_blog   # 일부만 다시 재고 기존 결과에 합친다
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

from urllib.parse import urlparse

import feedparser

import trafilatura
import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from net import (Http as GuardedHttp, Robots, BODY_MAX_BYTES, BODY_CONTENT_TYPES,
                 _url_error, _robots_verdict)
CANDIDATES_FILE = ROOT / "tools" / "candidates.yaml"
OUTPUT_FILE = ROOT / "store" / "source_survey.json"

USER_AGENT = "newsletter-agent-course/0.1 (personal learning project)"
TIMEOUT_S = 20
REQUEST_GAP_S = 0.4          # 연속 요청이 한꺼번에 몰리지 않게 두는 간격
MIN_BODY_CHARS = 600         # 강의 3강의 G1 기준선과 같은 값 (>= 로 통일)
BODY_SAMPLES = 3             # 소스마다 본문을 확인할 최근 글 수
MIN_ITEMS_7D = 3             # G2: 최근 7일에 최소 이 정도는 올라와야 '살아 있는' 소스로 본다
G1_MIN_RATIO = 2 / 3         # G1: 표본 중 이 비율 이상에서 본문 추출 성공
FUTURE_TOLERANCE = timedelta(minutes=5)   # 시계 오차 허용. 이보다 미래 날짜는 따로 센다
KST = timezone(timedelta(hours=9))
INLINE_BODY_ROUTES = {"api_arxiv"}        # 초록(abstract)이 응답에 이미 들어 있는 경로


# ── HTTP ────────────────────────────────────────────────────────────────────
class Http(GuardedHttp):
    """운영 보호 정책을 그대로 쓰고 조사 결과의 bytes 필드만 덧붙인다."""

    def __init__(self, **kwargs) -> None:
        super().__init__(USER_AGENT, timeout_s=kwargs.pop("timeout_s", TIMEOUT_S),
                         gap_s=kwargs.pop("gap_s", REQUEST_GAP_S), **kwargs)

    def get(self, url: str, **policy) -> dict:
        result = super().get(url, **policy)
        result["bytes"] = len(result["content"])
        return result


# ── 항목 정규화 ─────────────────────────────────────────────────────────────
def strip_tags(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def to_utc(struct_time) -> datetime | None:
    return datetime(*struct_time[:6], tzinfo=timezone.utc) if struct_time else None


def entries_from_feed(content: bytes) -> list[dict]:
    entries = []
    for e in feedparser.parse(content).entries:
        body_html = e["content"][0].get("value", "") if e.get("content") else ""
        summary = strip_tags(e.get("summary", ""))
        entries.append({
            "title": strip_tags(e.get("title", "")),
            "link": e.get("link", ""),
            "published": to_utc(e.get("published_parsed") or e.get("updated_parsed")),
            "inline_body_chars": len(strip_tags(body_html)) or len(summary),
            "blob": f"{strip_tags(e.get('title', ''))} {summary[:400]}".lower(),
            "has_external_url": True,
        })
    return entries


def entries_from_hn(content: bytes) -> list[dict]:
    entries = []
    for hit in json.loads(content).get("hits", []):
        created = hit.get("created_at_i")
        entries.append({
            "title": hit.get("title") or "",
            "link": hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
            "published": datetime.fromtimestamp(created, tz=timezone.utc) if created else None,
            "inline_body_chars": 0,
            "blob": (hit.get("title") or "").lower(),
            "has_external_url": bool(hit.get("url")),   # Ask HN 처럼 외부 글이 없는 항목은 본문 표본에서 뺀다
        })
    return entries


# ── 지표 계산 ───────────────────────────────────────────────────────────────
def volume_metrics(entries: list[dict], now: datetime) -> dict:
    dated = [e for e in entries if e["published"]]
    past = [e for e in dated if e["published"] <= now + FUTURE_TOLERANCE]

    def within(hours: int) -> int:
        cutoff = now - timedelta(hours=hours)
        return sum(1 for e in past if e["published"] >= cutoff)

    newest = max((e["published"] for e in past), default=None)
    return {
        "entries": len(entries),
        "dated_ratio": round(len(dated) / len(entries), 2) if entries else 0,
        "future_dated": len(dated) - len(past),
        "items_24h": within(24), "items_72h": within(72), "items_7d": within(24 * 7),
        "newest_age_h": round((now - newest).total_seconds() / 3600, 1) if newest else None,
        # 피드가 보여 주는 전부가 24시간 안이면, 하루치를 다 담지 못했을 수 있다(강의 3강의 '피드 제공 건수' 문제)
        "feed_saturated_24h": bool(entries) and within(24) >= len(entries),
    }


def keyword_hit_rate(entries: list[dict], keywords: list[str], now: datetime, days: int = 7):
    cutoff = now - timedelta(days=days)
    recent = [e for e in entries if e["published"] and cutoff <= e["published"] <= now + FUTURE_TOLERANCE]
    if not recent:
        return None
    return round(sum(1 for e in recent if any(k in e["blob"] for k in keywords)) / len(recent), 2)


def check_body(http: Http, robots: Robots, url: str, *, excluded_hosts=()) -> dict:
    with http._budget():
        return _check_body(http, robots, url, excluded_hosts)


def _check_body(http, robots, url, excluded_hosts):
    verdict = "not_checked" if _url_error(url, excluded_hosts) else _robots_verdict(robots, url, excluded_hosts)
    sample = {"url": url, "robots": verdict, "status": None, "chars": 0, "ok": False,
              "ms": None, "source": "page"}
    if verdict not in ("allowed", "no_robots"):
        return sample                      # 금지된 주소는 받아 오지 않는다
    resp = http.get(url, excluded_hosts=excluded_hosts, robots=robots,
                    max_bytes=BODY_MAX_BYTES, content_types=BODY_CONTENT_TYPES)
    sample["status"], sample["ms"] = resp["status"], resp["ms"]
    if resp["status"] == 200 and not resp["error"]:
        text = trafilatura.extract(resp["content"])
        sample["chars"] = len(text or "")
        sample["ok"] = sample["chars"] >= MIN_BODY_CHARS
    return sample


# ── 후보 한 곳 실측 ─────────────────────────────────────────────────────────
def survey_one(http: Http, robots: Robots, cand: dict, keywords: list[str],
               excluded_hosts: list[str], now: datetime) -> dict:
    result = {k: cand[k] for k in ("id", "name", "route", "tier", "lang", "url")}
    is_api = cand["route"] in {"api_hn", "api_arxiv"}
    with http._budget():
        result["robots_feed"] = ("n/a(API 약관 기준)" if is_api else
                                 "not_checked" if _url_error(cand["url"], excluded_hosts) else
                                 _robots_verdict(robots, cand["url"], excluded_hosts))
        if result["robots_feed"] in ("disallowed", "unreachable"):
            result.update(http=None, parse_ok=False, gates={"G1": False, "G2": False, "G3": False})
            return result
        resp = http.get(cand["url"], excluded_hosts=excluded_hosts, robots=None if is_api else robots)
    result["http"] = {k: resp[k] for k in ("status", "ms", "bytes", "final_url", "content_type", "error")}
    if resp["status"] != 200 or resp["error"]:
        result.update(parse_ok=False, gates={"G1": False, "G2": False, "G3": False})
        return result

    entries = entries_from_hn(resp["content"]) if cand["route"] == "api_hn" else entries_from_feed(resp["content"])
    result["parse_ok"] = bool(entries)
    result.update(volume_metrics(entries, now))
    result["keyword_hit_7d"] = keyword_hit_rate(entries, keywords, now)

    # 본문 표본: 날짜가 있는 최근 글 먼저. 제외 규정 도메인의 글은 어떤 경로로 들어와도 받지 않는다.
    ordered = sorted((e for e in entries if e["link"] and e["has_external_url"]),
                     key=lambda e: e["published"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    ordered = [e for e in ordered if not _url_error(e["link"], excluded_hosts)]
    samples = []
    for entry in ordered[:BODY_SAMPLES]:
        if cand["route"] in INLINE_BODY_ROUTES:
            chars = entry["inline_body_chars"]
            samples.append({"url": entry["link"], "robots": "n/a", "status": 200, "chars": chars,
                            "ok": chars >= MIN_BODY_CHARS, "ms": None, "source": "feed_inline"})
        else:
            samples.append(check_body(http, robots, entry["link"], excluded_hosts=excluded_hosts))
    result["body_samples"] = samples
    ok_count = sum(1 for s in samples if s["ok"])
    result["body_ok"] = f"{ok_count}/{len(samples)}"

    g1 = bool(samples) and ok_count / len(samples) >= G1_MIN_RATIO
    g2 = result["items_7d"] >= MIN_ITEMS_7D
    g3 = result["robots_feed"] in {"allowed", "no_robots"} or is_api
    g3 = g3 and all(s["robots"] not in {"disallowed", "unreachable"} for s in samples)
    result["gates"] = {"G1": g1, "G2": g2, "G3": g3}
    return result


# ── 진입점 ──────────────────────────────────────────────────────────────────
def load_spec() -> tuple[list[dict], list[str], list[str]]:
    spec = yaml.safe_load(CANDIDATES_FILE.read_text(encoding="utf-8"))
    excluded_hosts = [x["host"] for x in spec["excluded_by_assignment"]]
    for cand in spec["candidates"]:           # 제외 규정을 어긴 후보가 섞이지 않게 막는다
        error = _url_error(cand["url"], excluded_hosts)
        if error:
            raise SystemExit(f"지원하지 않는 후보: {cand['id']} ({error})")
    return spec["candidates"], [k.lower() for k in spec["topic_keywords"]], excluded_hosts


def print_table(results: list[dict]) -> None:
    head = f"{'id':<18}{'HTTP':>5}{'건수':>5}{'24h':>5}{'72h':>5}{'7d':>5}{'최신h':>7}{'본문':>6}  {'robots':<11}{'kw7d':>6}  G1 G2 G3"
    print(head)
    print("-" * len(head))
    for r in results:
        http = (r.get("http") or {}).get("status")
        g = r.get("gates", {})
        mark = lambda k: "ok" if g.get(k) else "--"
        print(f"{r['id']:<18}{str(http):>5}{r.get('entries', '-'):>5}{r.get('items_24h', '-'):>5}"
              f"{r.get('items_72h', '-'):>5}{r.get('items_7d', '-'):>5}{str(r.get('newest_age_h', '-')):>7}"
              f"{r.get('body_ok', '-'):>6}  {str(r.get('robots_feed'))[:10]:<11}{str(r.get('keyword_hit_7d', '-')):>6}"
              f"  {mark('G1')} {mark('G2')} {mark('G3')}")


def main() -> None:
    parser = argparse.ArgumentParser(description="소스 후보 실측 (읽기 전용 HTTP GET)")
    parser.add_argument("--dry", action="store_true", help="네트워크 없이 계획만 출력")
    parser.add_argument("--only", help="쉼표로 구분한 후보 id 만 다시 잰다(기존 결과에 합침)")
    args = parser.parse_args()

    candidates, keywords, excluded_hosts = load_spec()
    if args.only:
        wanted = set(args.only.split(","))
        candidates = [c for c in candidates if c["id"] in wanted]

    if args.dry:
        print(f"후보 {len(candidates)}곳, 본문 표본 소스당 최대 {BODY_SAMPLES}건")
        for c in candidates:
            print(f"  - {c['id']:<18}{c['route']:<12}{c['url']}")
        print(f"예상 요청 수: 약 {len(candidates) * (1 + BODY_SAMPLES) + len(candidates)}건 이하 (robots.txt 포함)")
        return

    now = datetime.now(timezone.utc)
    http = Http()
    try:
        _measure(http, candidates, keywords, excluded_hosts, args.only, now)
    finally:
        close = getattr(http, "close", None)
        if close is not None:
            close()


def _measure(http, candidates, keywords, excluded_hosts, only, now):
    robots = Robots(http)                    # 정책·요청 횟수·연결 풀 모두 같은 인스턴스
    results = []
    for cand in candidates:
        measured_at = datetime.now(timezone.utc)
        try:
            results.append(survey_one(http, robots, cand, keywords, excluded_hosts, measured_at))
        except Exception:                    # 한 후보의 오류가 전체 실측을 멈추지 않게 한다
            results.append({**{k: cand[k] for k in ("id", "name", "route", "tier", "lang", "url")},
                            "parse_ok": False, "gates": {"G1": False, "G2": False, "G3": False},
                            "error": traceback.format_exc(limit=2)[-400:]})
        results[-1]["surveyed_at_utc"] = measured_at.isoformat(timespec="seconds")
        results[-1]["surveyed_at_kst"] = measured_at.astimezone(KST).isoformat(timespec="seconds")
    print_table(results)

    merged = {}
    if only and OUTPUT_FILE.exists():
        previous = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
        merged = {r["id"]: {"surveyed_at_utc": previous.get("surveyed_at_utc"),
                            "surveyed_at_kst": previous.get("surveyed_at_kst"), **r}
                  for r in previous.get("sources", [])}
    merged.update({r["id"]: r for r in results})
    payload = {
        "surveyed_at_utc": now.isoformat(timespec="seconds"),
        "surveyed_at_kst": now.astimezone(KST).isoformat(timespec="seconds"),
        "user_agent": USER_AGENT, "min_body_chars": MIN_BODY_CHARS, "body_samples": BODY_SAMPLES,
        "min_items_7d": MIN_ITEMS_7D, "requests_made_this_run": http.count,
        "sources": list(merged.values()),
    }
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n요청 {http.count}건 · 저장: {OUTPUT_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
