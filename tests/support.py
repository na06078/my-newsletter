"""테스트용 가짜 부품 — 네트워크·모델·발행 없이 파이프라인 전체를 돌려 보기 위한 대역."""
from __future__ import annotations

import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import load_settings
from graph import Draft, Pick, Shortlist, Verdict
from llm import LLMError
from metrics import Metrics

NOW = datetime(2026, 10, 29, 3, 0, tzinfo=timezone.utc)
SRC = {"id": "t_blog", "name": "테스트 블로그", "route": "atom", "tier": 1, "lang": "en", "url": "http://t.example/feed"}


def atom(entries: list[dict]) -> bytes:
    """entries: [{title, link, hours_ago, body}] → Atom XML"""
    parts = ['<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>']
    for e in entries:
        when = (NOW - timedelta(hours=e.get("hours_ago", 1))).strftime("%Y-%m-%dT%H:%M:%SZ")
        parts.append(f'<entry><title>{e["title"]}</title><link href="{e["link"]}"/><updated>{when}</updated>'
                     f'<summary>{e.get("summary", "요약")}</summary><content type="html">{e.get("body", "")}</content></entry>')
    parts.append("</feed>")
    return "".join(parts).encode("utf-8")


def long_body(tag: str, n: int = 700) -> str:
    return (f"{tag} 본문입니다. " * 200)[:n]


class FakeHttp:
    """url → 응답 dict. 등록되지 않은 주소는 404."""

    def __init__(self, routes: dict[str, bytes] | None = None) -> None:
        self.routes, self.count, self.user_agent = routes or {}, 0, "fake"

    def get(self, url: str, *, excluded_hosts=(), robots=None, max_bytes=None, content_types=None) -> dict:
        self.count += 1
        if url in self.routes:
            return {"status": 200, "content": self.routes[url], "final_url": url, "content_type": "", "error": None, "ms": 1}
        return {"status": 404, "content": b"", "final_url": url, "content_type": "", "error": None, "ms": 1}


class FakeRobots:
    def verdict(self, url: str) -> str:
        return "allowed"


class FakePublisher:
    """실제 Discord 대신 쓰는 발송기 대역. 발송 호출 횟수(calls)와 보낸 내용(embeds)을 기록한다.

    발송 노드가 쓰는 계약(live·webhook_url·publish·read_message)을 모두 갖춘다.
    """
    live = True
    webhook_url = "https://fake.invalid/incoming"      # 실제 주소가 아닌 자리 표시값

    def __init__(self, status: str = "sent") -> None:
        self.status, self.embeds, self.username, self.calls = status, None, None, 0

    def publish(self, embeds, username="편집실") -> dict:
        self.calls += 1
        self.embeds, self.username = embeds, username
        result = {"embeds": len(embeds), "chars": 0, "status": self.status}
        if self.status == "sent":
            result.update(http_status=200, message_id="123456")
        return result

    def read_message(self, message_id: str) -> dict:
        message = {"id": message_id, "embeds": self.embeds, "author": {"username": self.username}}
        return {"status": "read", "http_status": 200, "message": message}


class FakeLLM:
    """단계별 응답을 스크립트로 정하는 가짜 모델. handler(stage, system, user, schema) → 응답 객체."""

    def __init__(self, handler=None) -> None:
        self.handler, self.calls, self.log = handler or self.default, 0, []

    def parse(self, stage, system, user, schema):
        self.calls += 1
        self.log.append(stage)
        out = self.handler(stage, system, user, schema)
        if isinstance(out, Exception):
            raise out
        return out

    @staticmethod
    def default(stage, system, user, schema):
        if stage.startswith("select"):
            n_items = len(re.findall(r"^\d+\. ", user, re.M))
            ask = int(re.search(r"순서대로 (\d+)건", system).group(1))
            return Shortlist(picks=[Pick(index=i, score=9 - i, reason="독자 기준에 맞음", event=f"사건{i}")
                                    for i in range(min(n_items, ask))])
        if stage == "report":
            retry = "이전 초안이 검수를 통과하지 못했습니다" in user
            return Draft(headline="다시 쓴 헤드라인" if retry else "테스트 헤드라인",
                         summary="첫째 문장입니다. 둘째 문장입니다. 셋째 문장입니다.", why="독자 실습에 영향이 있습니다.")
        return Verdict(ok=True, problems=[])


def make_settings(**select_overrides) -> dict:
    s = load_settings()
    s["cfg"]["collect"]["sources"] = [SRC]
    s["cfg"]["select"].update(select_overrides)
    return s


def tmp_metrics() -> tuple[Metrics, Path]:
    path = Path(tempfile.mkdtemp()) / "metrics.jsonl"
    return Metrics(path, "test-run"), path


__all__ = ["NOW", "SRC", "atom", "long_body", "FakeHttp", "FakeRobots", "FakePublisher", "FakeLLM",
           "LLMError", "make_settings", "tmp_metrics"]
