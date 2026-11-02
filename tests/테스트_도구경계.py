"""도구 경계: 실제 데이터 대신 임시 경로와 명시적 모델 대역만 사용한다."""
import json
import http.client
import socket
import threading
import time
import tempfile
import unittest
from pathlib import Path
from contextlib import contextmanager, ExitStack
from unittest.mock import patch

from tools import negative_control as nc
from tools import eval_selection as ev
from tools import label_receiver as receiver
from tests.test_tools import DRAFT
from tools import make_label_widget as widget


def candidates():
    return {"snapshot_at_kst": "2026-11-02T10:00:00+09:00", "items": [
        {"id": f"i{i}", "title": "기사", "url": "https://example.org", "source": "test",
         "summary": "요약", "published_at": None} for i in range(25)]}


def labels():
    return {"snapshot": candidates()["snapshot_at_kst"], "labels": {f"i{i}": 2 for i in range(25)}}


class MetricBoundaries(unittest.TestCase):
    def test_problem_list_rejection_is_attributed_to_judge(self):
        def contradictory(_):
            return {"ok": False, "rule_problems": [], "judge_ok": True, "error": None,
                    "problems": [{"note": "unsupported claim"}]}
        rows = nc.run_control([DRAFT], contradictory, kinds=("overstate",))
        self.assertEqual(nc.summarize(rows)["overstate"]["by_judge"], 1)

    def test_errors_are_not_catches_or_false_positives(self):
        def failed(_):
            return {"ok": False, "rule_problems": [], "judge_ok": None,
                    "error": "LLMError", "problems": []}
        rows = nc.run_control([DRAFT], failed)
        summary = nc.summarize(rows)
        self.assertIsNone(rows[0]["false_positive"])
        self.assertEqual(rows[0]["error"], "LLMError")
        for kind in (*nc.KINDS, "control"):
            self.assertEqual(summary[kind]["evaluated"], 0)
            self.assertEqual(summary[kind]["errors"], 1)
        self.assertEqual(summary["fabricate"]["caught"], 0)
        self.assertEqual(summary["control"]["false_positives"], 0)


class SurveyBoundaries(unittest.TestCase):
    def test_sources_record_their_own_measurement_start_times(self):
        from tools import survey_sources as survey
        from datetime import datetime, timezone, timedelta
        class Clock:
            tick = 0
            @classmethod
            def now(cls, tz):
                result = datetime(2026, 11, 2, tzinfo=timezone.utc) + timedelta(minutes=cls.tick)
                cls.tick += 1
                return result
        class HTTP:
            count = 0
        sources = [{"id": i} for i in ["one", "two"]]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); out = root / "survey.json"
            with patch.object(survey, "ROOT", root), patch.object(survey, "OUTPUT_FILE", out), \
                 patch.object(survey, "datetime", Clock), patch.object(survey, "print_table"), \
                 patch.object(survey, "load_spec", return_value=(sources, [], [])), \
                 patch.object(survey, "Http", HTTP), patch.object(survey, "survey_one", side_effect=lambda h, r, c, k, e, n: dict(c)), \
                 patch("sys.argv", ["survey"]):
                survey.main()
            result = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual([s["surveyed_at_utc"] for s in result["sources"]],
                             ["2026-11-02T00:01:00+00:00", "2026-11-02T00:02:00+00:00"])

    def test_partial_survey_keeps_legacy_time_and_renders_each_source_time(self):
        from tools import survey_sources as survey
        from tools import render_survey_table as table
        import yaml
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out, decisions = root / "survey.json", root / "decisions.yaml"
            old = {"id": "old", "name": "old", "route": "rss", "tier": "A"}
            fresh = {"id": "fresh", "name": "fresh", "route": "rss", "tier": "A"}
            out.write_text(json.dumps({"surveyed_at_kst": "legacy-kst", "surveyed_at_utc": "legacy-utc", "sources": [old]}), encoding="utf-8")
            class HTTP:
                count = 0
            with patch.object(survey, "ROOT", root), patch.object(survey, "OUTPUT_FILE", out), \
                 patch.object(survey, "load_spec", return_value=([fresh], [], [])), \
                 patch.object(survey, "Http", HTTP), patch.object(survey, "survey_one", return_value=fresh), \
                 patch("sys.argv", ["survey", "--only", "fresh"]):
                survey.main()
            result = json.loads(out.read_text(encoding="utf-8"))
            sources = {s["id"]: s for s in result["sources"]}
            self.assertEqual(sources["old"]["surveyed_at_kst"], "legacy-kst")
            self.assertEqual(sources["old"]["surveyed_at_utc"], "legacy-utc")
            self.assertNotEqual(sources["fresh"]["surveyed_at_kst"], "legacy-kst")
            decisions.write_text(yaml.safe_dump({i: {"decision": "채택", "reason": "test"} for i in sources}), encoding="utf-8")
            with patch.object(table, "SURVEY", out), patch.object(table, "DECISIONS", decisions):
                rendered = table.render()
            self.assertIn("legacy-kst", rendered)
            self.assertIn(sources["fresh"]["surveyed_at_kst"], rendered)


