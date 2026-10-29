"""실제 CLI의 발행 승인 경계. 모델·외부 발송은 명시적 대역으로 막는다."""
from __future__ import annotations

import unittest
import contextlib
import io
import tempfile
from pathlib import Path
from unittest.mock import patch

import run


class RunApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = {"root": Path(self.temp.name), "cfg": {
            "run": {"store_dir": "store"}, "publish": {"title": "시험 뉴스", "total_chars_max": 5800}}}
        article = {"headline": "검수한 뉴스", "summary": "첫째입니다. 둘째입니다. 셋째입니다.",
                   "why": "실습에 참고할 수 있습니다.", "url": "https://example.org/news",
                   "source": "시험 소스", "tier": 1, "published_at": "2026-10-29T01:00:00+00:00"}
        self.final = {"log": [], "verified": [article], "rejected": [], "collected": [article],
                      "shortlisted": [article], "picked": [article],
                      "published": {"status": "dry_run"}, "run_id": "run-approval"}

    def test_legacy_publish_does_not_generate_or_send(self):
        final = {"log": [], "verified": [], "rejected": [],
                 "published": {"status": "sent"}, "run_id": "old-path"}
        with patch("sys.argv", ["run.py", "--publish"]), \
                patch.object(run, "run_pipeline", return_value=final) as pipeline:
            with self.assertRaises(SystemExit) as stopped:
                run.main()
        self.assertEqual(stopped.exception.code, 2)
        pipeline.assert_not_called()

    def test_default_run_creates_frozen_preview_without_sending(self):
        with patch("sys.argv", ["run.py"]), patch.object(run, "load_settings", return_value=self.settings), \
                patch.object(run, "run_pipeline", return_value=self.final) as pipeline, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = run.main()
        self.assertEqual(code, 0)
        pipeline.assert_called_once_with(self.settings, publish=False, hours=None)
        candidate = Path(self.temp.name) / "store/cache/publications/run-approval.json"
        self.assertTrue(candidate.is_file(), "기본 실행은 실제 보낼 내용을 불변 준비본으로 저장해야 함")
        from 발행승인 import load_publication
        manifest = load_publication(Path(self.temp.name) / "store", "run-approval")
        self.assertIn(manifest["sha256"], output.getvalue())
        self.assertIn("검수한 뉴스", output.getvalue())

    def test_send_prepared_reads_same_content_without_generation(self):
        from publisher import DiscordPublisher
        manifest = run.prepare_result(self.settings, self.final)
        sent = []

        class Response:
            status_code = 200
            def __init__(self, data):
                self.data = data
            def json(self):
                return self.data

        def post(*args, **kwargs):
            sent.append(kwargs["json"])
            return Response({"id": "123"})

        def get(*args, **kwargs):
            return Response({"id": "123", "embeds": sent[0]["embeds"],
                             "author": {"username": sent[0]["username"]}})

        pub = DiscordPublisher("https://delivery.example.invalid/incoming", live=True, post=post, get=get)
        argv = ["run.py", "--send-prepared", "run-approval", "--confirm-hash", manifest["sha256"]]
        with patch("sys.argv", argv), patch.object(run, "load_settings", return_value=self.settings), \
                patch.object(run, "run_pipeline") as pipeline, \
                patch.object(DiscordPublisher, "from_env", return_value=pub), \
                contextlib.redirect_stdout(io.StringIO()):
            code = run.main()
        self.assertEqual(code, 0)
        pipeline.assert_not_called()
        self.assertEqual(sent, [manifest["payload"]])

    def test_send_without_hash_is_rejected_before_generation(self):
        with patch("sys.argv", ["run.py", "--send-prepared", "run-approval"]), \
                patch.object(run, "run_pipeline") as pipeline, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                run.main()
        self.assertEqual(stopped.exception.code, 2)
        pipeline.assert_not_called()


    def test_direct_pipeline_cannot_bypass_prepared_approval(self):
        import graph
        settings = {"root": Path(self.temp.name), "audience": {"importance": "시험 기준"}, "cfg": {
            "run": {"store_dir": "store"}, "collect": {"hours": 48, "sources": [], "user_agent": "isolated-test", "request_timeout_s": 1},
            "select": {"batch_size": 20}, "llm": {"models": {"select": "dummy"}},
            "publish": {"webhook_env": "UNUSED_TEST_WEBHOOK"}}}
        with patch.object(graph, "LLM") as llm, patch.object(graph, "Http"), \
                patch.object(graph, "Pipeline") as pipeline:
            llm.return_value.calls = 0
            pipeline.return_value.build.return_value.invoke.return_value = self.final
            with self.assertRaisesRegex(ValueError, "준비본"):
                graph.run_pipeline(settings, publish=True)
            llm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
