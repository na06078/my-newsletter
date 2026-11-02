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
           "발행승인.py", "불변저장.py", "publisher.py", "collectors.py", "net.py", "metrics.py")


class SyntheticService:
    """프롬프트를 실제로 읽어 스키마 응답을 만드는 전송 전용 대역."""
    def __init__(self):
        self.events = []
        self.errors = []
        self.lock = threading.Lock()
        self.payload = None
        self.report_content = None
        self.judge_problems = []
        self.webhook_status = 200
        self.omit_author = False
        self.inline_body = True
        self.robots_status = 404
        self.blocked_hits = 0
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
                                       f'<content type="text">{escape(body) if owner.inline_body else ""}</content></entry>')
                    self.reply(('<feed xmlns="http://www.w3.org/2005/Atom"><title>합성 피드</title>'
                                + ''.join(entries) + '</feed>').encode("utf-8"), "application/atom+xml; charset=utf-8")
                elif path == "/robots.txt":
                    self.reply(b"", "text/plain", status=owner.robots_status)
                elif path.startswith("/articles/"):
                    self.send_response(302)
                    self.send_header("Location", f"http://localhost:{owner.server.server_port}/blocked")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif path == "/blocked":
                    owner.blocked_hits += 1
                    self.reply(b"should not be fetched", "text/html")
                elif path == "/incoming/messages/123456" and owner.payload is not None:
                    message = {"id": "123456", "embeds": owner.payload["embeds"]}
                    if not owner.omit_author:
                        message["author"] = {"username": owner.payload["username"]}
                    self.reply(message)
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
                        self.reply({"retry_after": 0} if owner.webhook_status == 429 else {"id": "123456"},
                                   status=owner.webhook_status)
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
                        result = {"ok": True, "problems": owner.judge_problems}
                    else:
                        raise ValueError("unknown production prompt")
                    content = (owner.report_content if "뉴스레터 기자" in system and owner.report_content is not None
                               else json.dumps(result, ensure_ascii=False))
                    self.reply({"id": "chatcmpl-synthetic", "object": "chat.completion", "created": 1,
                                "model": data["model"], "choices": [{"index": 0, "finish_reason": "stop",
                                "message": {"role": "assistant", "content": content}}],
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
        # TMPDIR 가 없으면(대부분의 Windows 셸) tempfile 이 시스템 기본 임시 폴더를 쓴다.
        self.temp = tempfile.TemporaryDirectory(prefix="newsletter-cli-", dir=os.environ.get("TMPDIR"))
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
        """--dry-run 으로 보내지 않고 준비본만 만든다."""
        result = self.cli("--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("발행 상태: prepared", result.stdout)
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

    def test_default_run_publishes_end_to_end_without_any_human_step(self):
        result = self.cli()                                        # 플래그 없음: 수집부터 발송·재조회까지
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("발행 상태: sent", result.stdout)
        self.assertEqual(self.service.counts(), {"feed": 1, "llm": 8, "post": 1, "readback": 1})
        paths = [p for p in (self.root / "store/cache/publications").glob("*.json")
                 if not p.name.endswith((".claim.json", ".receipt.json"))]
        self.assertEqual(len(paths), 1)
        manifest = json.loads(paths[0].read_text(encoding="utf-8"))
        run_id, digest = manifest["run_id"], manifest["sha256"]
        self.assertIn(digest, result.stdout)
        self.assertEqual(self.service.payload, manifest["payload"])          # 보낸 것 = 저장한 것
        self.assertEqual(self.service.payload["allowed_mentions"], {"parse": []})
        self.assertIn(("POST", "/incoming?wait=true"), [(m, p) for m, p, _ in self.service.events])
        receipt = json.loads(paths[0].with_name(run_id + ".receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["sha256"], digest)
        self.assertEqual(receipt["result"]["status"], "sent")
        self.assertEqual(receipt["result"]["readback"], {"status": "verified"})
        self.assertIn("Discord 메시지: 123456", result.stdout)
        print("  합성 HTTP: 수집=1, LLM=8, POST=1, GET 재조회=1; 사람 개입 없이 끝까지 발송·재조회 확인")

    def test_dry_run_saves_prepared_content_and_sends_nothing(self):
        path, manifest = self.prepared()
        show = self.cli("--show-prepared", manifest["run_id"])
        self.assertEqual(show.returncode, 0, show.stderr)
        self.assertIn(manifest["sha256"], show.stdout)
        for embed in manifest["payload"]["embeds"]:
            for key in ("title", "description", "url"):
                if key in embed:
                    self.assertIn(embed[key], show.stdout)
        self.assertFalse(path.with_name(manifest["run_id"] + ".claim.json").exists())
        self.assertEqual(self.service.counts(), {"feed": 1, "llm": 8, "post": 0, "readback": 0})

    def test_invalid_model_schema_skips_articles_and_finishes_the_cli(self):
        self.service.report_content = '{"headline": 9, "summary": "SENSITIVE-INPUT-SENTINEL", "why": null}'
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.service.counts(), {"feed": 1, "llm": 5, "post": 1, "readback": 1})
        self.assertEqual(len(self.service.payload["embeds"]), 1)
        self.assertIn("검수를 통과하지 못해", self.service.payload["embeds"][0]["description"])
        self.assertNotIn("SENSITIVE-INPUT-SENTINEL", result.stdout + result.stderr)
        records = (self.root / "store/metrics.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("SENSITIVE-INPUT-SENTINEL", records)

    def test_positive_judge_with_problems_cannot_publish_articles(self):
        self.service.judge_problems = [{"claim": "잘못된 주장", "kind": "unsupported", "note": "본문에 없음"}]
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.service.counts(), {"feed": 1, "llm": 14, "post": 1, "readback": 1})
        self.assertEqual(len(self.service.payload["embeds"]), 1)
        self.assertIn("검수 통과 0", self.service.payload["embeds"][0]["description"])
        self.assertIn("검수를 통과하지 못해", result.stdout)

    def test_rate_limit_is_one_post_and_exit_code_2(self):
        self.service.webhook_status = 429
        result = self.cli()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.service.counts(), {"feed": 1, "llm": 8, "post": 1, "readback": 0})
        receipts = list((self.root / "store/cache/publications").glob("*.receipt.json"))
        self.assertEqual(len(receipts), 1)
        receipt = json.loads(receipts[0].read_text(encoding="utf-8"))["result"]
        self.assertEqual(receipt["send_outcome"], "rejected")
        self.assertEqual(receipt["http_status"], 429)

    def test_readback_without_author_is_not_success(self):
        self.service.omit_author = True
        result = self.cli()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.service.counts(), {"feed": 1, "llm": 8, "post": 1, "readback": 1})
        self.assertIn("재조회: mismatch", result.stdout)

    def test_excluded_redirect_body_is_never_requested_by_cli(self):
        self.service.inline_body = False
        cfg_path = self.root / "config.yaml"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["collect"]["excluded_hosts"] = ["localhost"]
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.service.blocked_hits, 0)
        self.assertTrue(any(p.startswith("/articles/") for method, p, _ in self.service.events if method == "GET"))
        self.assertEqual(self.service.counts(), {"feed": 1, "llm": 2, "post": 1, "readback": 1})
        self.assertEqual(len(self.service.payload["embeds"]), 1)

    def test_unreachable_robots_prevents_article_fetch_by_cli(self):
        self.service.inline_body = False
        self.service.robots_status = 503
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(any(p == "/robots.txt" for method, p, _ in self.service.events if method == "GET"))
        self.assertFalse(any(p.startswith("/articles/") for method, p, _ in self.service.events if method == "GET"))
        self.assertEqual(self.service.blocked_hits, 0)
        self.assertEqual(self.service.counts(), {"feed": 1, "llm": 2, "post": 1, "readback": 1})

    def test_approval_commands_are_gone(self):
        for args in (("--publish",), ("--send-prepared", "x"), ("--confirm-hash", "0" * 64)):
            with self.subTest(args=args):
                result = self.cli(*args)
                self.assertEqual(result.returncode, 2)
                self.assertIn("unrecognized arguments", result.stderr)
        self.assertEqual(self.service.counts(), {"feed": 0, "llm": 0, "post": 0, "readback": 0})

    def test_cache_write_error_after_delivery_does_not_hide_message_or_resend(self):
        result = self.python("-c", "import graph,run; "
            "graph._write_local_cache=lambda *a,**k: (_ for _ in ()).throw(OSError('synthetic-cache-failure')); "
            "raise SystemExit(run.main())")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("cache_write_failed", result.stdout)
        self.assertIn("123456", result.stdout)
        self.assertEqual(self.service.counts(), {"feed": 1, "llm": 8, "post": 1, "readback": 1})
        receipt_path = next((self.root / "store/cache/publications").glob("*.receipt.json"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["result"]["status"], "sent")

    def test_missing_webhook_stops_before_feed_and_model_calls(self):
        (self.root / ".env").write_text("SYNTHETIC_API_KEY=dummy-local-test-only\n", encoding="utf-8")
        result = self.cli()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("webhook_not_configured", result.stderr)
        self.assertEqual(self.service.counts(), {"feed": 0, "llm": 0, "post": 0, "readback": 0})
        self.assertFalse((self.root / "store/cache/publications").exists())

    def test_tampered_candidate_cannot_be_shown(self):
        path, manifest = self.prepared()
        manifest["payload"]["embeds"][1]["description"] = "승인하지 않은 변조 내용"
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        result = self.cli("--show-prepared", manifest["run_id"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("hash_mismatch", result.stderr)
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
