"""연결 확인의 한 번 요청·실패 기록 경계. 실제 제공자는 부르지 않는다."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.연결확인 import check_connection
from llm import LLMError


class ConnectionTests(unittest.TestCase):
    def test_one_structured_request_is_persisted_without_retry(self):
        calls = []

        class Budget:
            def snapshot(self):
                used = len(calls)
                return {"limit": 65, "used": used, "remaining": 65 - used}

        class Probe:
            def __init__(self, cfg, metrics):
                self.cfg, self.metrics, self.calls, self.budget = cfg, metrics, 0, Budget()
                if cfg.get("max_retries") != 0 or cfg.get("temperature") is not None:
                    raise AssertionError("연결 확인은 재시도 없이 한 번만 요청해야 함")
            def parse(self, stage, system, user, schema):
                calls.append(stage)
                self.calls += 1
                self.metrics.emit("llm_call", model="observed-test-model", prompt_tokens=10, completion_tokens=5)
                return schema(marker="newsletter-connection-ok")

        with tempfile.TemporaryDirectory() as tmp:
            settings = {"root": Path(tmp), "cfg": {"run": {"store_dir": "store"},
                        "llm": {"models": {"select": "requested-test-model"}, "max_retries": 2, "temperature": 0}}}
            result = check_connection(settings, llm_factory=Probe)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(calls, ["select_connection"])
            self.assertEqual(result["budget_after"]["used"], 1)
            self.assertEqual(result["observed_model"], "observed-test-model")
            saved = json.loads((Path(tmp) / "store/연결확인.json").read_text(encoding="utf-8"))
            self.assertEqual(saved, result)

    def test_provider_failure_is_recorded_without_exception_text(self):
        class Probe:
            calls = 0
            budget = type("Budget", (), {"snapshot": lambda self: {"limit": 65, "used": 1, "remaining": 64}})()
            def __init__(self, cfg, metrics):
                pass
            def parse(self, *args):
                self.calls += 1
                raise LLMError("opaque-sensitive-provider-message")
        with tempfile.TemporaryDirectory() as tmp:
            settings = {"root": Path(tmp), "cfg": {"run": {"store_dir": "store"},
                        "llm": {"models": {"select": "test"}}}}
            result = check_connection(settings, llm_factory=Probe)
            self.assertEqual(result["status"], "error")
            self.assertNotIn("opaque-sensitive-provider-message", json.dumps(result))
            self.assertTrue((Path(tmp) / "store/연결확인.json").is_file())


if __name__ == "__main__":
    unittest.main()
