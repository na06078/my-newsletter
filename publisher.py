"""발행 어댑터 — Discord 웹훅.

임베드(embed: 제목·본문·링크를 카드 한 장으로 보여 주는 Discord 메시지 형식)를 만들고 보낸다.
결과는 dry_run / sent / failed / skipped_no_webhook 으로 구분해 돌려준다.
(강의 기본 코드는 보내지 않은 모든 경우를 dry-run 으로 표시해 실패와 구분되지 않았다.)
웹훅 주소 자체가 비밀 열쇠이므로 로그·오류 문구에 절대 넣지 않는다.
"""
from __future__ import annotations

import copy
import json
import os
import re
import time
from datetime import datetime, timezone

import requests

TITLE_MAX, DESC_MAX, EMBED_MAX = 256, 4096, 10
COLORS = {1: 0x0B6E77, 2: 0x4C7C9C}      # 1차 소스 / 2차 소스
HEADER_COLOR, QUIET_COLOR = 0x5F7476, 0x8F5606


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _age_text(published_at: str | None, now: datetime) -> str:
    if not published_at:
        return "시각 미상"
    hours = (now - datetime.fromisoformat(published_at)).total_seconds() / 3600
    return f"{int(hours)}시간 전" if hours < 48 else f"{int(hours // 24)}일 전"


def total_chars(embeds: list[dict]) -> int:
    return sum(len(e.get("title", "")) + len(e.get("description", "")) + len(e.get("footer", {}).get("text", ""))
               for e in embeds)


def build_embeds(run_label: str, title: str, articles: list[dict], stats: dict, *,
                 total_max: int = 5800, now: datetime | None = None) -> tuple[list[dict], int]:
    """검수를 통과한 기사 목록을 Discord 임베드로 만든다. (임베드 목록, 한도 때문에 뺀 건수)를 돌려준다."""
    now = now or datetime.now(timezone.utc)
    flow = (f"수집 {stats.get('collected', 0)} → 예선 {stats.get('shortlisted', 0)} → "
            f"선별 {stats.get('picked', 0)} → 검수 통과 {stats.get('verified', 0)}")
    if not articles:
        if stats.get("collected", 0) == 0:
            reason = "수집 0건입니다. 조용한 날이 아니라 소스 응답 문제일 수 있어 점검이 필요합니다."
        elif stats.get("picked", 0) > 0:
            reason = f"선별한 {stats['picked']}건이 모두 검수를 통과하지 못해 발행하지 않았습니다."
        else:
            reason = "수집한 글 중 독자 기준에 맞는 소식이 없었습니다."
        return [{"title": _clip(f"🗞️ {run_label} · 오늘은 조용합니다", TITLE_MAX),
                 "description": f"{flow}\n{reason}", "color": QUIET_COLOR}], 0

    sources = ", ".join(dict.fromkeys(a["source"] for a in articles))
    embeds = [{"title": _clip(f"🗞️ {run_label} · {title}", TITLE_MAX),
               "description": f"오늘은 {len(articles)}건을 골랐습니다. ({sources})\n{flow}", "color": HEADER_COLOR}]
    for i, a in enumerate(articles, 1):
        desc = a["summary"] + (f"\n\n💡 **{a['why']}**" if a.get("why") else "")
        embeds.append({
            "title": _clip(f"{i}. {a['headline']}", TITLE_MAX), "description": _clip(desc, DESC_MAX),
            "url": a["url"], "color": COLORS.get(a.get("tier", 2), HEADER_COLOR),
            "footer": {"text": f"{a['source']} · {_age_text(a.get('published_at'), now)}"},
        })
    trimmed = 0
    while len(embeds) > EMBED_MAX or total_chars(embeds) > total_max:
        embeds.pop()                                        # 뒤 기사부터 덜어 낸다
        trimmed += 1
    return embeds, trimmed


class DiscordPublisher:
    def __init__(self, webhook_url: str | None, *, live: bool, post=requests.post, get=requests.get, sleep=time.sleep) -> None:
        self.webhook_url, self.live, self._post, self._sleep = webhook_url, live, post, sleep
        self._get = get

    @classmethod
    def from_env(cls, publish_cfg: dict, *, live: bool) -> "DiscordPublisher":
        return cls(os.environ.get(publish_cfg["webhook_env"]) or None, live=live)

    def publish(self, embeds: list[dict], username: str = "편집실") -> dict:
        base = {"embeds": len(embeds), "chars": total_chars(embeds)}
        if not self.live:
            return {**base, "status": "dry_run"}
        if not self.webhook_url:
            return {**base, "status": "skipped_no_webhook"}
        payload = {"username": username, "embeds": copy.deepcopy(embeds), "allowed_mentions": {"parse": []}}
        try:
            resp = self._send(payload)
        except (requests.RequestException, ValueError) as exc:
            return {**base, "status": "failed", "error": type(exc).__name__}   # 문구에 주소가 섞일 수 있어 종류만
        if resp.status_code in (200, 204):
            message_id = None
            if resp.status_code == 200:
                try:
                    candidate_id = resp.json().get("id")
                    if self._valid_message_id(candidate_id):
                        message_id = candidate_id
                except ValueError:
                    pass
            return {**base, "status": "sent", "http_status": resp.status_code, "message_id": message_id}
        return {**base, "status": "failed", "http_status": resp.status_code, "error": "HTTPError"}

    @staticmethod
    def _valid_message_id(message_id):
        return isinstance(message_id, str) and message_id.isascii() and message_id.isdigit() and len(message_id) <= 30

    def read_message(self, message_id: str) -> dict:
        """전송 메시지 재조회. URL·응답 오류 본문은 결과에 포함하지 않는다."""
        if not self._valid_message_id(message_id):
            return {"status": "failed", "error": "invalid_message_id"}
        if not self.live:
            return {"status": "dry_run"}
        if not self.webhook_url:
            return {"status": "skipped_no_webhook"}
        try:
            # query는 실행 옵션이며 메시지 리소스 경로의 일부가 아니다.
            from urllib.parse import urlsplit, urlunsplit
            parts = urlsplit(self.webhook_url)
            url = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") + "/messages/" + message_id, parts.query, ""))
            response = self._get(url, timeout=20)
            if response.status_code != 200:
                return {"status": "failed", "http_status": response.status_code, "error": "HTTPError"}
            message = response.json()
            if not isinstance(message, dict) or message.get("id") != message_id:
                return {"status": "failed", "error": "invalid_message"}
            # 오류 응답에 토큰 주소가 반사된 경우에도 비밀을 반환하지 않는다.
            raw = json.dumps(message, ensure_ascii=False)
            if self.webhook_url in raw or re.search(r"https?://(?:[a-z]+\.)?discord(?:app)?\.com/api(?:/v[0-9]+)?/webhooks/", raw, re.I):
                return {"status": "failed", "error": "unsafe_response"}
            return {"status": "read", "http_status": 200, "message": copy.deepcopy(message)}
        except (requests.RequestException, ValueError, TypeError):
            return {"status": "failed", "error": "read_failed"}

    def _send(self, payload: dict):
        # wait=true: 전송된 메시지 정보(id)를 응답으로 돌려받아 '실제로 올라갔다'는 증거로 쓴다
        return self._post(self.webhook_url, params={"wait": "true"}, json=payload, timeout=20)
