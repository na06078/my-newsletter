"""파이프라인 전체(수집→선별→요약→검수→발행)를 가짜 모델·네트워크로 끝까지 돌려 분기를 확인한다.

모델 대역이 정해 준 응답으로 '그래프가 올바르게 갈라지고 기록이 남는가'를 보는 시험이다.
실제 모델의 판단 품질(환각을 잡는가 등)은 이 시험이 증명하지 않는다 — tools/negative_control.py 가 따로 잰다.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from graph import Pipeline, Verdict, Problem, initial_state
from 발행승인 import PublicationError, load_publication
from tests.support import (NOW, SRC, FakeHttp, FakeLLM, FakePublisher, FakeRobots, LLMError, atom, long_body,
                           make_settings, tmp_metrics)


def feed_of(n: int, *, body_chars: int = 700) -> bytes:
    return atom([{"title": f"기사 {i}", "link": f"http://a.example/{i}", "hours_ago": i + 1,
                  "summary": f"요약 {i}", "body": long_body(f"기사{i}", body_chars)} for i in range(n)])


class PipelineTests(unittest.TestCase):
    def run_pipeline(self, feed: bytes | None, llm: FakeLLM, *, publisher=None, send: bool = True, **select):
        metrics, path = tmp_metrics()
        http = FakeHttp({SRC["url"]: feed} if feed is not None else {})
        pub = publisher or FakePublisher()
        self.store = Path(tempfile.mkdtemp())               # 준비본은 실제 store/ 가 아닌 임시 폴더에만 쓴다
        pipe = Pipeline(make_settings(**select), llm=llm, http=http, robots=FakeRobots(), metrics=metrics,
                        publisher=pub, store=self.store, now_fn=lambda: NOW)
        final = pipe.build().invoke(initial_state("test-run", 48, send=send), config={"max_concurrency": 4})
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
        for stage in ("collect", "select_prelim", "select_final", "report", "verify",
                      "publication_prepared", "publication_sent"):
            self.assertIn(stage, stages)
        self.assertNotIn("publish", stages)                                # 예전 dry-run 전용 이벤트는 사라졌다
        self.assertEqual(stages.count("select_prelim"), 2)                 # 묶음마다 한 줄
        final_ev = next(e for e in events if e["stage"] == "select_final")
        self.assertEqual(len(final_ev["detail"]), 4)                       # 예선 통과 4건 각각의 본선 결과가 기록됨
        self.assertTrue(all(e["run_id"] == "test-run" for e in events))

    def test_failed_draft_is_regenerated_once_then_passes(self):
        def handler(stage, system, user, schema):
            if stage == "verify":
                passed = "다시 쓴" in user
                return Verdict(ok=passed, problems=[] if passed else [Problem(claim="첫 문장", kind="unsupported", note="원문에 없음")])
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

    def test_contradictory_positive_judge_is_regenerated_before_sending(self):
        def handler(stage, system, user, schema):
            if stage == "verify":
                retry = "다시 쓴" in user
                return Verdict(ok=True, problems=[] if retry else [Problem(claim="초안", kind="unsupported", note="원문 근거 부족")])
            return FakeLLM.default(stage, system, user, schema)
        final, events, pub = self.run_pipeline(feed_of(3), FakeLLM(handler))
        self.assertEqual(len(final["verified"]), 3)
        self.assertTrue(all(v["attempt"] == 1 for v in final["verified"]))
        self.assertEqual(sum(e["stage"] == "report" for e in events), 6)
        self.assertEqual(pub.calls, 1)
        self.assertTrue(all("다시 쓴" in e["title"] for e in pub.embeds[1:]))

    def test_judge_outage_fails_closed_without_retry(self):
        def handler(stage, system, user, schema):
            if stage == "verify":
                return LLMError("API 장애")
            return FakeLLM.default(stage, system, user, schema)
        final, events, _ = self.run_pipeline(feed_of(3), FakeLLM(handler))
        self.assertEqual(final["verified"], [])                             # 검수하지 못한 글은 내보내지 않는다
        self.assertEqual(sum(1 for e in events if e["stage"] == "report"), 3)   # 재생성으로 가지 않는다

    def test_mixed_outcomes_preserve_good_articles_and_retry_only_failed_draft(self):
        from graph import Draft
        feed = atom([{"title": f"기사 {i}", "link": f"http://a.example/{i}", "hours_ago": i + 1,
                      "body": "짧다" if i == 2 else long_body(f"기사{i}", 700)} for i in range(4)])
        def handler(stage, system, user, schema):
            if stage == "report":
                i = next(i for i in range(4) if f"제목: 기사 {i}" in user)
                headline = f"교정{i}" if "이전 초안이" in user else f"혼합{i}"
                return Draft(headline=headline, summary="첫째입니다. 둘째입니다. 셋째입니다.", why="실습에 참고할 수 있습니다.")
            if stage == "verify":
                if "headline: 혼합3" in user:
                    return LLMError("APITimeoutError")
                if "headline: 혼합1" in user:
                    return Verdict(ok=False, problems=[Problem(claim="초안", kind="unsupported", note="다시 확인")])
            return FakeLLM.default(stage, system, user, schema)
        final, events, pub = self.run_pipeline(feed, FakeLLM(handler), max_per_source=10)
        self.assertEqual({v["headline"]: v["attempt"] for v in final["verified"]}, {"혼합0": 0, "교정1": 1})
        self.assertEqual({r["title"] for r in final["rejected"]}, {"기사 2", "기사 3"})
        self.assertEqual(sum(e["stage"] == "report" for e in events), 5)
        self.assertEqual(sum(e["stage"] == "verify" for e in events), 4)
        self.assertEqual(pub.calls, 1)
        self.assertEqual(len(pub.embeds), 3)

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

    # ── 발송 노드 통합 (명세서 11절) ─────────────────────────────────────────
    def test_graph_runs_straight_through_to_send_without_a_human_step(self):
        metrics, _ = tmp_metrics()
        pipe = Pipeline(make_settings(), llm=FakeLLM(), http=FakeHttp(), robots=FakeRobots(), metrics=metrics,
                        publisher=FakePublisher(), store=Path(tempfile.mkdtemp()), now_fn=lambda: NOW)
        nodes = set(pipe.build().get_graph().nodes) - {"__start__", "__end__"}
        self.assertEqual(nodes, {"collect", "select", "report", "verify", "prepare_publication", "send_publication"})

    def test_sent_content_is_the_saved_prepared_content(self):
        final, events, pub = self.run_pipeline(feed_of(3), FakeLLM())
        manifest = load_publication(self.store, "test-run")                # 해시·형식까지 검증하며 다시 읽는다
        self.assertEqual(pub.calls, 1)                                      # 전송은 정확히 1회
        self.assertEqual(pub.embeds, manifest["payload"]["embeds"])         # 보낸 것 = 저장한 것
        self.assertEqual(final["published"]["sha256"], manifest["sha256"])
        self.assertEqual(final["published"]["status"], "sent")
        self.assertEqual(final["published"]["readback"], {"status": "verified"})
        self.assertTrue((self.store / "cache" / "publications" / "test-run.receipt.json").is_file())

    def test_dry_run_saves_prepared_content_but_never_sends(self):
        final, events, pub = self.run_pipeline(feed_of(3), FakeLLM(), send=False)
        self.assertEqual(pub.calls, 0)
        self.assertEqual(final["published"]["status"], "prepared")
        self.assertEqual(load_publication(self.store, "test-run")["sha256"], final["published"]["sha256"])
        stages = [e["stage"] for e in events]
        self.assertIn("publication_prepared", stages)
        self.assertNotIn("publication_sent", stages)
        self.assertFalse((self.store / "cache" / "publications" / "test-run.claim.json").exists())

    def test_send_failure_is_recorded_and_not_retried(self):
        final, events, pub = self.run_pipeline(feed_of(3), FakeLLM(), publisher=FakePublisher(status="failed"))
        self.assertEqual(final["published"]["status"], "failed")
        self.assertEqual(final["published"]["readback"], {"status": "unavailable"})
        self.assertEqual(pub.calls, 1)                                      # 실패해도 자동 재발송하지 않는다
        sent = next(e for e in events if e["stage"] == "publication_sent")
        self.assertEqual(sent["status"], "failed")

    def test_publication_nodes_refuse_to_run_without_a_store(self):
        metrics, _ = tmp_metrics()
        pipe = Pipeline(make_settings(), llm=None, http=None, robots=None, metrics=metrics, publisher=FakePublisher())
        with self.assertRaisesRegex(PublicationError, "store_required"):
            pipe.prepare_publication({"run_id": "no-store", "verified": []})
        with self.assertRaisesRegex(PublicationError, "store_required"):
            pipe.send_publication({"run_id": "no-store", "published": {"sha256": "0" * 64}})


if __name__ == "__main__":
    unittest.main()
