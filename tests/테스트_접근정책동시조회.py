"""동시 접근 정책: 실제 loopback HTTP와 사건 기반 순서 제어만 사용한다."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import unittest
from unittest.mock import patch

from net import Http, Robots, fetch_body

ALLOW = b"User-agent: *\nAllow: /\n"
ARTICLE = b"<html><body><article><p>" + b"Useful local model release improves inference. " * 50 + b"</p></article></body></html>"


@contextmanager
def controlled_server(respond):
    seen, guard = [], threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            with guard:
                seen.append(self.path)
                number = seen.count(self.path)
            status, headers, body = respond(self.path, number)
            try:
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass  # 개인 기한으로 취소된 실제 연결도 허용한다.

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class SingleFlightTests(unittest.TestCase):
    def test_same_origin_one_robots_and_each_allowed_body_once(self):
        entered, release = threading.Event(), threading.Event()
        def respond(path, number):
            if path == "/robots.txt":
                entered.set()
                release.wait(2)
                return 200, {}, ALLOW
            return 200, {"Content-Type": "text/html"}, ARTICLE
        with controlled_server(respond) as (base, seen), Http("test", gap_s=0, deadline_s=3) as http:
            robots = Robots(http)
            with ThreadPoolExecutor(max_workers=4) as pool:
                try:
                    futures = [pool.submit(fetch_body, http, robots, base + f"/body{i}", 6000) for i in range(4)]
                    self.assertTrue(entered.wait(1))
                    time.sleep(.15)  # 소유자의 응답은 아직 차단한 상태로 경쟁자를 실행한다.
                finally:
                    release.set()
                outputs = [f.result(timeout=3) for f in futures]
            self.assertTrue(all(o["robots"] == "allowed" and o["error"] is None and o["text"] for o in outputs))
            self.assertEqual(Counter(seen), Counter({"/robots.txt": 1, **{f"/body{i}": 1 for i in range(4)}}))
            self.assertEqual(http.count, 5)
            print("SINGLE_FLIGHT same_origin", dict(Counter(seen)), "requests", http.count)

    def test_waiter_deadline_returns_before_owner_without_target_request(self):
        for entry in ("get", "fetch_body", "verdict"):
            with self.subTest(entry=entry):
                entered, release = threading.Event(), threading.Event()
                def respond(path, number):
                    if path == "/robots.txt":
                        entered.set()
                        release.wait(2)
                        return 200, {}, ALLOW
                    return 200, {}, ARTICLE
                with controlled_server(respond) as (base, seen), Http("test", gap_s=0, deadline_s=3) as http:
                    robots = Robots(http)
                    def waiter():
                        with http._budget():
                            http._deadline.set(time.monotonic() + .08)
                            if entry == "get":
                                out = http.get(base + "/waiter", robots=robots)
                            elif entry == "fetch_body":
                                out = fetch_body(http, robots, base + "/waiter", 6000)
                            else:
                                out = robots.verdict(base + "/waiter")
                            return out, http._deadline_expired.get()
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        owner = pool.submit(http.get, base + "/owner", robots=robots)
                        try:
                            self.assertTrue(entered.wait(1))
                            waiting = pool.submit(waiter)
                            out, expired = waiting.result(timeout=.6)
                            self.assertTrue(expired)
                            self.assertFalse(owner.done())
                            if entry == "verdict":
                                self.assertEqual(out, "unreachable")
                            else:
                                self.assertEqual(out["error"], "total_deadline_exceeded")
                            self.assertEqual(seen, ["/robots.txt"])
                        finally:
                            release.set()
                        self.assertIsNone(owner.result(timeout=2)["error"])
                    self.assertEqual(Counter(seen), Counter({"/robots.txt": 1, "/owner": 1}))
                    print("WAITER_DEADLINE", entry, dict(Counter(seen)))

    def test_owner_personal_deadline_does_not_poison_longer_waiter(self):
        entered, release = threading.Event(), threading.Event()
        def respond(path, number):
            if path == "/robots.txt":
                if number == 1:
                    entered.set()
                    release.wait(2)
                return 200, {}, ALLOW
            return 200, {}, ARTICLE
        with controlled_server(respond) as (base, seen), Http("test", gap_s=0, deadline_s=3) as http:
            robots = Robots(http)
            def short_owner():
                with http._budget():
                    http._deadline.set(time.monotonic() + .2)
                    return http.get(base + "/owner", robots=robots)
            with ThreadPoolExecutor(max_workers=2) as pool:
                try:
                    owner = pool.submit(short_owner)
                    self.assertTrue(entered.wait(1))
                    waiter = pool.submit(http.get, base + "/waiter", robots=robots)
                    self.assertEqual(owner.result(timeout=1)["error"], "total_deadline_exceeded")
                    self.assertIsNone(waiter.result(timeout=1)["error"])
                finally:
                    release.set()
            self.assertEqual(Counter(seen), Counter({"/robots.txt": 2, "/waiter": 1}))
            self.assertEqual(robots.verdict(base + "/later"), "allowed")
            self.assertEqual(http.count, 3)
            print("OWNER_DEADLINE_RETRY", dict(Counter(seen)))

    def test_different_origins_robots_fetches_overlap(self):
        rendezvous = threading.Barrier(2)
        overlapping = []
        def respond(path, number):
            if path == "/robots.txt":
                try:
                    rendezvous.wait(timeout=1)
                    overlapping.append(True)
                except threading.BrokenBarrierError:
                    return 503, {}, b"did not overlap"
                return 200, {}, ALLOW
            return 200, {}, ARTICLE
        with controlled_server(respond) as (a, seen_a), controlled_server(respond) as (b, seen_b), Http("test", gap_s=0, deadline_s=3) as http:
            robots = Robots(http)
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(http.get, base + "/body", robots=robots) for base in (a, b)]
                self.assertTrue(all(f.result(timeout=2)["error"] is None for f in futures))
            self.assertEqual(overlapping, [True, True])
            self.assertEqual(seen_a, ["/robots.txt", "/body"])
            self.assertEqual(seen_b, ["/robots.txt", "/body"])
            print("ORIGIN_OVERLAP robots_at_barrier", len(overlapping), "requests", http.count)

    def test_strict_policy_independent_of_inflight_and_cached_loose_policy(self):
        entered, release = threading.Event(), threading.Event()
        def policy(path, number):
            entered.set()
            release.wait(2)
            return 200, {}, ALLOW
        with controlled_server(policy) as (destination, dest_seen):
            location = destination.replace("127.0.0.1", "localhost") + "/policy"
            def respond(path, number):
                if path == "/robots.txt":
                    return 302, {"Location": location}, b""
                return 200, {}, ARTICLE
            with controlled_server(respond) as (base, seen), Http("test", gap_s=0, deadline_s=3) as http:
                robots = Robots(http)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    loose = pool.submit(http.get, base + "/loose", robots=robots)
                    try:
                        self.assertTrue(entered.wait(1))
                        strict = pool.submit(http.get, base + "/strict", robots=robots, excluded_hosts=["localhost"])
                        self.assertEqual(strict.result(timeout=.7)["error"], "robots_unreachable")
                        self.assertFalse(loose.done())
                        self.assertEqual(dest_seen, ["/policy"])
                    finally:
                        release.set()
                    self.assertIsNone(loose.result(timeout=2)["error"])
                again = http.get(base + "/strict_cached", robots=robots, excluded_hosts=["localhost"])
                self.assertEqual(again["error"], "robots_unreachable")
                self.assertEqual(Counter(seen), Counter({"/robots.txt": 2, "/loose": 1}))
                self.assertEqual(dest_seen, ["/policy"])
                print("POLICY_INDEPENDENT", dict(Counter(seen)), "redirect_target", dest_seen)

    def test_server_error_singleflight_is_cached_fail_closed_without_deadlock(self):
        entered, release = threading.Event(), threading.Event()
        def respond(path, number):
            entered.set()
            release.wait(2)
            return 503, {}, b"unavailable"
        with controlled_server(respond) as (base, seen), Http("test", gap_s=0, deadline_s=3) as http:
            robots = Robots(http)
            with ThreadPoolExecutor(max_workers=3) as pool:
                try:
                    futures = [pool.submit(http.get, base + f"/body{i}", robots=robots) for i in range(3)]
                    self.assertTrue(entered.wait(1))
                    time.sleep(.1)
                finally:
                    release.set()
                self.assertEqual([f.result(timeout=2)["error"] for f in futures], ["robots_unreachable"] * 3)
            self.assertEqual(robots.verdict(base + "/later"), "unreachable")
            self.assertEqual(seen, ["/robots.txt"])
            self.assertEqual(http.count, 1)

    def test_parser_exception_releases_key_for_next_caller(self):
        from urllib.robotparser import RobotFileParser
        entered, release = threading.Event(), threading.Event()
        def respond(path, number):
            if path == "/robots.txt" and number == 1:
                entered.set()
                release.wait(2)
            return 200, {}, ALLOW if path == "/robots.txt" else ARTICLE
        original = RobotFileParser.parse
        attempts = []
        def fail_once(parser, lines):
            attempts.append(1)
            if len(attempts) == 1:
                raise ValueError("synthetic parser failure")
            return original(parser, lines)
        with controlled_server(respond) as (base, seen), Http("test", gap_s=0, deadline_s=3) as http:
            robots = Robots(http)
            with patch.object(RobotFileParser, "parse", fail_once), ThreadPoolExecutor(max_workers=2) as pool:
                owner = pool.submit(robots.verdict, base + "/owner")
                try:
                    self.assertTrue(entered.wait(1))
                    waiter = pool.submit(http.get, base + "/waiter", robots=robots)
                finally:
                    release.set()
                with self.assertRaisesRegex(ValueError, "synthetic parser failure"):
                    owner.result(timeout=2)
                self.assertIsNone(waiter.result(timeout=2)["error"])
            self.assertEqual(Counter(seen), Counter({"/robots.txt": 2, "/waiter": 1}))

    def test_legacy_http_and_verdict_adapter_interfaces_remain_supported(self):
        class FakeHttp:
            user_agent = "test"
            count = 0
            def get(self, url, **kwargs):
                self.count += 1
                return {"error": None, "status": 404, "content": b""}
        fake = FakeHttp()
        robots = Robots(fake)
        self.assertEqual(robots.verdict("http://127.0.0.1/body"), "no_robots")
        self.assertEqual(robots.verdict("http://127.0.0.1/other"), "no_robots")
        self.assertEqual(fake.count, 1)
        class LegacyRobots:
            def verdict(self, url):
                return "disallowed"
        with controlled_server(lambda p, n: (200, {}, ARTICLE)) as (base, seen), Http("test", gap_s=0) as http:
            out = fetch_body(http, LegacyRobots(), base + "/body", 6000)
            self.assertEqual(out["robots"], "disallowed")
            self.assertEqual(seen, [])
