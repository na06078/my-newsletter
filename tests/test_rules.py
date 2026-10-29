"""코드가 확정하는 규칙(수집 필터·선별 규칙·검수 보조 검사·발행 어댑터)의 단위 시험 — 네트워크·모델 없이 돈다."""
from __future__ import annotations

import json
import unittest
from datetime import timedelta

import collectors
from graph import (Pick, custom_checks, derive_outcome, enforce_rules, format_problems, korean_ratio,
                   normalize_event, unmatched_numbers)
from publisher import DiscordPublisher, build_embeds, total_chars
from tests.support import NOW, SRC, FakeHttp, atom, long_body

import requests


class CollectTests(unittest.TestCase):
    def cfg(self, **kw):
        return {"per_source_cap": 12, "excluded_hosts": ["techcrunch.com"], "sources": [SRC], **kw}

    def test_window_future_excluded_host_and_dedupe(self):
        feed = atom([
            {"title": "신규 글", "link": "http://a.example/1?utm_source=x", "hours_ago": 2},
            {"title": "같은 글 다른 주소", "link": "http://a.example/1/", "hours_ago": 3},        # 추적 꼬리표·끝 슬래시만 다른 중복
            {"title": "오래된 글", "link": "http://a.example/old", "hours_ago": 100},              # 48시간 창 밖
            {"title": "미래 글", "link": "http://a.example/future", "hours_ago": -5},             # 시계 오류 → 상한으로 제외
            {"title": "제외 도메인", "link": "https://techcrunch.com/x", "hours_ago": 1},           # 과제 제외 소스
            {"title": "기사 id 쿼리", "link": "http://a.example/p?id=7", "hours_ago": 4},         # 기사를 가르는 쿼리는 남긴다
        ])
        items, stats = collectors.collect(self.cfg(), FakeHttp({SRC["url"]: feed}), NOW, 48)
        self.assertEqual(sorted(i["title"] for i in items), ["기사 id 쿼리", "신규 글"])
        st = stats[SRC["id"]]
        self.assertEqual((st["fetched"], st["in_window"], st["excluded_host"], st["error"]), (6, 3, 1, None))
        self.assertEqual(stats["_dedupe_removed"], 1)

    def test_failed_source_is_recorded_and_others_continue(self):
        other = {**SRC, "id": "ok_blog", "name": "정상", "url": "http://ok.example/feed"}
        feed = atom([{"title": "정상 글", "link": "http://ok.example/1", "hours_ago": 1}])
        cfg = self.cfg(sources=[SRC, other])                       # SRC 는 404
        items, stats = collectors.collect(cfg, FakeHttp({other["url"]: feed}), NOW, 48)
        self.assertEqual(len(items), 1)
        self.assertEqual(stats[SRC["id"]]["error"], "HTTP 404")    # 조용히 빠지지 않고 기록된다
        self.assertIsNone(stats["ok_blog"]["error"])

    def test_per_source_cap_keeps_newest(self):
        feed = atom([{"title": f"글{i}", "link": f"http://a.example/{i}", "hours_ago": i + 1} for i in range(6)])
        items, _ = collectors.collect(self.cfg(per_source_cap=2), FakeHttp({SRC["url"]: feed}), NOW, 48)
        self.assertEqual(sorted(i["title"] for i in items), ["글0", "글1"])

    def test_normalize_url(self):
        self.assertEqual(collectors.normalize_url("HTTP://A.example/p/?utm_medium=x&id=3#frag"), "http://a.example/p?id=3")


class RuleTests(unittest.TestCase):
    def item(self, sid, n):
        return {"id": f"{sid}{n}", "source_id": sid}

    def test_enforce_rules_event_and_source_cap(self):
        ranked = [(self.item("a", 1), Pick(index=0, score=9, reason="", event="GPT 출시")),
                  (self.item("a", 2), Pick(index=1, score=8, reason="", event="gpt  출시")),     # 같은 사건(표기만 다름)
                  (self.item("a", 3), Pick(index=2, score=7, reason="", event="다른 사건 1")),
                  (self.item("a", 4), Pick(index=3, score=6, reason="", event="다른 사건 2")),     # a 소스 3번째 → 상한
                  (self.item("b", 1), Pick(index=4, score=5, reason="", event="b 사건"))]
        taken, dropped = enforce_rules(ranked, target=5, min_final=3, max_per_source=2)
        self.assertEqual([i["id"] for i, _ in taken], ["a1", "a3", "b1"])
        self.assertEqual(sorted(r for *_, r in dropped), ["같은 사건 중복", "소스 상한"])

    def test_enforce_rules_backfills_source_cap_to_reach_minimum(self):
        ranked = [(self.item("a", i), Pick(index=i, score=9 - i, reason="", event=f"e{i}")) for i in range(1, 5)]
        taken, _ = enforce_rules(ranked, target=5, min_final=3, max_per_source=2)
        self.assertEqual(len(taken), 3)       # 소스가 하나뿐이어도 최소 3건은 채운다

    def test_unmatched_numbers_is_a_flag_not_a_verdict(self):
        body = "The model has 70B parameters and costs $1,200 per month after 3 months."
        self.assertEqual(unmatched_numbers("70B 모델이 월 1200 달러입니다.", body), [])                # 쉼표 차이는 같은 수
        self.assertEqual(unmatched_numbers("가격은 월 1500 달러입니다.", body), ["1500"])              # 원문에 없는 수
        self.assertEqual(unmatched_numbers("출시 후 3개월 뒤 가격이 바뀝니다.", "after three months"), ["3"])  # 표기 차이는 오탐 → 모델이 판단

    def test_format_and_custom_checks(self):
        ok = {"headline": "정상 헤드라인", "summary": "한 문장입니다. 두 문장입니다. 세 문장입니다.", "why": "의미가 있습니다."}
        vcfg = {"headline_max_chars": 40, "summary_max_chars": 700, "why_max_chars": 220, "korean_min_ratio": 0.3}
        self.assertEqual(format_problems(ok, vcfg) + custom_checks(ok, ""), [])
        bad = {"headline": "영어 headline?", "summary": "This is all English text. Nothing Korean here.", "why": "주목된다."}
        problems = format_problems(bad, vcfg) + custom_checks(bad, "")
        self.assertTrue(any("한국어" in p for p in problems))
        self.assertTrue(any("금지 표현" in p for p in problems))
        self.assertTrue(any("물음표" in p for p in problems))
        self.assertAlmostEqual(korean_ratio("가나다abc"), 0.5)

    def test_derive_outcome_uses_latest_passing_attempt(self):
        picked = [{"id": "x", "title": "X", "url": "u", "source": "s", "source_id": "s", "tier": 1, "published_at": None},
                  {"id": "y", "title": "Y", "url": "u", "source": "s", "source_id": "s", "tier": 1, "published_at": None}]
        drafted = [{"article_id": "x", "attempt": 0, "headline": "h0", "summary": "s", "why": "w"},
                   {"article_id": "x", "attempt": 1, "headline": "h1", "summary": "s", "why": "w"},
                   {"article_id": "y", "attempt": 0, "headline": "hy", "summary": "s", "why": "w"}]
        verdicts = [{"article_id": "x", "attempt": 0, "ok": False, "rule_problems": [], "problems": [{"note": "숫자 틀림"}], "error": None},
                    {"article_id": "x", "attempt": 1, "ok": True, "rule_problems": [], "problems": [], "error": None},
                    {"article_id": "y", "attempt": 0, "ok": False, "rule_problems": ["형식"], "problems": [], "error": None}]
        verified, rejected = derive_outcome(picked, drafted, verdicts)
        self.assertEqual([(v["article_id"], v["headline"], v["attempt"]) for v in verified], [("x", "h1", 1)])
        self.assertEqual([(r["article_id"], r["attempts"]) for r in rejected], [("y", 1)])


