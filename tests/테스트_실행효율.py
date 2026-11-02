"""합성 응답만으로 본문 재사용·검수 동시성의 행동 계약을 검증한다."""
import unittest
import tempfile
from pathlib import Path
from threading import Barrier, BrokenBarrierError, Event, Lock
from unittest.mock import patch

from graph import Pipeline, initial_state
from tests.support import (FakeHttp, FakeLLM, FakePublisher, FakeRobots, NOW,
                           SRC, atom, long_body, make_settings, tmp_metrics)


class EfficiencyTests(unittest.TestCase):
    def test_immediate_fatal_does_not_start_avoidably_queued_checks(self):
        from concurrent.futures import ThreadPoolExecutor
        from 호출예산 import BudgetExceeded
        fatal = BudgetExceeded("합성 예산 소진")
        checked = []
        class FirstFinishesBeforeNextSubmit(ThreadPoolExecutor):
            def submit(self, fn, *args, **kwargs):
                future = super().submit(fn, *args, **kwargs)
                # 실제 worker에서 첫 오류가 이미 확정된 스케줄을 재현한다.
                future.exception(timeout=3)
                return future
        pipe = self.pipe()
        def check(d):
            checked.append(d["article_id"])
            raise fatal
        with patch("graph.ThreadPoolExecutor", FirstFinishesBeforeNextSubmit), \
             patch.object(pipe, "check_draft", side_effect=check):
            with self.assertRaises(BudgetExceeded) as caught:
                pipe.verify({"picked": [], "drafted": self.drafts()})
        self.assertIs(caught.exception, fatal)
        self.assertEqual(checked, ["0"])

    def test_graph_regeneration_keeps_exact_calls_with_one_page_get_per_article(self):
        from graph import Problem, Verdict
        def respond(stage, system, user, schema):
            if stage == "verify" and "다시 쓴" not in user:
                return Verdict(ok=False, problems=[Problem(claim="초안", kind="unsupported", note="수정")])
            return FakeLLM.default(stage, system, user, schema)
        with tempfile.TemporaryDirectory() as directory:
            pipe = self.pipe(FakeLLM(respond), make_settings(max_per_source=10))
            pipe.store = Path(directory)
            entries = [{"title": f"기사{i}", "link": f"http://a.example/{i}", "body": ""} for i in range(3)]
            pipe.http = FakeHttp({SRC["url"]: atom(entries)} | {
                e["link"]: ("<html><body><article><p>" + long_body("원문", 1500) +
                            "</p></article></body></html>").encode() for e in entries})
            final = pipe.build().invoke(initial_state("reuse-pages", 48), config={"max_concurrency": 4})
            self.assertEqual(pipe.http.count, 4)  # 피드 1 + 기사 3, 재생성 GET 0
            self.assertEqual(pipe.llm.log.count("report"), 6)
            self.assertEqual(pipe.llm.log.count("verify"), 6)
            self.assertEqual(pipe.llm.calls, 14)  # 선별 2 + 요약 6 + 검수 6
            self.assertEqual((final["round"], len(final["verified"]), final["rejected"]), (2, 3, []))
            self.assertEqual(pipe.publisher.calls, 1)
            for article in final["picked"]:
                drafts = [d for d in final["drafted"] if d["article_id"] == article["id"]]
                self.assertEqual([d["attempt"] for d in drafts], [0, 1])
                self.assertEqual(drafts[0]["body"], drafts[1]["body"])
                self.assertEqual(drafts[0]["body_meta"], drafts[1]["body_meta"])

    def test_injected_body_keeps_length_guards_and_never_fetches(self):
        pipe = self.pipe()
        with patch.object(pipe, "_body", side_effect=AssertionError("재조회 금지")):
            result = pipe.report({"item": self.item(), "attempt": 1, "feedback": "수정", "body": "짧다",
                                  "body_meta": {"body_error": "http_403"}})
            self.assertEqual(result["verdicts"][0]["error"], "http_403")
            self.assertFalse(result["verdicts"][0]["retryable"])
            self.assertEqual(pipe.llm.calls, 0)
            result = pipe.report({"item": self.item(), "attempt": 1, "feedback": "수정", "body": "가" * 9000})
            self.assertEqual(len(result["drafted"][0]["body"]), 6000)
            with self.assertRaisesRegex(ValueError, "body must be a string"):
                pipe.report({"item": self.item(), "attempt": 1, "feedback": None, "body": ["bad"]})

    def test_retry_uses_matching_attempt_not_another_article_or_latest_cache(self):
        pipe = self.pipe()
        pipe.cfg["verify"]["max_regenerations"] = 3
        drafts = [{**d, "body": long_body(str(d["attempt"]))} for d in
                  [self.drafts(1, 0)[0], self.drafts(1, 1)[0], self.drafts(1, 2)[0]]]
        state = {"picked": [self.item("0")], "drafted": drafts, "round": 2,
                 "verdicts": [{"article_id": "0", "attempt": 1, "ok": False, "retryable": True,
                               "rule_problems": ["수정"], "problems": []}]}
        self.assertEqual(pipe.route_after_verify(state)[0].arg["body"], long_body("1"))

    def test_all_worker_limits_keep_same_results_calls_and_current_round_only(self):
        results = []
        for workers in (1, 2, 3, 4):
            with self.subTest(workers=workers):
                settings = make_settings()
                settings["cfg"]["verify"]["max_concurrency"] = workers
                pipe = self.pipe(settings=settings)
                drafts = self.drafts(8, attempt=1)
                old = {"article_id": "old-failure", "attempt": 0, "ok": False,
                       "rule_problems": [], "problems": [], "error": "robots_unreachable", "retryable": False}
                state = {"picked": [self.item(str(i)) for i in range(8)] + [self.item("old-failure")],
                         "drafted": self.drafts(8) + drafts, "verdicts": [old], "round": 1}
                barrier = Barrier(workers)
                lock = Lock()
                active = peak = 0
                original = pipe.check_draft
                def check(d):
                    nonlocal active, peak
                    with lock:
                        active += 1
                        peak = max(peak, active)
                    try:
                        # 첫 묶음만 완전한 barrier를 사용한다(마지막 묶음은 더 작을 수 있다).
                        if int(d["article_id"]) < workers:
                            barrier.wait(timeout=3)
                        return original(d)
                    finally:
                        with lock:
                            active -= 1
                with patch.object(pipe, "check_draft", side_effect=check):
                    result = pipe.verify(state)
                self.assertEqual(peak, workers)
                self.assertEqual(pipe.llm.log, ["verify"] * 8)
                self.assertEqual([v["attempt"] for v in result["verdicts"]], [1] * 8)
                self.assertEqual(result["round"], 2)
                self.assertEqual(result["rejected"][0]["reason"], "robots_unreachable")
                self.assertEqual(state["verdicts"], [old])
                results.append(result)
        self.assertTrue(all(result == results[0] for result in results))

    def test_real_sdk_budget_fatal_waits_for_running_call_and_never_publishes(self):
        import httpx
        from openai import OpenAI
        from llm import LLM
        from 호출예산 import BudgetExceeded, RequestBudget
        with tempfile.TemporaryDirectory() as directory:
            budget = RequestBudget(Path(directory) / "budget.sqlite3", limit=1)
            aborted, completed = Event(), Event()
            requests, fatal_errors, verify_calls = [], [], []
            def handler(request):
                requests.append(request)
                self.assertTrue(aborted.wait(5), "동시 요청이 예산 소진을 관측하지 못함")
                completed.set()
                return httpx.Response(200, json={"id": "synthetic", "object": "chat.completion", "created": 1,
                    "model": "dummy", "choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": '{"ok":true,"problems":[]}'}}]})
            with OpenAI(api_key="dummy-not-secret", base_url="https://dummy.invalid/v1",
                        http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
                sdk = LLM({"models": {"verify": "dummy"}, "max_retries": 0}, client=client, budget=budget)
                def respond(stage, system, user, schema):
                    if stage != "verify":
                        return FakeLLM.default(stage, system, user, schema)
                    verify_calls.append(stage)
                    try:
                        return sdk.parse(stage, system, user, schema)
                    except BudgetExceeded as exc:
                        fatal_errors.append(exc)
                        aborted.set()
                        raise
                pipe = self.pipe(FakeLLM(respond))
                pipe.store = Path(directory) / "publication"
                items = [self.item(str(i)) | {"inline_body": long_body("본문")} for i in range(5)]
                with patch.object(pipe, "collect", return_value={"collected": []}), \
                     patch.object(pipe, "select", return_value={"picked": items}):
                    with self.assertRaises(BudgetExceeded) as caught:
                        pipe.build().invoke(initial_state("fatal", 48), config={"max_concurrency": 4})
                self.assertIs(caught.exception, fatal_errors[0])
                self.assertTrue(completed.is_set())
                self.assertEqual((len(verify_calls), len(requests), budget.snapshot()["used"]), (2, 1, 1))
                self.assertEqual(pipe.publisher.calls, 0)
                self.assertFalse(pipe.store.exists())

    def test_invalid_worker_setting_is_rejected_before_any_model_call(self):
        for value in (True, False, 0, -1, 5, 2.0, "2", None):
            with self.subTest(value=value):
                settings = make_settings()
                settings["cfg"]["verify"]["max_concurrency"] = value
                llm = FakeLLM()
                with self.assertRaisesRegex(ValueError, "verify.max_concurrency"):
                    pipe = self.pipe(llm, settings)
                    pipe.verify({"picked": [], "drafted": self.drafts()})
                self.assertEqual(llm.calls, 0)

    def drafts(self, count=5, attempt=0):
        return [{"article_id": str(i), "attempt": attempt, "headline": f"기사{i}",
                 "summary": "첫째입니다. 둘째입니다. 셋째입니다.", "why": "실습에 참고합니다.",
                 "body": long_body("본문"), "body_source": "page"} for i in range(count)]

    def test_default_workers_overlap_and_return_input_order_not_completion_order(self):
        pipe = self.pipe()
        second_done = Event()
        pair = Barrier(2)
        lock = Lock()
        active = peak = 0
        completed = []
        original = pipe.check_draft
        def check(d):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                if d["article_id"] in ("0", "1"):
                    try:
                        pair.wait(timeout=3)
                    except BrokenBarrierError:
                        self.fail("검수 두 건이 동시에 진입하지 못함")
                    if d["article_id"] == "0":
                        self.assertTrue(second_done.wait(3))
                result = original(d)
                with lock:
                    completed.append(d["article_id"])
                if d["article_id"] == "1":
                    second_done.set()
                return result
            finally:
                with lock:
                    active -= 1
        with patch.object(pipe, "check_draft", side_effect=check):
            result = pipe.verify({"picked": [], "drafted": self.drafts(), "round": 0})
        self.assertEqual(peak, 2)
        self.assertLess(completed.index("1"), completed.index("0"))
        self.assertEqual([v["article_id"] for v in result["verdicts"]], ["0", "1", "2", "3", "4"])
        self.assertEqual((result["round"], pipe.llm.log), (1, ["verify"] * 5))

    def pipe(self, llm=None, settings=None):
        metrics, _ = tmp_metrics()
        return Pipeline(settings or make_settings(), llm=llm or FakeLLM(), http=FakeHttp(),
                        robots=FakeRobots(), metrics=metrics, publisher=FakePublisher(), now_fn=lambda: NOW)

    def item(self, name="one"):
        return {"id": name, "title": name, "url": "http://a.example/" + name,
                "source": "시험", "source_id": "test", "tier": 1, "published_at": None}

    def test_retry_uses_failed_attempt_body_and_safe_metadata_without_refetch(self):
        pipe = self.pipe()
        item = self.item()
        body = long_body("최초 본문")
        meta = {"http_status": 200, "robots": "allowed", "body_error": None, "unsafe": "discard"}
        with patch.object(pipe, "_body", return_value=(body, "page", meta)) as fetch:
            first = pipe.report({"item": item, "attempt": 0, "feedback": None})["drafted"][0]
            state = {**initial_state("reuse", 48), "picked": [item], "drafted": [first], "round": 1,
                     "verdicts": [{"article_id": "one", "attempt": 0, "ok": False, "retryable": True,
                                   "rule_problems": ["수정"], "problems": []}]}
            retry = pipe.route_after_verify(state)[0].arg
            second = pipe.report(retry)["drafted"][0]
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(second["body"], body)
        self.assertEqual(second["body_source"], "page")
        self.assertEqual(second["body_meta"], {k: meta[k] for k in ("http_status", "robots", "body_error")})
        self.assertEqual((retry["attempt"], retry["feedback"]), (1, "- 수정"))
        self.assertEqual(pipe.llm.log, ["report", "report"])
        self.assertEqual(pipe.route_after_select({"picked": [item]})[0].arg,
                         {"item": item, "attempt": 0, "feedback": None})


if __name__ == "__main__":
    unittest.main()
