"""파이프라인 전체(수집→선별→요약→검수→발행)를 가짜 모델·네트워크로 끝까지 돌려 분기를 확인한다.

모델 대역이 정해 준 응답으로 '그래프가 올바르게 갈라지고 기록이 남는가'를 보는 시험이다.
실제 모델의 판단 품질(환각을 잡는가 등)은 이 시험이 증명하지 않는다 — tools/negative_control.py 가 따로 잰다.
"""
from __future__ import annotations

import json
import unittest

from graph import Pipeline, Verdict, Problem, initial_state
from tests.support import (NOW, SRC, FakeHttp, FakeLLM, FakePublisher, FakeRobots, LLMError, atom, long_body,
                           make_settings, tmp_metrics)


def feed_of(n: int, *, body_chars: int = 700) -> bytes:
    return atom([{"title": f"기사 {i}", "link": f"http://a.example/{i}", "hours_ago": i + 1,
                  "summary": f"요약 {i}", "body": long_body(f"기사{i}", body_chars)} for i in range(n)])


class PipelineTests(unittest.TestCase):
    def run_pipeline(self, feed: bytes | None, llm: FakeLLM, *, publisher=None, **select):
        metrics, path = tmp_metrics()
        http = FakeHttp({SRC["url"]: feed} if feed is not None else {})
        pub = publisher or FakePublisher()
        pipe = Pipeline(make_settings(**select), llm=llm, http=http, robots=FakeRobots(), metrics=metrics,
                        publisher=pub, now_fn=lambda: NOW)
        final = pipe.build().invoke(initial_state("test-run", 48), config={"max_concurrency": 4})
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return final, events, pub

    def test_happy_path_runs_all_five_stages_and_records_each(self):
        # 소스가 하나뿐인 시험이라 '소스당 최대 2건' 규칙이 결과를 줄이지 않도록 상한을 푼다
        final, events, pub = self.run_pipeline(feed_of(8), FakeLLM(), batch_size=4, prelim_picks=2, max_per_source=10)
        self.assertEqual(len(final["collected"]), 8)
        self.assertEqual(len(final["shortlisted"]), 4)                     # 묶음 2개 × 예선 2건
        self.assertEqual(len(final["picked"]), 4)
        self.assertEqual(len(final["verified"]), 4)
        self.assertEqual(final["published"]["status"], "sent")
        self.assertEqual(len(pub.embeds), 5)                               # 머리 카드 + 기사 4건
        stages = [e["stage"] for e in events]
        for stage in ("collect", "select_prelim", "select_final", "report", "verify", "publish"):
            self.assertIn(stage, stages)
        self.assertEqual(stages.count("select_prelim"), 2)                 # 묶음마다 한 줄
        final_ev = next(e for e in events if e["stage"] == "select_final")
        self.assertEqual(len(final_ev["detail"]), 4)                       # 예선 통과 4건 각각의 본선 결과가 기록됨
        self.assertTrue(all(e["run_id"] == "test-run" for e in events))

    def test_failed_draft_is_regenerated_once_then_passes(self):
        def handler(stage, system, user, schema):
            if stage == "verify":
                return Verdict(ok=("다시 쓴" in user), problems=[Problem(claim="첫 문장", kind="unsupported", note="원문에 없음")])
            return FakeLLM.default(stage, system, user, schema)
        final, events, _ = self.run_pipeline(feed_of(3), FakeLLM(handler))
        self.assertEqual(len(final["verified"]), 3)
        self.assertTrue(all(v["attempt"] == 1 and v["headline"] == "다시 쓴 헤드라인" for v in final["verified"]))
        self.assertEqual(sum(1 for e in events if e["stage"] == "report"), 6)   # 3건 × 2회
        self.assertEqual(sum(1 for e in events if e["stage"] == "verify"), 6)

    def test_still_failing_after_retry_is_skipped_and_service_continues(self):
        def handler(stage, system, user, schema):
            if stage == "verify":
                return Verdict(ok=False, problems=[Problem(claim="x", kind="number_mismatch", note="숫자 불일치")])
            return FakeLLM.default(stage, system, user, schema)
        final, events, pub = self.run_pipeline(feed_of(3), FakeLLM(handler))
        self.assertEqual(final["verified"], [])
        self.assertEqual(len(final["rejected"]), 3)
        self.assertEqual(final["rejected"][0]["attempts"], 2)               # 처음 + 재생성 1회 = 2번 시도 후 스킵
        self.assertEqual(final["published"]["status"], "sent")             # 서비스는 멈추지 않고 '조용한 날' 카드를 보낸다
        self.assertIn("검수를 통과하지 못해", pub.embeds[0]["description"])

    def test_judge_outage_fails_closed_without_retry(self):
        def handler(stage, system, user, schema):
            if stage == "verify":
                return LLMError("API 장애")
            return FakeLLM.default(stage, system, user, schema)
        final, events, _ = self.run_pipeline(feed_of(3), FakeLLM(handler))
        self.assertEqual(final["verified"], [])                             # 검수하지 못한 글은 내보내지 않는다
        self.assertEqual(sum(1 for e in events if e["stage"] == "report"), 3)   # 재생성으로 가지 않는다

    def test_format_violation_skips_judge_cost_and_triggers_retry(self):
        calls = {"verify": 0}

        def handler(stage, system, user, schema):
            if stage == "report" and "이전 초안이" not in user:
                from graph import Draft
                return Draft(headline="영문 only?", summary="English only. Still English.", why="주목된다.")
            if stage == "verify":
                calls["verify"] += 1
            return FakeLLM.default(stage, system, user, schema)
        final, events, _ = self.run_pipeline(feed_of(2), FakeLLM(handler))
        self.assertEqual(len(final["verified"]), 2)
        self.assertEqual(calls["verify"], 2)       # 형식 불합격한 첫 초안은 모델 검수를 부르지 않았다(재생성 초안 2건만 검수)

    def test_no_collected_articles_goes_straight_to_quiet_card(self):
        llm = FakeLLM()
        final, events, pub = self.run_pipeline(None, llm)
        self.assertEqual(llm.calls, 0)                                     # 후보가 없으면 모델을 한 번도 부르지 않는다
        self.assertEqual(final["published"]["status"], "sent")
        self.assertIn("소스 응답 문제", pub.embeds[0]["description"])        # 조용한 날이 아니라 장애일 수 있음을 알린다
        collect_ev = next(e for e in events if e["stage"] == "collect")
        self.assertEqual(collect_ev["dead"], ["t_blog"])

    def test_short_body_is_skipped_not_summarized(self):
        short = atom([{"title": "짧은 글", "link": "http://a.example/s", "hours_ago": 1, "body": "짧다"}])
        final, events, _ = self.run_pipeline(short, FakeLLM())              # 페이지 주소는 404 → 본문 0자
        self.assertEqual(final["verified"], [])
        rep = next(e for e in events if e["stage"] == "report")
        self.assertEqual(rep["status"], "skipped_short_body")

    def test_prelim_llm_failure_falls_back_to_recency(self):
        def handler(stage, system, user, schema):
            if stage == "select_prelim":
                return LLMError("429")
            return FakeLLM.default(stage, system, user, schema)
        final, events, _ = self.run_pipeline(feed_of(4), FakeLLM(handler), max_per_source=10)
        self.assertEqual(len(final["picked"]), 4)                           # 모델이 실패해도 최신순으로 대체해 계속 간다
        self.assertTrue(next(e for e in events if e["stage"] == "select_prelim")["fallback"])


if __name__ == "__main__":
    unittest.main()
