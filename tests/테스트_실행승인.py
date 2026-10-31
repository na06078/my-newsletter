"""실제 CLI(run.py)의 발송 동작. 모델·외부 발송은 명시적 대역으로 막는다.

뉴스 에이전트는 실행하면 발행까지 끝내는 것이 기본이다(명세서 11절). 사람이 해시를 넣어 승인하는 명령은 없다.
"""
from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run
from 발행승인 import PublicationError, prepare_publication

EMBEDS = [{"title": "검수한 뉴스", "description": "첫째입니다. 둘째입니다.", "url": "https://example.org/news"}]


class RunCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name) / "store"
        self.settings = {"root": Path(self.temp.name), "cfg": {
            "run": {"store_dir": "store"}, "publish": {"title": "시험 뉴스", "total_chars_max": 5800}}}
        self.manifest = prepare_publication(self.store, "run-auto", EMBEDS)
        article = {"headline": "검수한 뉴스", "source": "시험 소스", "title": "t", "reason": "r"}
        self.final = {"log": ["⑤ 발행   시험"], "verified": [article], "rejected": [], "run_id": "run-auto",
                      "published": {"status": "sent", "sha256": self.manifest["sha256"], "message_id": "123",
                                    "readback": {"status": "verified"}}}

    def run_main(self, *argv, final=None):
        final = final or self.final
        with patch("sys.argv", ["run.py", *argv]), patch.object(run, "load_settings", return_value=self.settings), \
                patch.object(run, "run_pipeline", return_value=final) as pipeline, \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                code = run.main()
            except SystemExit as stopped:
                code = stopped.code
        return code, pipeline, out.getvalue(), err.getvalue()

    def test_default_run_publishes_without_any_human_step(self):
        code, pipeline, out, _ = self.run_main()
        self.assertEqual(code, 0)
        pipeline.assert_called_once_with(self.settings, publish=True, hours=None)   # 플래그 없이 기본이 발송
        self.assertIn("발행 상태: sent", out)
        self.assertIn(self.manifest["sha256"], out)
        self.assertIn("검수한 뉴스", out)

    def test_dry_run_prepares_but_does_not_publish(self):
        final = {**self.final, "published": {"status": "prepared", "sha256": self.manifest["sha256"]}}
        code, pipeline, out, _ = self.run_main("--dry-run", final=final)
        self.assertEqual(code, 0)
        pipeline.assert_called_once_with(self.settings, publish=False, hours=None)
        self.assertIn("발행 상태: prepared", out)

    def test_hours_is_passed_through(self):
        _, pipeline, _, _ = self.run_main("--hours", "24")
        pipeline.assert_called_once_with(self.settings, publish=True, hours=24)

    def test_send_failure_and_readback_mismatch_are_exit_code_2(self):
        for published in ({"status": "failed", "sha256": self.manifest["sha256"], "readback": {"status": "unavailable"}},
                          {"status": "sent", "sha256": self.manifest["sha256"], "readback": {"status": "mismatch"}}):
            with self.subTest(published=published):
                code, _, out, _ = self.run_main(final={**self.final, "published": published})
                self.assertEqual(code, 2)
                self.assertIn("발행 상태: " + published["status"], out)

    def test_approval_commands_no_longer_exist(self):
        for argv in (["--publish"], ["--send-prepared", "run-auto", "--confirm-hash", "0" * 64], ["--confirm-hash", "0" * 64]):
            with self.subTest(argv=argv):
                code, pipeline, _, err = self.run_main(*argv)
                self.assertEqual(code, 2)                                          # argparse: 알 수 없는 인자
                self.assertIn("unrecognized arguments", err)
                pipeline.assert_not_called()

    def test_show_prepared_is_read_only_and_detects_tampering(self):
        code, pipeline, out, _ = self.run_main("--show-prepared", "run-auto")
        self.assertEqual(code, 0)
        pipeline.assert_not_called()
        self.assertIn(self.manifest["sha256"], out)
        path = self.store / "cache/publications/run-auto.json"
        path.write_text(path.read_text(encoding="utf-8").replace("첫째입니다", "바꿔치기"), encoding="utf-8")
        code, pipeline, _, err = self.run_main("--show-prepared", "run-auto")
        self.assertEqual(code, 1)
        self.assertIn("hash_mismatch", err)
        pipeline.assert_not_called()

    def test_hours_cannot_be_combined_with_show_prepared(self):
        code, pipeline, _, _ = self.run_main("--show-prepared", "run-auto", "--hours", "24")
        self.assertEqual(code, 2)
        pipeline.assert_not_called()


class RunPipelineEntryTests(unittest.TestCase):
    """graph.run_pipeline 의 발송 전 점검과 발송기 연결. 모델·네트워크는 대역."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = {"root": Path(self.temp.name), "audience": {"importance": "시험 기준"}, "cfg": {
            "run": {"store_dir": "store"}, "collect": {"hours": 48, "sources": [], "user_agent": "isolated-test", "request_timeout_s": 1},
            "select": {"batch_size": 20}, "llm": {"models": {"select": "dummy"}},
            "publish": {"webhook_env": "UNUSED_TEST_WEBHOOK"}}}
        self.final = {"run_id": "x", "collected": [], "shortlisted": [], "picked": [], "verified": [], "rejected": [],
                      "published": {"status": "prepared"}, "log": []}

    def call(self, *, publish, webhook):
        import graph
        env = {"UNUSED_TEST_WEBHOOK": webhook} if webhook else {}
        with patch.dict(os.environ, env), patch.object(graph, "LLM") as llm, patch.object(graph, "Http") as http, \
                patch.object(graph, "Pipeline") as pipeline:
            llm.return_value.calls, http.return_value.count = 0, 0
            pipeline.return_value.build.return_value.invoke.return_value = self.final
            try:
                graph.run_pipeline(self.settings, publish=publish)
                error = None
            except PublicationError as exc:
                error = exc
            return error, llm, pipeline

    def test_missing_webhook_stops_before_any_model_call(self):
        error, llm, pipeline = self.call(publish=True, webhook=None)
        self.assertIsNotNone(error)
        self.assertIn("webhook_not_configured", str(error))
        llm.assert_not_called()                                                    # 유료 모델 호출 전에 중단
        pipeline.assert_not_called()

    def test_publish_wires_a_live_publisher_and_the_store(self):
        error, llm, pipeline = self.call(publish=True, webhook="https://delivery.example.invalid/incoming")
        self.assertIsNone(error)
        kwargs = pipeline.call_args.kwargs
        self.assertIs(kwargs["publisher"].live, True)
        self.assertEqual(Path(kwargs["store"]), Path(self.temp.name) / "store")

    def test_dry_run_needs_no_webhook_and_gets_no_publisher(self):
        error, llm, pipeline = self.call(publish=False, webhook=None)
        self.assertIsNone(error)
        self.assertIsNone(pipeline.call_args.kwargs["publisher"])


if __name__ == "__main__":
    unittest.main()