class WidgetBoundaries(unittest.TestCase):
    def test_existing_widget_is_preserved_without_new_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); src, out = root / "candidates.json", root / "기존_위젯.html"
            src.write_text(json.dumps(candidates()), encoding="utf-8")
            out.write_bytes(b"historical-widget")
            with patch.object(widget, "ROOT", root), patch.object(widget, "SRC", src):
                with self.assertRaises(FileExistsError):
                    widget.build(out)
            self.assertEqual(out.read_bytes(), b"historical-widget")
            self.assertFalse((root / "store/cache/label_sessions").exists())

    def test_build_creates_private_expiring_session_without_leaking_to_manual_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src, out = root / "candidates.json", root / "새_위젯.html"
            src.write_text(json.dumps(candidates()), encoding="utf-8")
            with patch.object(widget, "ROOT", root), patch.object(widget, "SRC", src):
                widget.build(out)
            sessions = list((root / "store/cache/label_sessions").glob("*.json"))
            self.assertEqual(len(sessions), 1)
            session = json.loads(sessions[0].read_text(encoding="utf-8"))
            html = out.read_text(encoding="utf-8")
            self.assertGreaterEqual(len(session["token"]), 32)
            self.assertTrue(session["token"] in html, "생성 HTML 에 세션 인증값이 필요합니다")
            self.assertEqual(session["snapshot"], candidates()["snapshot_at_kst"])
            self.assertAlmostEqual(session["expires_at"] - session["created_at"], 1800, delta=1)
            self.assertIn('JSON.stringify({snapshot: DATA.snapshot, labels: labels})', html)
            self.assertIn('session_id: DATA.session_id, token: DATA.token', html)
            self.assertTrue(session["token"] not in sessions[0].name, "파일명에 인증값을 넣으면 안 됩니다")

    def test_receiver_rejects_missing_token_without_writing_labels(self):
        from http.server import HTTPServer
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cp, out = root / "candidates.json", root / "labels.json"
            cp.write_text(json.dumps(candidates()), encoding="utf-8")
            ref = {}
            server = HTTPServer(("127.0.0.1", 0), receiver.make_handler(ref)); ref["server"] = server
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                with patch.object(receiver, "ROOT", root), patch.object(receiver, "CANDIDATES", cp), patch.object(receiver, "OUT", out):
                    conn = http.client.HTTPConnection(*server.server_address, timeout=2)
                    conn.request("POST", "/labels", json.dumps(labels()), {"Origin": "null"})
                    response = conn.getresponse(); data = json.loads(response.read()); conn.close()
                    self.assertEqual(response.status, 403)
                    self.assertEqual(data["error"], "invalid_session")
                    self.assertFalse(out.exists())
            finally:
                server.shutdown(); server.server_close(); thread.join(2)


