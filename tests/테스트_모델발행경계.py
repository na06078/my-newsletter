"""설치 SDK + 명시적 전송 대역으로 모델·발행 경계를 검증한다."""
import json
import tempfile
import traceback
import unittest
from pathlib import Path

import httpx
from openai import OpenAI
from pydantic import BaseModel

from llm import LLM, LLMError
from 호출예산 import BudgetExceeded, RequestBudget


class Answer(BaseModel):
    ok: bool


class ModelBoundaryTests(unittest.TestCase):
    def make_llm(self, content, limit=2):
        scratch = Path.home() / "AppData/Local/hermes/cache/scratch"
        temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temp.cleanup)
        budget = RequestBudget(Path(temp.name) / "격리예산.sqlite3", limit=limit)
        self.requests, self.events = [], []

        def handler(request):
            self.requests.append(budget.snapshot()["used"])
            return httpx.Response(200, json={
                "id": "test", "object": "chat.completion", "created": 1, "model": "dummy",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": content}}]})

        client = OpenAI(api_key="dummy-not-a-secret", base_url="https://dummy.invalid/v1",
                        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        self.addCleanup(client.close)
        events = self.events

        class Metrics:
            def emit(self, event, **fields):
                events.append({"event": event, **fields})

        return LLM({"models": {"select": "dummy"}, "max_retries": 0},
                   client=client, budget=budget, metrics=Metrics()), budget

    def test_sdk_invalid_response_is_sanitized_llm_error(self):
        for content in ('{"ok": {"private": "secret-input-marker"}}', 'secret-input-marker invalid JSON'):
            with self.subTest(content=content):
                llm, budget = self.make_llm(content)
                with self.assertRaises(LLMError) as caught:
                    llm.parse("select", "system", "user", Answer)
                self.assertEqual(str(caught.exception), "ValidationError")
                rendered = "".join(traceback.format_exception(caught.exception))
                self.assertNotIn("secret-input-marker", rendered)
                self.assertNotIn("secret-input-marker", json.dumps(self.events))
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertEqual(self.requests, [1])
                self.assertEqual(budget.snapshot()["used"], 1)
                self.assertEqual(llm.calls, 1)
                self.assertEqual(self.events[0]["error"], "ValidationError")

    def test_sdk_valid_response_remains_typed_and_accounted(self):
        llm, budget = self.make_llm('{"ok": true}')
        result = llm.parse("select", "system", "user", Answer)
        self.assertIsInstance(result, Answer)
        self.assertIs(result.ok, True)
        self.assertEqual(self.requests, [1])
        self.assertEqual(budget.snapshot()["used"], 1)
        self.assertIsNone(self.events[0]["error"])

    def test_budget_exhaustion_remains_fatal_without_transport(self):
        llm, budget = self.make_llm('{"ok": true}', limit=0)
        with self.assertRaises(BudgetExceeded):
            llm.parse("select", "system", "user", Answer)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.events, [])
        self.assertEqual(budget.snapshot()["used"], 0)