class PublisherTests(unittest.TestCase):
    ARTICLES = [{"headline": f"제목{i}", "summary": "요" * 1300, "why": "왜" * 100, "url": f"http://a/{i}",
                 "source": "소스", "tier": 1, "published_at": (NOW - timedelta(hours=3)).isoformat()} for i in range(5)]

    class Resp:
        def __init__(self, status, payload=None, text=""):
            self.status_code, self._p, self.text = status, payload or {}, text

        def json(self):
            return self._p

    def test_embed_limits_and_trimming(self):
        embeds, trimmed = build_embeds("2026-10-29", "브리핑", self.ARTICLES, {"collected": 50, "shortlisted": 15, "picked": 5, "verified": 5},
                                       total_max=5800, now=NOW)
        self.assertLessEqual(len(embeds), 10)
        self.assertLessEqual(total_chars(embeds), 5800)
        self.assertGreaterEqual(trimmed, 1)               # 1,300자 요약 5건(약 6,900자)은 한도 때문에 뒤에서부터 덜어 낸다

    def test_quiet_card_distinguishes_no_news_from_outage(self):
        quiet, _ = build_embeds("d", "t", [], {"collected": 40, "shortlisted": 10, "picked": 0, "verified": 0}, now=NOW)
        outage, _ = build_embeds("d", "t", [], {"collected": 0}, now=NOW)
        failed, _ = build_embeds("d", "t", [], {"collected": 40, "picked": 3, "verified": 0}, now=NOW)
        self.assertIn("독자 기준에 맞는 소식이 없었습니다", quiet[0]["description"])
        self.assertIn("소스 응답 문제", outage[0]["description"])
        self.assertIn("검수를 통과하지 못해", failed[0]["description"])

    def test_status_is_never_confused(self):
        embeds = [{"title": "t", "description": "d"}]
        secret_url = "https://discord.com/api/webhooks/123456/SECRET-TOKEN"
        self.assertEqual(DiscordPublisher(secret_url, live=False).publish(embeds)["status"], "dry_run")
        self.assertEqual(DiscordPublisher(None, live=True).publish(embeds)["status"], "skipped_no_webhook")
        ok = DiscordPublisher(secret_url, live=True, post=lambda *a, **k: self.Resp(200, {"id": "999"}))
        self.assertEqual(ok.publish(embeds), {"embeds": 1, "chars": 2, "status": "sent", "http_status": 200, "message_id": "999"})
        bad = DiscordPublisher(secret_url, live=True, post=lambda *a, **k: self.Resp(404, text="Unknown Webhook"))
        self.assertEqual((bad.publish(embeds)["status"], bad.publish(embeds)["http_status"]), ("failed", 404))

        def boom(*a, **k):
            raise requests.ConnectionError(f"failed to reach {secret_url}")     # 예외 문구에 주소가 섞여도
        res = DiscordPublisher(secret_url, live=True, post=boom).publish(embeds)
        self.assertEqual((res["status"], res["error"]), ("failed", "ConnectionError"))
        self.assertNotIn("SECRET", json.dumps(res))                                # 결과에는 새지 않는다

    def test_rate_limit_retries_once(self):
        seq = [self.Resp(429, {"retry_after": 0.5}), self.Resp(200, {"id": "1"})]
        slept = []
        pub = DiscordPublisher("https://x/y", live=True, post=lambda *a, **k: seq.pop(0), sleep=slept.append)
        self.assertEqual(pub.publish([{"title": "t"}])["status"], "sent")
        self.assertEqual(slept, [0.5])


if __name__ == "__main__":
    unittest.main()