class ValidationBoundaries(unittest.TestCase):
    def test_repeated_label_ids_in_json_are_rejected_before_model(self):
        import config
        with tempfile.TemporaryDirectory() as tmp:
            cp, lp = Path(tmp) / "candidates.json", Path(tmp) / "labels.json"
            cp.write_text(json.dumps(candidates()), encoding="utf-8")
            text = json.dumps(labels()).replace('"i0": 2', '"i0": 0, "i0": 2')
            lp.write_text(text, encoding="utf-8")
            with patch.object(ev, "CANDIDATES", cp), patch.object(ev, "LABELS", lp), patch("sys.argv", ["eval"]), \
                 patch.object(config, "load_settings", side_effect=AssertionError("duplicate input reached model boundary")):
                self.assertEqual(ev.main(), 1)

    def test_eval_mains_preserve_fixed_outputs_and_create_exclusive_runs(self):
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
                return {"shortlisted": state["collected"], "picked": []}
            def check_draft(self, draft):
                return {"ok": True, "rule_problems": [], "judge_ok": True, "error": None, "problems": []}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "eval").mkdir()
            (root / "store/cache").mkdir(parents=True)
            cp, lp = root / "eval/candidates.json", root / "eval/labels.json"
            cp.write_text(json.dumps(candidates()), encoding="utf-8")
            lp.write_text(json.dumps(labels()), encoding="utf-8")
            cache = root / "store/cache/source.json"
            cache.write_text(json.dumps({"verified": [{"article_id": "a1", "attempt": 0}], "drafted": [DRAFT]}), encoding="utf-8")
            settings = {"root": root, "cfg": {"run": {"store_dir": "store"}, "llm": {"models": {}}},
                        "audience": {"importance": "test"}}
            with patch.object(config, "load_settings", return_value=settings), patch.object(graph, "Pipeline", Pipeline), patch.object(llm, "LLM", Model):
                for module, fixed in [(ev, "selection_eval.json"), (nc, "negative_control.json")]:
                    old = root / "eval" / fixed
                    old.write_bytes(b"historical")
                    with patch.object(module, "ROOT", root), patch.object(module, "OUT", old), patch("sys.argv", ["eval"]):
                        with patch.object(ev, "CANDIDATES", cp), patch.object(ev, "LABELS", lp):
                            self.assertEqual(module.main(), 0)
                            self.assertEqual(module.main(), 0)
                    self.assertEqual(old.read_bytes(), b"historical")
                    outputs = list((root / "eval").glob(f"{Path(fixed).stem}-*.json"))
                    self.assertEqual(len(outputs), 2)
                    for output in outputs:
                        result = json.loads(output.read_text(encoding="utf-8"))
                        self.assertEqual(output.stem, result["run_id"])

    def test_invalid_input_stops_before_settings_or_model_creation(self):
        bad_cases = []
        c, l = candidates(), labels()
        bad_cases.append((c, {**l, "snapshot": "wrong"}))
        for bad in [True, 1.0, "2", 3]:
            bad_cases.append((c, {**l, "labels": {**l["labels"], "i0": bad}}))
        bad_cases.append((c, {**l, "labels": {**l["labels"], "unknown": 2}}))
        bad_cases.append((c, {**l, "labels": {k: v for k, v in l["labels"].items() if k != "i0"}}))
        bad_cases.append(({**c, "items": c["items"] + [c["items"][0]]}, l))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cp, lp = root / "candidates.json", root / "labels.json"
            import config
            for c, l in bad_cases:
                with self.subTest(case=len(c["items"]), labels=l):
                    cp.write_text(json.dumps(c), encoding="utf-8")
                    lp.write_text(json.dumps(l), encoding="utf-8")
                    with patch.object(ev, "CANDIDATES", cp), patch.object(ev, "LABELS", lp), \
                         patch("sys.argv", ["eval_selection.py"]), patch.object(config, "load_settings", side_effect=AssertionError("invalid input reached settings/model boundary")) as factory:
                        self.assertEqual(ev.main(), 1)
                        factory.assert_not_called()
            for bad in [True, 1.0]:
                l = labels(); l["labels"]["i0"] = bad
                clean, error = receiver.validate(l, candidates())
                self.assertIsNone(clean)
                self.assertIsNotNone(error)


