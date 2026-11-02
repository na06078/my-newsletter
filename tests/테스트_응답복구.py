"""설치 SDK의 합성 HTTP 응답과 발송 후 실패를 격리 검증한다."""
import json
import tempfile
import traceback
import unittest
import warnings
from pathlib import Path
from unittest.mock import Mock, patch


import httpx
from openai import OpenAI
from pydantic import BaseModel

from llm import LLM, LLMError
from 호출예산 import BudgetExceeded, RequestBudget


class Answer(BaseModel):
    ok: bool


def envelope(**changes):
    body = {"id": "test", "object": "chat.completion", "created": 1, "model": "dummy",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": '{"ok": true}'}}]}
    body.update(changes)
    return body


class ResponseRecoveryTests(unittest.TestCase):
    def test_owned_client_is_closed_once(self):
        client, wrapped = Mock(), Mock()
        client.with_options.return_value = wrapped
        wrapped._client.event_hooks = {"request": []}
        cfg = {"api_key_env": "TEST_CLIENT_KEY", "base_url": "https://dummy.invalid/v1", "models": {"select": "dummy"}}
        with patch.dict("os.environ", {"TEST_CLIENT_KEY": "dummy-local-key"}), patch("llm.OpenAI", return_value=client):
            model = LLM(cfg, budget=object())
        model.close()
        model.close()
        wrapped.close.assert_called_once_with()

    def test_injected_client_remains_owned_by_caller(self):
        model, _, _, _ = self.model(httpx.Response(200, json=envelope()))
        original_transport = model.client._client
        model.close()
        self.assertFalse(original_transport.is_closed)

    def model(self, response, *, limit=2):
        temporary = tempfile.TemporaryDirectory(dir=Path.home() / "AppData/Local/hermes/cache/scratch")
        self.addCleanup(temporary.cleanup)
        budget = RequestBudget(Path(temporary.name) / "budget.sqlite3", limit)
        sent, events = [], []
        def handler(request):
            sent.append(budget.snapshot()["used"])
            return response
        client = OpenAI(api_key="dummy-not-a-secret", base_url="https://dummy.invalid/v1",
                        http_client=httpx.Client(transport=httpx.MockTransport(handler), trust_env=False))
        self.addCleanup(client.close)
        class Metrics:
            def emit(self, event, **fields):
                events.append({"event": event, **fields})
        model = LLM({"models": {"select": "dummy"}, "max_retries": 0},
                    client=client, budget=budget, metrics=Metrics())
        return model, budget, sent, events

    def assert_safe_failure(self, response, expected):
        model, budget, sent, events = self.model(response)
        with self.assertRaises(LLMError) as caught:
            model.parse("select", "system", "user", Answer)
        self.assertEqual(str(caught.exception), expected)
        self.assertNotIn("private-marker", "".join(traceback.format_exception(caught.exception)))
        self.assertNotIn("private-marker", json.dumps(events))
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertEqual(sent, [1])
        self.assertEqual(budget.snapshot()["used"], 1)
        self.assertEqual(model.calls, 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["error"], expected)

    def test_malformed_http200_json_is_safe_model_failure(self):
        self.assert_safe_failure(httpx.Response(200, content=b'private-marker {',
                                                headers={"content-type": "application/json"}), "JSONDecodeError")

    def test_response_envelope_is_checked(self):
        cases = [None, [], "private-marker", {}, envelope(choices=[]), envelope(choices=None),
                 envelope(choices="private-marker"), envelope(choices=[None]),
                 envelope(choices=[{"index": 0, "finish_reason": "stop", "message": None}]),
                 envelope(choices=[{"index": 0, "finish_reason": "stop", "message": "private-marker"}]),
                 envelope(choices=[{"index": 0, "finish_reason": "stop", "message": {}}])]
        for body in cases:
            with self.subTest(body=body):
                self.assert_safe_failure(httpx.Response(200, content=json.dumps(body),
                                        headers={"content-type": "application/json"}), "invalid_response")

    def test_positive_control_returns_typed_answer(self):
        model, budget, sent, events = self.model(httpx.Response(200, json=envelope()))
        answer = model.parse("select", "system", "user", Answer)
        self.assertIsInstance(answer, Answer)
        self.assertIs(answer.ok, True)
        self.assertEqual(sent, [1])
        self.assertEqual(budget.snapshot()["used"], 1)
        self.assertEqual(model.calls, 1)
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0]["error"])

    def test_invalid_envelope_metadata_does_not_warn_with_raw_values(self):
        for changes in ({"model": {"private-marker": True}},
                        {"usage": {"prompt_tokens": "private-marker", "completion_tokens": 1, "total_tokens": 2}}):
            with self.subTest(changes=changes), warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                self.assert_safe_failure(httpx.Response(200, json=envelope(**changes)), "invalid_response")
                self.assertEqual(caught, [])

    def test_sdk_error_is_sanitized_once(self):
        self.assert_safe_failure(httpx.Response(401, json={"error": {"message": "private-marker"}}),
                                 "AuthenticationError")

    def test_budget_remains_fatal_and_unlogged(self):
        model, budget, sent, events = self.model(httpx.Response(200, json=envelope()), limit=0)
        with self.assertRaises(BudgetExceeded):
            model.parse("select", "system", "user", Answer)
        self.assertEqual(sent, [])
        self.assertEqual(events, [])
        self.assertEqual(model.calls, 0)
        self.assertEqual(budget.snapshot()["used"], 0)
