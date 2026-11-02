"""새 표본 경로가 위젯·수신·평가까지 이어지고 기본 자료는 보존되는지 확인한다."""
import http.client
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from tools import make_label_widget as widget, label_receiver as receiver, eval_selection as evaluation
from tests import 테스트_도구경계 as fixtures


class NewSnapshotFlowTests(unittest.TestCase):
    def test_survey_candidate_policy_uses_actual_hostname(self):
        from tools import survey_sources as survey
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "후보.json"
            data = {"excluded_by_assignment": [{"host": "techcrunch.com"}], "topic_keywords": [],
                    "candidates": [{"id": "safe", "url": "https://nottechcrunch.com/feed"}]}
            with patch.object(survey, "CANDIDATES_FILE", source):
                source.write_text(json.dumps(data), encoding="utf-8")
                self.assertEqual(survey.load_spec()[0][0]["id"], "safe")
                data["candidates"][0]["url"] = "https://%74echcrunch.com/feed"
                source.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(SystemExit):
                    survey.load_spec()

    def test_survey_inline_samples_keep_shared_host_exclusion(self):
        from contextlib import nullcontext
        from tools import survey_sources as survey
        from tests.support import NOW, atom, FakeRobots
        feed = atom([{"title": "encoded", "link": "https://%74echcrunch.com/a", "hours_ago": 1, "body": "body " * 200}])
        class HTTP:
            def _budget(self):
                return nullcontext()
            def get(self, url, **kwargs):
                return {"status": 200, "ms": 1, "bytes": len(feed), "final_url": url, "content_type": "application/atom+xml", "error": None, "content": feed}
        candidate = {"id": "api", "name": "test", "route": "api_arxiv", "tier": 1, "lang": "en", "url": "https://safe.example/feed"}
        result = survey.survey_one(HTTP(), FakeRobots(), candidate, [], ["techcrunch.com"], NOW)
        self.assertEqual(result["body_samples"], [])

    def test_snapshot_command_closes_owned_http(self):
        from tools import snapshot_candidates as snapshot
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            http = Mock(count=0)
            settings = {"cfg": {"collect": {"user_agent": "test", "request_timeout_s": 1, "hours": 48}}}
            with patch.object(snapshot, "load_settings", return_value=settings), patch.object(snapshot, "Http", return_value=http), \
                 patch.object(snapshot.collectors, "collect", return_value=([], {})), patch("sys.argv", ["snapshot", "--out", str(root / "새표본.json")]):
                snapshot.main()
            http.close.assert_called_once_with()

    def test_survey_uses_and_closes_one_owned_http(self):
        from tools import survey_sources as survey
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            http = Mock(count=0)
            with patch.object(survey, "ROOT", root), patch.object(survey, "OUTPUT_FILE", root / "survey.json"), \
                 patch.object(survey, "load_spec", return_value=([], [], [])), patch.object(survey, "Http", return_value=http) as factory, \
                 patch("sys.argv", ["survey"]):
                survey.main()
            self.assertEqual(factory.call_count, 1)
            http.close.assert_called_once_with()

    def test_new_snapshot_flows_to_labels_and_evaluation_without_replacing_defaults(self):
        import config
        import graph
        import llm
        class Model:
            calls = 0
            def __init__(self, *args):
                pass
        class Pipeline:
            def __init__(self, *args, **kwargs):
                pass
            def select(self, state):
                return {"shortlisted": state["collected"], "picked": [], "selection_status": "model", "selection_fallbacks": []}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "eval").mkdir()
            default_snapshot, default_labels = root / "eval/candidates.json", root / "eval/labels.json"
            default_snapshot.write_bytes(b"historical-default-snapshot")
            default_labels.write_bytes(b"historical-default-labels")
            new_snapshot, new_labels = root / "eval/새표본.json", root / "eval/새라벨.json"
            new_snapshot.write_text(json.dumps(fixtures.candidates()), encoding="utf-8")
            html = root / "eval/새위젯.html"
            with patch.object(widget, "ROOT", root), patch.object(widget, "SRC", default_snapshot):
                widget.build(html, candidates_path=new_snapshot)
            data = json.loads(re.search(r"const DATA = (.*);", html.read_text(encoding="utf-8")).group(1))
            ref = {"candidates_path": new_snapshot, "out_path": new_labels}
            server = receiver.HTTPServer(("127.0.0.1", 0), receiver.make_handler(ref))
            ref["server"] = server
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            with patch.object(receiver, "ROOT", root), patch.object(receiver, "CANDIDATES", default_snapshot), patch.object(receiver, "OUT", default_labels):
                thread.start()
                try:
                    payload = {"snapshot": data["snapshot"], "labels": {i["id"]: 2 for i in data["items"]},
                               "session_id": data["session_id"], "token": data["token"]}
                    conn = http.client.HTTPConnection(*server.server_address, timeout=2)
                    conn.request("POST", "/labels", json.dumps(payload), {"Origin": "null"})
                    response = conn.getresponse(); reply = json.loads(response.read()); conn.close()
                    self.assertEqual((response.status, reply.get("count")), (200, 25))
                    thread.join(2)
                    self.assertFalse(thread.is_alive())
                finally:
                    if thread.is_alive():
                        server.shutdown()
                    server.close_connections(); server.server_close(); thread.join(2)
            settings = {"root": root, "cfg": {"run": {"store_dir": "store"}, "llm": {"models": {}}},
                        "audience": {"importance": "test"}}
            with patch.object(evaluation, "ROOT", root), patch.object(evaluation, "OUT", root / "eval/selection_eval.json"), \
                 patch.object(evaluation, "CANDIDATES", default_snapshot), patch.object(evaluation, "LABELS", default_labels), \
                 patch.object(config, "load_settings", return_value=settings), patch.object(graph, "Pipeline", Pipeline), patch.object(llm, "LLM", Model), \
                 patch("sys.argv", ["eval", "--candidates", str(new_snapshot), "--labels", str(new_labels), "--repeats", "1"]):
                self.assertEqual(evaluation.main(), 0)
            result_path = next((root / "eval").glob("selection_eval-*.json"))
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(result["items"], 25)
            self.assertEqual(result["clock_source"], "snapshot_at_kst")
            self.assertEqual(default_snapshot.read_bytes(), b"historical-default-snapshot")
            self.assertEqual(default_labels.read_bytes(), b"historical-default-labels")

if __name__ == "__main__":
    unittest.main()