class ReceiverRuntime(unittest.TestCase):
    @contextmanager
    def runtime(self, *, existing=False, wait=2):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            cp, out = root / "candidates.json", root / "labels.json"
            cp.write_text(json.dumps(candidates()), encoding="utf-8")
            with patch.object(widget, "ROOT", root), patch.object(widget, "SRC", cp):
                widget.build(root / "새_위젯.html")
            session_path = next((root / "store/cache/label_sessions").glob("*.json"))
            session = json.loads(session_path.read_text(encoding="utf-8"))
            if existing:
                out.write_bytes(b"historical-labels")
            for name, value in [("ROOT", root), ("CANDIDATES", cp), ("OUT", out), ("PORT", 0), ("WAIT_SECONDS", wait)]:
                stack.enter_context(patch.object(receiver, name, value))
            created, ready, result = [], threading.Event(), []
            original = receiver.HTTPServer
            def factory(*args, **kwargs):
                server = original(*args, **kwargs); created.append(server); ready.set(); return server
            stack.enter_context(patch.object(receiver, "HTTPServer", factory))
            thread = threading.Thread(target=lambda: result.append(receiver.main()), daemon=True)
            thread.start(); self.assertTrue(ready.wait(2))
            server = created[0]
            try:
                yield server, thread, session, session_path, out, result
            finally:
                if thread.is_alive():
                    server.shutdown()
                server.server_close(); thread.join(2)

    def post(self, server, payload, origin="null", headers=None):
        conn = http.client.HTTPConnection(*server.server_address, timeout=2)
        conn.request("POST", "/labels", json.dumps(payload), {"Origin": origin, **(headers or {})})
        response = conn.getresponse(); body = json.loads(response.read()); cors = response.getheader("Access-Control-Allow-Origin")
        conn.close()
        return response.status, body, cors

    def payload(self, session):
        return {**labels(), "session_id": session["session_id"], "token": session["token"]}

    def test_existing_labels_cannot_be_overwritten(self):
        with self.runtime(existing=True) as (server, thread, session, _, out, result):
            code, body, cors = self.post(server, self.payload(session))
            self.assertEqual(code, 409)
            self.assertEqual(body["error"], "labels_exist")
            self.assertEqual(out.read_bytes(), b"historical-labels")

    def test_real_post_from_generated_html_rejects_bad_token_then_saves_and_closes(self):
        import re
        with self.runtime() as (server, thread, session, path, out, result):
            html = (out.parent / "새_위젯.html").read_text(encoding="utf-8")
            data = json.loads(re.search(r"const DATA = (.*);", html).group(1))
            payload = {"snapshot": data["snapshot"], "labels": {i["id"]: 2 for i in data["items"]},
                       "session_id": data["session_id"], "token": data["token"]}
            code, body, _ = self.post(server, {**payload, "token": "wrong"})
            self.assertEqual((code, body.get("error")), (403, "invalid_session"))
            self.assertFalse(out.exists())
            code, body, cors = self.post(server, payload)
            self.assertEqual((code, body, cors), (200, {"ok": True, "count": 25}, "null"))
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result, [0])
            saved = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(saved["labels"], labels()["labels"])
            self.assertTrue(data["token"] not in out.read_text(encoding="utf-8"), "라벨 파일은 인증값을 저장하지 않습니다")
            with self.assertRaises(OSError):
                socket.create_connection(server.server_address, timeout=0.2)

    def test_expired_or_changed_snapshot_session_cannot_save(self):
        with self.runtime() as (server, thread, session, path, out, result):
            path.write_text(json.dumps({**session, "expires_at": time.time() - 1}), encoding="utf-8")
            code, body, _ = self.post(server, self.payload(session))
            self.assertEqual((code, body.get("error")), (403, "invalid_session"))
            path.write_text(json.dumps(session), encoding="utf-8")
            code, body, _ = self.post(server, {**self.payload(session), "snapshot": "wrong"})
            self.assertEqual((code, body.get("error")), (403, "invalid_session"))
            candidate_path = out.parent / "candidates.json"
            changed = candidates(); changed["items"][0]["title"] = "changed under same timestamp"
            candidate_path.write_text(json.dumps(changed), encoding="utf-8")
            code, body, _ = self.post(server, self.payload(session))
            self.assertEqual((code, body.get("error")), (403, "invalid_session"))
            self.assertFalse(out.exists())

    def test_default_read_deadline_closes_slow_body_but_server_still_accepts_valid_post(self):
        with self.runtime(wait=8) as (server, thread, session, path, out, result):
            slow = socket.create_connection(server.server_address, timeout=7)
            started = time.monotonic()
            try:
                slow.sendall(b"POST /labels HTTP/1.1\r\nHost: localhost\r\nOrigin: null\r\nContent-Length: 100\r\n\r\n{")
                # Slow bytes do not restart the absolute five-second budget.
                for _ in range(3):
                    time.sleep(1)
                    slow.sendall(b" ")
                try:
                    data = slow.recv(2048)
                    self.assertFalse(data)
                except (ConnectionError, OSError):
                    pass
                self.assertLess(time.monotonic() - started, 6.5)
                self.assertFalse(out.exists())
                self.assertTrue(thread.is_alive())
                code, body, _ = self.post(server, self.payload(session))
                self.assertEqual(code, 200)
                thread.join(2)
                self.assertEqual(result, [0])
            finally:
                slow.close()

    def test_foreign_origin_and_oversized_body_are_rejected(self):
        with self.runtime() as (server, thread, session, _, out, result):
            code, body, cors = self.post(server, self.payload(session), origin="https://evil.example")
            self.assertEqual(code, 403)
            self.assertEqual((body.get("error"), cors), ("origin_not_allowed", None))
            code, body, cors = self.post(server, self.payload(session), headers={"Content-Length": "131073"})
            self.assertEqual((code, body["error"]), (413, "body_too_large"))
            self.assertFalse(out.exists())

    def test_deadline_closes_active_slow_connection_and_listener(self):
        with self.runtime(wait=0.3) as (server, thread, session, _, out, result):
            slow = socket.create_connection(server.server_address, timeout=2)
            try:
                slow.sendall(b"POST /labels HTTP/1.1\r\nHost: localhost\r\nOrigin: null\r\nContent-Length: 100\r\n\r\n{")
                thread.join(1.2)
                self.assertFalse(thread.is_alive(), "deadline must terminate during a blocked body read")
                self.assertEqual(result, [1])
                self.assertFalse(out.exists())
                with self.assertRaises(OSError):
                    socket.create_connection(server.server_address, timeout=0.2)
            finally:
                slow.close()


