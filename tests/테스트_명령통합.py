"""실제 CLI/설치 SDK/loopback HTTP 통합 시험. 모델 판단 품질 시험은 아니다.

원본의 지정 Python 소스만 TMPDIR로 복사한다. 실제 .env/store/cache는 읽지 않는다.
Atom, OpenAI 호환 응답, Discord 형태 POST/GET은 모두 명시적 합성 HTTP 서버다.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from xml.sax.saxutils import escape
import unittest


SOURCES = ("run.py", "graph.py", "config.py", "llm.py", "호출예산.py",
           "발행승인.py", "publisher.py", "collectors.py", "net.py", "metrics.py")


class SyntheticService:
    """프롬프트를 실제로 읽어 스키마 응답을 만드는 전송 전용 대역."""
    def __init__(self):
        self.events = []
        self.errors = []
        self.lock = threading.Lock()
        self.payload = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, data, content_type="application/json", status=200):
                raw = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                with owner.lock:
                    owner.events.append(("GET", self.path, None))
                path = urlsplit(self.path).path
                if path == "/feed":
                    when = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
                    entries = []
                    for topic in ("도구", "학습", "검증"):
                        body = (f"{topic} 공개 자료입니다. 사용자는 원문을 읽을 수 있습니다. 안전한 실습 절차를 설명합니다. " * 30)
                        entries.append(f'<entry><id>{topic}</id><title>{topic} 소식</title>'
                                       f'<link href="{owner.base}/articles/{topic}"/>'
                                       f'<updated>{when}</updated><summary>{topic} 자료 공개</summary>'
                                       f'<content type="text">{escape(body)}</content></entry>')
                    self.reply(('<feed xmlns="http://www.w3.org/2005/Atom"><title>합성 피드</title>'
                                + ''.join(entries) + '</feed>').encode("utf-8"), "application/atom+xml; charset=utf-8")
                elif path == "/incoming/messages/123456" and owner.payload is not None:
                    self.reply({"id": "123456", "embeds": owner.payload["embeds"],
                                "author": {"username": owner.payload["username"]}})
                else:
                    owner.errors.append("unexpected GET " + path)
                    self.reply({"error": "unexpected route"}, status=404)

            def do_POST(self):
                try:
                    data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    path = urlsplit(self.path).path
                    with owner.lock:
                        owner.events.append(("POST", self.path, data))
                    if path == "/incoming":
                        owner.payload = data
                        self.reply({"id": "123456"})
                        return
                    if path != "/v1/chat/completions":
                        raise ValueError("unexpected POST route")
                    system, user = [message["content"] for message in data["messages"]]
                    if "뉴스레터 편집자" in system:
                        ask = int(re.search(r"순서대로 (\d+)건", system).group(1))
                        listing = re.findall(r"^(\d+)\. .*?\] (.*?) —", user, re.M)
                        assert listing and "<candidates>" in user
                        result = {"picks": [{"index": int(index), "score": 9,
                                             "reason": "합성 독자 기준에 맞는 공개 자료", "event": title}
                                            for index, title in listing[:ask]]}
                    elif "뉴스레터 기자" in system:
                        assert "<article>" in user and len(user.split("<article>")[1].split("</article>")[0]) >= 600
                        topic = re.search(r"제목: (도구|학습|검증) 소식", user).group(1)
                        result = {"headline": f"{topic} 공개 자료 안내",
                                  "summary": f"{topic} 공개 자료입니다. 사용자는 원문을 읽을 수 있습니다. 안전한 실습 절차를 설명합니다.",
                                  "why": "실습 절차를 확인하는 데 쓸 수 있습니다."}
                    elif "뉴스레터 사실 검수자" in system:
                        assert "[원문]" in user and "[초안]" in user
                        result = {"ok": True, "problems": []}
                    else:
                        raise ValueError("unknown production prompt")
                    self.reply({"id": "chatcmpl-synthetic", "object": "chat.completion", "created": 1,
                                "model": data["model"], "choices": [{"index": 0, "finish_reason": "stop",
                                "message": {"role": "assistant", "content": json.dumps(result, ensure_ascii=False)}}],
                                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}})
                except Exception as exc:
                    owner.errors.append(type(exc).__name__ + ": " + str(exc))
                    self.reply({"error": {"message": "synthetic fixture failure", "type": "test_error"}}, status=500)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def counts(self):
        with self.lock:
            return {"feed": sum(m == "GET" and p == "/feed" for m, p, _ in self.events),
                    "llm": sum(m == "POST" and p == "/v1/chat/completions" for m, p, _ in self.events),
                    "post": sum(m == "POST" and urlsplit(p).path == "/incoming" for m, p, _ in self.events),
                    "readback": sum(m == "GET" and p.startswith("/incoming/messages/") for m, p, _ in self.events)}


class CommandHTTPIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="newsletter-cli-", dir=os.environ["TMPDIR"])
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        original = Path(__file__).resolve().parents[1]
        for name in SOURCES:
            shutil.copyfile(original / name, self.root / name)
        self.service = SyntheticService()
        self.service.start()
        self.addCleanup(self.service.close)
        # Allowlist only process prerequisites: no inherited API keys/proxy/PYTHONPATH.
        self.env = {key: value for key, value in os.environ.items()
                    if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "TMPDIR", "TEMP", "TMP"}}
        self.env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "NO_PROXY": "127.0.0.1,localhost",
                         "HOME": str(self.root), "USERPROFILE": str(self.root), "PYTHONNOUSERSITE": "1"})
        (self.root / ".env").write_text(
            f"SYNTHETIC_API_KEY=dummy-local-test-only\nSYNTHETIC_WEBHOOK={self.service.base}/incoming\n", encoding="utf-8")
        cfg = {"run": {"store_dir": "store"},
               "llm": {"base_url": self.service.base + "/v1", "api_key_env": "SYNTHETIC_API_KEY",
                       "models": {stage: "synthetic-transport-only" for stage in ("select", "report", "verify")},
                       "temperature": 0, "timeout_s": 5, "max_retries": 0, "request_limit": 65},
               "collect": {"hours": 48, "per_source_cap": 12, "min_body_chars": 600, "body_max_chars": 6000,
                           "request_timeout_s": 5, "user_agent": "synthetic-local-integration", "excluded_hosts": [],
                           "sources": [{"id": "synthetic", "name": "합성 로컬 소스", "route": "atom", "tier": 1,
                                        "lang": "ko", "url": self.service.base + "/feed"}]},
               "select": {"batch_size": 20, "prelim_picks": 5, "prelim_borderline": 2, "final_target": 3,
                          "final_min": 3, "final_ask_extra": 3, "max_per_source": 3, "summary_chars_in_listing": 200},
               "verify": {"max_regenerations": 1, "korean_min_ratio": .3, "headline_max_chars": 40,
                          "summary_max_chars": 700, "why_max_chars": 220},
               "publish": {"webhook_env": "SYNTHETIC_WEBHOOK", "title": "합성 통합 시험", "total_chars_max": 5800}}
        audience = {"status": "synthetic", "persona": "안전한 실습을 배우는 독자", "importance": "공개된 실습 자료",
                    "exclude": [], "tone": "간결한 한국어", "verification_criteria": ["원문에 있는 사실만 확인"]}
        # JSON is valid YAML; no production settings or audience data are copied.
        for name, data in (("config.yaml", cfg), ("audience.yaml", audience)):
            (self.root / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        probe = self.python("-c", "import pathlib,run,graph,llm,config; "
                            "assert all(pathlib.Path(m.__file__).resolve().parent == pathlib.Path.cwd().resolve() "
                            "for m in (run,graph,llm,config))")
        self.assertEqual(probe.returncode, 0, probe.stdout + probe.stderr)

    def python(self, *args):
        return subprocess.run([sys.executable, *args], cwd=self.root, env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
                              encoding="utf-8", errors="strict")

    def cli(self, *args):
        result = self.python("run.py", *args)
        self.assertEqual(self.service.errors, [], "합성 서버 fixture 실패")
        return result

    def prepared(self):
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("dry_run", result.stdout)
        paths = list((self.root / "store/cache/publications").glob("*.json"))
        self.assertEqual(len(paths), 1)
        manifest = json.loads(paths[0].read_text(encoding="utf-8"))
        self.assertEqual(manifest["metadata"]["counts"],
                         {"collected": 3, "shortlisted": 3, "picked": 3, "verified": 3})
        self.assertEqual(self.service.counts(), {"feed": 1, "llm": 8, "post": 0, "readback": 0})
        self.assertEqual(len(manifest["payload"]["embeds"]), 4)
        raw = json.dumps(manifest["payload"], sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.assertEqual(manifest["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertIn(manifest["sha256"], result.stdout)
        return paths[0], manifest

    def test_frozen_preview_confirmation_exact_send_and_duplicate(self):
        path, manifest = self.prepared()
        before = path.read_bytes()
        run_id, digest = manifest["run_id"], manifest["sha256"]
        show = self.cli("--show-prepared", run_id)
        self.assertEqual(show.returncode, 0, show.stderr)
        self.assertIn(digest, show.stdout)
        for embed in manifest["payload"]["embeds"]:
            for key in ("title", "description", "url"):
                if key in embed:
                    self.assertIn(embed[key], show.stdout)
        missing = self.cli("--send-prepared", run_id)
        self.assertEqual(missing.returncode, 2)
        self.assertIn("--confirm-hash", missing.stderr)
        wrong = self.cli("--send-prepared", run_id, "--confirm-hash", "0" * 64)
        self.assertEqual(wrong.returncode, 1)
        self.assertIn("confirmation_mismatch", wrong.stderr)
        legacy = self.cli("--publish")
        self.assertEqual(legacy.returncode, 2)
        self.assertIn("즉시 발송은 중지", legacy.stderr)
        self.assertEqual(self.service.counts(), {"feed": 1, "llm": 8, "post": 0, "readback": 0})
        sent = self.cli("--send-prepared", run_id, "--confirm-hash", digest)
        self.assertEqual(sent.returncode, 0, sent.stdout + sent.stderr)
        outcome = json.loads(sent.stdout)
        self.assertEqual(outcome["status"], "sent")
        self.assertEqual(outcome["readback"], {"status": "verified"})
        self.assertEqual(self.service.payload, manifest["payload"])
        self.assertEqual(self.service.payload["allowed_mentions"], {"parse": []})
        self.assertEqual(self.service.counts(), {"feed": 1, "llm": 8, "post": 1, "readback": 1})
        self.assertIn(("POST", "/incoming?wait=true"), [(m, p) for m, p, _ in self.service.events])
        receipt = json.loads(path.with_name(run_id + ".receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["sha256"], digest)
        self.assertEqual(receipt["result"], outcome)
        again = self.cli("--send-prepared", run_id, "--confirm-hash", digest)
        self.assertEqual(again.returncode, 1)
        self.assertIn("already_claimed", again.stderr)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.service.counts(), {"feed": 1, "llm": 8, "post": 1, "readback": 1})
        print("  합성 HTTP: 수집=1, LLM=8, 승인 POST=1, GET 재조회=1; 동일 payload·중복 차단 확인")

    def test_tampered_candidate_cannot_be_shown_or_sent(self):
        path, manifest = self.prepared()
        digest = manifest["sha256"]
        manifest["payload"]["embeds"][1]["description"] = "승인하지 않은 변조 내용"
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        for args in (("--show-prepared", manifest["run_id"]),
                     ("--send-prepared", manifest["run_id"], "--confirm-hash", digest)):
            with self.subTest(args=args):
                result = self.cli(*args)
                self.assertEqual(result.returncode, 1)
                self.assertIn("hash_mismatch", result.stderr)
        self.assertFalse(path.with_name(manifest["run_id"] + ".claim.json").exists())
        self.assertEqual(self.service.counts(), {"feed": 1, "llm": 8, "post": 0, "readback": 0})

    def budget_used(self):
        with closing(sqlite3.connect(self.root / "store/호출예산.sqlite3")) as conn:
            return conn.execute("SELECT cap, used FROM budget WHERE id=1").fetchone()

    def test_persistent_budget_stops_before_next_http_across_processes(self):
        # ONLY the isolated copied module and isolated ledger receive these reservations.
        seed = self.python("-c", "from 호출예산 import RequestBudget; "
                           "b=RequestBudget('store/호출예산.sqlite3', limit=65); "
                           "[b.reserve() for _ in range(64)]; print(b.snapshot()['used'])")
        self.assertEqual(seed.returncode, 0, seed.stderr)
        self.assertEqual(seed.stdout.strip(), "64")
        first = self.cli()
        self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
        self.assertIn("BudgetExceeded", first.stderr)
        self.assertIn("실제 요청 예산이 소진", first.stderr)
        self.assertEqual(self.service.counts(), {"feed": 1, "llm": 1, "post": 0, "readback": 0})
        self.assertEqual(self.budget_used(), (65, 65))
        second = self.cli()
        self.assertEqual(second.returncode, 1)
        self.assertIn("실제 요청 예산이 소진", second.stderr)
        self.assertEqual(self.service.counts(), {"feed": 2, "llm": 1, "post": 0, "readback": 0})
        self.assertEqual(self.budget_used(), (65, 65))
        self.assertFalse((self.root / "store/cache/publications").exists())
        print("  격리 예산: 64 예약 → 합성 HTTP 1회 → 65/65; 재실행 추가 LLM HTTP=0")


if __name__ == "__main__":
    unittest.main()
