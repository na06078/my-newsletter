"""더미 키·MockTransport만 사용하며 실제 장부와 네트워크에는 접근하지 않는다."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import OpenAI
from pydantic import BaseModel

from llm import LLM, LLMError
from 호출예산 import RequestBudget, BudgetExceeded
import sqlite3
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor


def reserve_in_process(path):
    try:
        RequestBudget(path, limit=7).reserve()
        return True
    except BudgetExceeded:
        return False


class Answer(BaseModel):
    ok: bool


CFG = {"models": {"select": "dummy"}, "max_retries": 2}


def success():
    return httpx.Response(200, json={"id": "dummy", "object": "chat.completion", "created": 1,
        "model": "dummy", "choices": [{"index": 0, "finish_reason": "stop",
        "message": {"role": "assistant", "content": '{"ok": true}'}}]})


class Probe:
    def __init__(self):
        self.used = 0

    def reserve(self, **kwargs):
        self.used += 1
        return {"limit": 65, "used": self.used, "remaining": 65 - self.used}


class BudgetTests(unittest.TestCase):
    def client(self, handler):
        http = httpx.Client(transport=httpx.MockTransport(handler))
        client = OpenAI(api_key="dummy-not-a-secret", base_url="https://dummy.invalid/v1", http_client=http,
                        max_retries=2)
        self.addCleanup(client.close)
        return client

    def ledger(self, limit=65):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return RequestBudget(Path(temp.name) / "budget.sqlite3", limit)

    def test_restart_never_resets_and_limit_mismatch_fails_closed(self):
        budget = self.ledger(2)
        budget.reserve()
        self.assertEqual(RequestBudget(budget.path, 2).snapshot(), {"limit": 2, "used": 1, "remaining": 1})
        with self.assertRaises(BudgetExceeded):
            RequestBudget(budget.path, 65)
        self.assertEqual(budget.snapshot()["used"], 1)

    def test_invalid_ledgers_fail_closed_without_reset(self):
        for corruption in ("empty", "garbage", "missing_row", "negative_used", "over_cap", "extra_row", "wrong_schema"):
            with self.subTest(corruption=corruption):
                budget = self.ledger(3)
                if corruption == "empty":
                    budget.path.write_bytes(b"")
                elif corruption == "garbage":
                    budget.path.write_bytes(b"not sqlite")
                else:
                    conn = sqlite3.connect(budget.path)
                    try:
                        if corruption == "missing_row":
                            conn.execute("DELETE FROM budget")
                        elif corruption == "negative_used":
                            conn.execute("PRAGMA ignore_check_constraints=ON")
                            conn.execute("UPDATE budget SET used=-1")
                        elif corruption == "over_cap":
                            conn.execute("PRAGMA ignore_check_constraints=ON")
                            conn.execute("UPDATE budget SET used=4")
                        elif corruption == "extra_row":
                            conn.execute("PRAGMA ignore_check_constraints=ON")
                            conn.execute("INSERT INTO budget VALUES(2,3,0)")
                        else:
                            conn.execute("ALTER TABLE budget ADD COLUMN unexpected TEXT")
                        conn.commit()
                    finally:
                        conn.close()
                with self.assertRaises(BudgetExceeded):
                    RequestBudget(budget.path, 3)
                with self.assertRaises(BudgetExceeded):
                    budget.reserve()

    def test_invalid_limits_fail_closed(self):
        for limit in (-1, True, 1.5, "65", 65.0, None):
            with self.subTest(limit=limit), self.assertRaises(BudgetExceeded):
                self.ledger(limit)

    def test_unavailable_write_is_fatal(self):
        budget = self.ledger()
        with patch("호출예산.sqlite3.connect", side_effect=sqlite3.OperationalError("secret-bearing fake error")):
            with self.assertRaisesRegex(BudgetExceeded, "장부") as caught:
                budget.reserve()
            self.assertNotIn("secret", str(caught.exception))

    def test_sdk_retries_are_each_reserved_and_bounded(self):
        budget = self.ledger(8)
        observed = []
        def handler(request):
            observed.append(budget.snapshot()["used"])
            return httpx.Response(500, headers={"retry-after-ms": "1"}, json={"error": {"message": "test"}})
        llm = LLM(CFG, client=self.client(handler), budget=budget)
        with self.assertRaises(LLMError):
            llm.parse("select", "s", "u", Answer)
        self.assertEqual(observed, [1, 2, 3])
        self.assertEqual(budget.snapshot()["used"], 3)

    def test_timeout_reservations_are_not_refunded(self):
        budget = self.ledger(5)
        observed = []
        def handler(request):
            observed.append(budget.snapshot()["used"])
            raise httpx.ReadTimeout("test-only timeout", request=request)
        llm = LLM(CFG, client=self.client(handler), budget=budget)
        with patch("openai._base_client.time.sleep"):
            with self.assertRaises(LLMError):
                llm.parse("select", "s", "u", Answer)
        self.assertEqual(observed, [1, 2, 3])
        self.assertEqual(budget.snapshot()["used"], 3)

    def test_exhaustion_during_sdk_retry_is_fatal_before_send(self):
        budget = self.ledger(1)
        sent = []
        def handler(request):
            sent.append(1)
            return httpx.Response(429, headers={"retry-after-ms": "1"}, json={"error": {"message": "test"}})
        llm = LLM(CFG, client=self.client(handler), budget=budget)
        with self.assertRaises(BudgetExceeded):
            llm.parse("select", "s", "u", Answer)
        self.assertEqual(len(sent), 1)
        with self.assertRaises(BudgetExceeded):
            llm.parse("select", "s", "u", Answer)
        self.assertEqual(len(sent), 1)
        self.assertEqual(budget.snapshot()["used"], 1)
        self.assertFalse(issubclass(BudgetExceeded, LLMError))

    def test_temperature_fallback_is_reserved_separately(self):
        budget = self.ledger(3)
        bodies, counts = [], []
        def handler(request):
            bodies.append(json.loads(request.content))
            counts.append(budget.snapshot()["used"])
            if "temperature" in bodies[-1]:
                return httpx.Response(400, json={"error": {"message": "Unsupported temperature"}})
            return success()
        llm = LLM({**CFG, "temperature": 0.1}, client=self.client(handler), budget=budget)
        self.assertTrue(llm.parse("select", "s", "u", Answer).ok)
        self.assertTrue(llm.parse("select", "s", "u", Answer).ok)
        self.assertEqual(counts, [1, 2, 3])
        self.assertIn("temperature", bodies[0])
        self.assertNotIn("temperature", bodies[1])
        self.assertNotIn("temperature", bodies[2])

    def test_thread_and_process_reservations_share_one_cap(self):
        budget = self.ledger(7)
        with ThreadPoolExecutor(max_workers=8) as pool:
            threaded = list(pool.map(reserve_in_process, [str(budget.path)] * 5))
        with ProcessPoolExecutor(max_workers=3) as pool:
            processed = list(pool.map(reserve_in_process, [str(budget.path)] * 12))
        self.assertEqual(sum(threaded) + sum(processed), 7)
        self.assertEqual(budget.snapshot(), {"limit": 7, "used": 7, "remaining": 0})

    def test_concurrent_real_sdk_calls_never_send_above_cap(self):
        budget = self.ledger(5)
        sent = []
        def handler(request):
            sent.append(1)
            return success()
        llm = LLM(CFG, client=self.client(handler), budget=budget)
        def call(_):
            try:
                return llm.parse("select", "s", "u", Answer).ok
            except BudgetExceeded:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(call, range(20)))
        self.assertEqual(sum(results), 5)
        self.assertEqual(len(sent), 5)
        self.assertEqual(budget.snapshot()["used"], 5)

    def test_pipeline_selection_does_not_swallow_exhaustion(self):
        from graph import Pipeline
        from tests.support import make_settings, FakeHttp, FakeRobots, FakePublisher, NOW
        from metrics import Metrics
        budget = self.ledger(0)
        sent = []
        llm = LLM(CFG, client=self.client(lambda request: sent.append(1) or success()), budget=budget)
        with patch("config.load_dotenv"):
            settings = make_settings()
        metrics = Metrics(budget.path.parent / "metrics.jsonl", "budget-test")
        pub = FakePublisher()
        pipe = Pipeline(settings, llm=llm, http=FakeHttp(), robots=FakeRobots(), metrics=metrics,
                        publisher=pub, now_fn=lambda: NOW)
        article = {"id": "one", "title": "test", "source": "test", "tier": 1, "published_at": None,
                   "summary": "test", "source_id": "test"}
        with self.assertRaises(BudgetExceeded):
            pipe.select({"collected": [article]})
        self.assertEqual(sent, [])
        self.assertIsNone(pub.embeds)

    def test_provider_exception_text_is_not_exposed(self):
        budget = self.ledger()
        llm = LLM({**CFG, "max_retries": 0}, budget=budget,
                  client=self.client(lambda request: httpx.Response(401, json={"error": {"message": "secret-marker"}})))
        with self.assertRaises(LLMError) as caught:
            llm.parse("select", "s", "u", Answer)
        self.assertNotIn("secret-marker", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_retried_failure_then_success_counts_both_attempts(self):
        budget = self.ledger(2)
        observed = []
        def handler(request):
            observed.append(budget.snapshot()["used"])
            if len(observed) == 1:
                return httpx.Response(503, headers={"retry-after-ms": "1"}, json={"error": {"message": "test"}})
            return success()
        llm = LLM(CFG, client=self.client(handler), budget=budget)
        self.assertTrue(llm.parse("select", "s", "u", Answer).ok)
        self.assertEqual(observed, [1, 2])

    def test_temperature_retry_exhaustion_never_sends_fallback(self):
        budget = self.ledger(1)
        sent = []
        def handler(request):
            sent.append(1)
            return httpx.Response(400, json={"error": {"message": "Unsupported temperature"}})
        llm = LLM({**CFG, "temperature": 0.1}, client=self.client(handler), budget=budget)
        with self.assertRaises(BudgetExceeded):
            llm.parse("select", "s", "u", Answer)
        self.assertEqual(sent, [1])
        self.assertEqual(budget.snapshot()["used"], 1)

    def test_shared_http_client_uses_only_current_call_budget(self):
        one, two = self.ledger(2), self.ledger(2)
        sent = []
        client = self.client(lambda request: sent.append(1) or success())
        a = LLM(CFG, client=client, budget=one)
        b = LLM(CFG, client=client, budget=two)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda llm: llm.parse("select", "s", "u", Answer).ok, [a, b]))
        self.assertEqual(results, [True, True])
        self.assertEqual([one.snapshot()["used"], two.snapshot()["used"]], [1, 1])
        self.assertEqual(len(sent), 2)

    def test_removed_ledger_is_not_recreated_by_existing_instance(self):
        budget = self.ledger(2)
        budget.reserve()
        budget.path.unlink()
        with self.assertRaises(BudgetExceeded):
            budget.reserve()
        self.assertFalse(budget.path.exists())

    def test_default_constructor_reserves_before_http_send(self):
        budget = Probe()
        observed = []
        def handler(request):
            observed.append(budget.used)
            return success()
        with patch("llm.RequestBudget", return_value=budget, create=True) as factory:
            llm = LLM({**CFG, "request_limit": 17}, client=self.client(handler))
            self.assertTrue(llm.parse("select", "s", "u", Answer).ok)
            from llm import __file__ as module_path
            factory.assert_called_once_with(Path(module_path).resolve().parent / "store" / "호출예산.sqlite3", limit=17)
        self.assertEqual(observed, [1], "실제 HTTP 전송 전에 공통 예산을 예약해야 합니다")


if __name__ == "__main__":
    unittest.main()