class ToolCommandIntegration(unittest.TestCase):
    def test_copied_real_build_and_receiver_commands_complete_without_external_services(self):
        import os
        import re
        import shutil
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tools").mkdir(); (root / "eval").mkdir()
            for module in [widget, receiver]:
                source = Path(module.__file__)
                shutil.copy2(source, root / "tools" / source.name)
            (root / "eval/candidates.json").write_text(json.dumps(candidates()), encoding="utf-8")
            # No inherited API keys, dotenv, proxy or provider settings enter either process.
            env = {key: os.environ[key] for key in ["SYSTEMROOT", "WINDIR"] if key in os.environ}
            env["PYTHONUTF8"] = "1"
            built = subprocess.run([sys.executable, "tools/make_label_widget.py"], cwd=root, env=env,
                                   capture_output=True, text=True, timeout=10)
            self.assertEqual(built.returncode, 0, "격리 위젯 생성 명령 실패")
            html = (root / "eval/label_widget.html").read_text(encoding="utf-8")
            data = json.loads(re.search(r"const DATA = (.*);", html).group(1))
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0)); port = reservation.getsockname()[1]
            env["LABEL_PORT"] = str(port)
            process = subprocess.Popen([sys.executable, "tools/label_receiver.py"], cwd=root, env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 5
                while True:
                    try:
                        connection = socket.create_connection(("127.0.0.1", port), timeout=0.2)
                        connection.close(); break
                    except OSError:
                        if process.poll() is not None or time.monotonic() > deadline:
                            self.fail("격리 수신기 실행 실패")
                        time.sleep(0.03)
                payload = {"snapshot": data["snapshot"], "labels": {i["id"]: 2 for i in data["items"]},
                           "session_id": data["session_id"], "token": data["token"]}
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                conn.request("POST", "/labels", json.dumps(payload), {"Origin": "null", "Content-Type": "text/plain;charset=UTF-8"})
                response = conn.getresponse(); reply = json.loads(response.read()); conn.close()
                self.assertEqual((response.status, reply), (200, {"ok": True, "count": 25}))
                stdout, stderr = process.communicate(timeout=3)
                self.assertEqual(process.returncode, 0)
                self.assertFalse(stderr, "수신기 표준 오류는 비어 있어야 합니다")
                self.assertTrue(data["token"] not in built.stdout + built.stderr + stdout + stderr, "명령 로그에 인증값이 노출되면 안 됩니다")
                saved = json.loads((root / "eval/labels.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["labels"], labels()["labels"])
                with self.assertRaises(OSError):
                    socket.create_connection(("127.0.0.1", port), timeout=0.2)
            finally:
                if process.poll() is None:
                    process.kill(); process.communicate(timeout=3)
            # Exercise real process termination too, scaling only the 30-minute wait.
            process = subprocess.Popen([sys.executable, "-c",
                "from tools import label_receiver as r; r.WAIT_SECONDS=0.6; raise SystemExit(r.main())"],
                cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            slow = None
            try:
                deadline = time.monotonic() + 5
                while slow is None:
                    try:
                        slow = socket.create_connection(("127.0.0.1", port), timeout=0.2)
                    except OSError:
                        if process.poll() is not None or time.monotonic() > deadline:
                            self.fail("격리 느린 연결 시험 수신기 실행 실패")
                        time.sleep(0.03)
                started = time.monotonic()
                slow.sendall(b"POST /labels HTTP/1.1\r\nHost: localhost\r\nOrigin: null\r\nContent-Length: 100\r\n\r\n{")
                stdout, stderr = process.communicate(timeout=2)
                self.assertEqual(process.returncode, 1)
                self.assertLess(time.monotonic() - started, 1.5)
                self.assertFalse(stderr, "느린 연결 종료가 traceback 을 남기면 안 됩니다")
                self.assertEqual(json.loads((root / "eval/labels.json").read_text(encoding="utf-8"))["labels"], labels()["labels"])
            finally:
                if slow is not None:
                    slow.close()
                if process.poll() is None:
                    process.kill(); process.communicate(timeout=3)
