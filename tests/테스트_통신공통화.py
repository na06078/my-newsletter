"""운영·조사 공통 통신과 취소 경계: 실제 loopback만 사용한다."""
import gzip
import zlib
import threading
import time
import unittest
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from net import Http, Robots, fetch_body
from tests.테스트_수집경계 import server
from tools import survey_sources as survey
from tests.support import NOW, atom
from unittest.mock import patch
import net


@contextmanager
def slow_server(mode):
    seen, closed = [], threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            # 읽기 방향 EOF는 서버가 더 쓰기 전에도 클라이언트 취소/소켓 종료를 증명한다.
            def watch_close():
                try:
                    self.connection.settimeout(1.5)
                    if self.connection.recv(1) == b"":
                        closed.set()
                except (ConnectionResetError, ConnectionAbortedError):
                    closed.set()
                except (TimeoutError, OSError):
                    pass
            watcher = threading.Thread(target=watch_close, daemon=True)
            watcher.start()
            try:
                if mode == "headers":
                    time.sleep(.55)
                    self.wfile.write(b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nok")
                elif mode == "drip_headers":
                    for byte in b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nok":
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(.04)
                elif mode == "drip_body":
                    self.wfile.write(b"HTTP/1.0 200 OK\r\nContent-Length: 30\r\n\r\n")
                    for _ in range(30):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        time.sleep(.04)
                else:
                    self.send_response(200)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"ok")
            except (OSError, TimeoutError):
                pass
            watcher.join(1.6)

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}", seen, closed
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join()


class DeadlineTests(unittest.TestCase):
    def test_transport_invalid_url_is_a_safe_result_not_a_leaked_exception(self):
        with Http("test", gap_s=0) as http:
            try:
                out = http.get("http://\ud800.invalid/feed")
            except Exception as exc:
                self.fail("GET boundary leaked " + type(exc).__name__)
            self.assertEqual(out["error"], "unsupported_url")
            self.assertEqual(out["content"], b"")

    def test_concurrent_callers_get_independent_deadlines_and_shared_counter(self):
        routes = {"/slow": (.4, 200, {}, b"slow"), "/good": (.08, 200, {}, b"ok")}
        with timed_server(routes) as (base, seen):
            with Http("test", timeout_s=1, gap_s=0, deadline_s=.15) as http:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(http.get, base + "/slow")
                    time.sleep(.1)
                    second = pool.submit(http.get, base + "/good")
                    self.assertEqual(first.result()["error"], "total_deadline_exceeded")
                    allowed = second.result()
                    self.assertIsNone(allowed["error"])
                    self.assertEqual(allowed["content"], b"ok")
                    self.assertEqual(http.count, 2)
                    self.assertEqual(seen, ["/slow", "/good"])

    def test_redirects_and_rate_gap_do_not_reset_deadline(self):
        with server({"/start": (302, {"Location": "/late"}, b""),
                     "/late": (200, {}, b"must not be requested")}) as (base, seen):
            with Http("test", timeout_s=1, gap_s=.2, deadline_s=.08) as http:
                out = http.get(base + "/start")
                self.assertEqual(out["error"], "total_deadline_exceeded")
                time.sleep(.15)
                self.assertEqual(seen, ["/start"])
                self.assertEqual(http.count, 1)

    def test_client_reuse_after_timeout_and_idempotent_close(self):
        http = Http("test", timeout_s=1, gap_s=0, deadline_s=.18)
        try:
            with slow_server("headers") as (base, seen, closed):
                out = http.get(base + "/slow")
                self.assertEqual(out["error"], "total_deadline_exceeded")
                self.assertTrue(closed.wait(.12))
            client, thread = http._client, http._thread
            with server({"/good": (200, {}, b"ok")}) as (base, seen):
                out = http.get(base + "/good")
                self.assertIsNone(out["error"])
                self.assertEqual(out["content"], b"ok")
                self.assertEqual(http.count, 2)
                self.assertIs(http._client, client)
                self.assertIs(http._thread, thread)
        finally:
            http.close()
            http.close()
        self.assertTrue(http._client.is_closed)
        self.assertFalse(http._thread.is_alive())
        self.assertTrue(http._loop.is_closed())
        with Http("unused") as unused:
            self.assertIsNone(unused._thread)
        unused.close()
        self.assertTrue(unused._client.is_closed)

    def test_explicit_nested_timeout_signal_survives_clock_rounding(self):
        async def timeout_before_clock_advances(*args):
            raise TimeoutError
        with Http("test", timeout_s=1, gap_s=0, deadline_s=5) as http:
            with patch.object(http, "_request", side_effect=timeout_before_clock_advances):
                out = http.get("http://127.0.0.1:1/body", robots=Robots(http))
                self.assertEqual(out["error"], "total_deadline_exceeded")

    def test_redirect_destination_robots_wait_uses_remaining_deadline(self):
        with slow_server("headers") as (dest, dest_seen, closed):
            routes = {"/robots.txt": (.04, 404, {}, b""),
                      "/start": (.08, 302, {"Location": dest + "/late"}, b"")}
            with timed_server(routes) as (base, seen):
                with Http("test", timeout_s=1, gap_s=0, deadline_s=.24) as http:
                    started = time.monotonic()
                    out = http.get(base + "/start", robots=Robots(http))
                    elapsed = time.monotonic() - started
                    self.assertEqual(out["error"], "total_deadline_exceeded")
                    self.assertLess(elapsed, .38)
                    self.assertTrue(closed.wait(.12))
                    time.sleep(.65)  # 버려진 blocking worker라면 늦은 robots 응답 뒤 목표를 요청할 시간까지 관찰한다.
                    self.assertEqual(dest_seen, ["/robots.txt"])
                    self.assertEqual(seen, ["/robots.txt", "/start"])
                    self.assertEqual(http.count, 3)
                    print("DEADLINE nested_robots", round(elapsed, 3), "peer_closed", closed.is_set(), "requests", http.count)

    def test_drip_headers_and_body_cannot_extend_idle_timeout(self):
        for mode in ("drip_headers", "drip_body"):
            with self.subTest(mode=mode), slow_server(mode) as (base, seen, closed):
                with Http("test", timeout_s=.3, gap_s=0, deadline_s=.18) as http:
                    started = time.monotonic()
                    out = http.get(base + "/drip")
                    self.assertEqual(out["error"], "total_deadline_exceeded")
                    self.assertLess(time.monotonic() - started, .45)
                    self.assertTrue(closed.wait(.12))
                    self.assertEqual(seen, ["/drip"])
                    print("DEADLINE", mode, round(time.monotonic() - started, 3), "peer_closed", closed.is_set())

    def test_fetch_body_initial_robots_and_redirect_share_deadline(self):
        with server({"/late": (200, {}, b"must not be requested")}) as (dest, dest_seen):
            routes = {"/robots.txt": (.14, 200, {}, b"User-agent: *\nAllow: /\n"),
                      "/start": (.14, 302, {"Location": dest + "/late"}, b"")}
            with timed_server(routes) as (base, seen):
                with Http("test", timeout_s=1, gap_s=0, deadline_s=.22) as http:
                    started = time.monotonic()
                    out = fetch_body(http, Robots(http), base + "/start", 6000)
                    self.assertEqual(out["error"], "total_deadline_exceeded")
                    self.assertLess(time.monotonic() - started, .35)
                    time.sleep(.2)
                    self.assertEqual(dest_seen, [])
                    self.assertEqual(seen, ["/robots.txt", "/start"])

    def test_delayed_headers_cancel_socket_before_return(self):
        with slow_server("headers") as (base, seen, closed):
            http = Http("deadline-test", timeout_s=1, gap_s=0)
            http.deadline_s = .18
            self.addCleanup(http.close)
            started = time.monotonic()
            out = http.get(base + "/slow")
            elapsed = time.monotonic() - started
            self.assertEqual(out["error"], "total_deadline_exceeded")
            self.assertLess(elapsed, .45)
            self.assertEqual(out["content"], b"")
            self.assertTrue(closed.wait(.12), "deadline must close the real peer socket")
            self.assertEqual(seen, ["/slow"])
            print("DEADLINE delayed_headers", round(elapsed, 3), "peer_closed", closed.is_set())


class SurveyTests(unittest.TestCase):
    def test_allowed_api_feed_keeps_inline_stats_and_robots_exemption(self):
        entries = [{"title": f"model {i}", "link": f"https://article.example/{i}",
                    "hours_ago": i + 1, "body": "Useful model release. " * 50} for i in range(3)]
        payload = atom(entries)
        with server({"/feed": (200, {}, payload),
                     "/robots.txt": (503, {}, b"API must not request robots")}) as (base, seen):
            with survey.Http(gap_s=0) as http:
                cand = {"id": "local", "name": "local", "route": "api_arxiv", "tier": 1,
                        "lang": "en", "url": base + "/feed"}
                out = survey.survey_one(http, survey.Robots(http), cand, ["model"], [], NOW)
                self.assertEqual(seen, ["/feed"])
                self.assertEqual(http.count, 1)
                self.assertEqual(out["http"]["bytes"], len(payload))
                self.assertEqual(set(out["http"]), {"status", "ms", "bytes", "final_url", "content_type", "error"})
                self.assertEqual(out["robots_feed"], "n/a(API 약관 기준)")
                self.assertTrue(out["parse_ok"])
                self.assertEqual(out["entries"], 3)
                self.assertEqual(out["dated_ratio"], 1)
                self.assertEqual(out["future_dated"], 0)
                self.assertEqual([out[k] for k in ("items_24h", "items_72h", "items_7d")], [3, 3, 3])
                self.assertEqual(out["newest_age_h"], 1)
                self.assertEqual(out["keyword_hit_7d"], 1)
                self.assertEqual(out["body_ok"], "3/3")
                self.assertEqual(out["gates"], {"G1": True, "G2": True, "G3": True})

    def test_non_api_feed_fails_closed_when_robots_unreachable(self):
        with server({"/robots.txt": (503, {}, b"unavailable"),
                     "/feed": (200, {}, atom([]))}) as (base, seen):
            with survey.Http(gap_s=0) as http:
                cand = {"id": "local", "name": "local", "route": "rss", "tier": 1,
                        "lang": "en", "url": base + "/feed"}
                out = survey.survey_one(http, survey.Robots(http), cand, [], [], NOW)
                self.assertEqual(seen, ["/robots.txt"])
                self.assertIsNone(out["http"])
                self.assertEqual(out["robots_feed"], "unreachable")
                self.assertEqual(out["gates"], {"G1": False, "G2": False, "G3": False})

    def test_survey_body_shares_type_size_and_redirect_robots_guards(self):
        cases = [({"Content-Type": "application/pdf"}, b"pdf"),
                 ({"Content-Type": "text/plain", "Content-Encoding": "gzip"},
                  gzip.compress(b"x" * (2 * 1024 * 1024 + 1)))]
        for headers, payload in cases:
            with self.subTest(headers=headers), server({"/body": (200, headers, payload)}) as (base, seen):
                with survey.Http(gap_s=0) as http:
                    out = survey.check_body(http, survey.Robots(http), base + "/body")
                    self.assertEqual(set(out), {"url", "robots", "status", "chars", "ok", "ms", "source"})
                    self.assertFalse(out["ok"])
                    self.assertEqual(out["chars"], 0)
                    self.assertEqual(seen, ["/robots.txt", "/body"])
        with server({"/robots.txt": (200, {}, b"User-agent: *\nDisallow: /private\n"),
                     "/start": (302, {"Location": "/private"}, b""),
                     "/private": (200, {}, b"must not be requested")}) as (base, seen):
            with survey.Http(gap_s=0) as http:
                out = survey.check_body(http, survey.Robots(http), base + "/start")
                self.assertFalse(out["ok"])
                self.assertEqual(seen, ["/robots.txt", "/start"])

    def test_survey_check_body_robots_and_headers_share_deadline(self):
        with timed_server({"/robots.txt": (.13, 404, {}, b""),
                           "/body": (.13, 200, {}, b"body")}) as (base, seen):
            with survey.Http(timeout_s=1, gap_s=0, deadline_s=.2) as http:
                started = time.monotonic()
                out = survey.check_body(http, survey.Robots(http), base + "/body")
                self.assertLess(time.monotonic() - started, .32)
                self.assertFalse(out["ok"])
                self.assertIsNone(out["status"])
                self.assertEqual(seen, ["/robots.txt", "/body"])

    def test_survey_main_closes_its_only_client_even_if_rendering_fails(self):
        created = []
        original = survey.Http
        def factory():
            http = original(gap_s=0)
            created.append(http)
            return http
        try:
            with patch.object(survey, "Http", side_effect=factory), \
                 patch.object(survey, "load_spec", return_value=([], [], [])), \
                 patch.object(survey, "print_table", side_effect=RuntimeError("synthetic render failure")), \
                 patch("sys.argv", ["survey"]):
                with self.assertRaisesRegex(RuntimeError, "synthetic render failure"):
                    survey.main()
            self.assertEqual(len(created), 1, "robots must share the one counted HTTP client")
            self.assertTrue(created[0]._closed)
        finally:
            for http in created:
                http.close()

    def test_survey_feed_redirect_applies_excluded_policy_before_target(self):
        with server({"/feed": (200, {}, b"must not be requested")}) as (dest, dest_seen):
            location = dest.replace("127.0.0.1", "localhost") + "/feed"
            with server({"/feed": (302, {"Location": location}, b"")}) as (base, seen):
                with survey.Http(gap_s=0) as http:
                    cand = {"id": "local", "name": "local", "route": "rss", "tier": 1,
                            "lang": "en", "url": base + "/feed"}
                    out = survey.survey_one(http, survey.Robots(http), cand, [], ["localhost"], NOW)
                    self.assertEqual(dest_seen, [])
                    self.assertEqual(out["http"]["error"], "excluded_host")
                    self.assertFalse(out["parse_ok"])
                    self.assertEqual(seen, ["/robots.txt", "/feed"])
                    self.assertEqual(http.count, 2)

    def test_unreachable_robots_does_not_request_survey_body(self):
        with server({"/robots.txt": (503, {}, b"unavailable"),
                     "/body": (200, {}, b"body must not be requested")}) as (base, seen):
            http = survey.Http()
            http.gap_s = 0
            try:
                out = survey.check_body(http, survey.Robots(http), base + "/body")
                self.assertEqual(out["robots"], "unreachable")
                self.assertEqual(seen, ["/robots.txt"])
                self.assertFalse(out["ok"])
                self.assertIsNone(out["status"])
                self.assertEqual(http.count, 1)
            finally:
                http.close()


class DecodingTests(unittest.TestCase):
    def test_valid_gzip_members_and_zlib_deflate_allow_exact_decoded_size(self):
        raw = b"1234567890"
        payloads = [("gzip", gzip.compress(raw)),
                    ("gzip", gzip.compress(raw[:5]) + gzip.compress(raw[5:])),
                    ("deflate", zlib.compress(raw))]
        for encoding, payload in payloads:
            with self.subTest(encoding=encoding), server({"/encoded": (200, {"Content-Encoding": encoding}, payload)}) as (base, seen):
                with Http("test", gap_s=0) as http:
                    out = http.get(base + "/encoded", max_bytes=10)
                    self.assertIsNone(out["error"])
                    self.assertEqual(out["content"], raw)
                    self.assertEqual(seen, ["/encoded"])

    def test_raw_deflate_remains_supported_and_bounded(self):
        compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        payload = compressor.compress(b"12345678901") + compressor.flush()
        with server({"/deflate": (200, {"Content-Encoding": "deflate"}, payload)}) as (base, seen):
            with Http("test", gap_s=0) as http:
                exact = http.get(base + "/deflate", max_bytes=11)
                self.assertIsNone(exact["error"])
                self.assertEqual(exact["content"], b"12345678901")
                extra = http.get(base + "/deflate", max_bytes=10)
                self.assertEqual(extra["error"], "response_too_large")
                self.assertEqual(extra["content"], b"")

    def test_gzip_bomb_never_materializes_unbounded_decoded_chunk(self):
        payload = gzip.compress(b"x" * (32 * 1024 * 1024))
        observed, responses = [], []
        original = net.zlib.decompressobj
        class Decoder:
            def __init__(self, *args):
                self.actual = original(*args)
            def decompress(self, data, max_length=0):
                output = self.actual.decompress(data, max_length)
                observed.append((max_length, len(output)))
                return output
            def __getattr__(self, name):
                return getattr(self.actual, name)
        with server({"/bomb": (200, {"Content-Encoding": "gzip"}, payload)}) as (base, seen):
            with Http("test", gap_s=0) as http:
                from tests.테스트_수집경계 import observe_responses
                observe_responses(http, responses)
                with patch.object(net.zlib, "decompressobj", Decoder):
                    out = http.get(base + "/bomb", max_bytes=65536)
                self.assertEqual(out["error"], "response_too_large")
                self.assertEqual(out["content"], b"")
                self.assertTrue(observed)
                self.assertTrue(all(0 < bound <= 65537 and produced <= bound for bound, produced in observed))
                self.assertEqual(len(responses), 1)
                self.assertTrue(responses[0].is_closed)

    def test_concatenated_gzip_members_count_toward_same_decoded_limit(self):
        payload = gzip.compress(b"12345") + gzip.compress(b"678901")
        with server({"/gzip": (200, {"Content-Encoding": "gzip"}, payload)}) as (base, seen):
            with Http("test", gap_s=0) as http:
                out = http.get(base + "/gzip", max_bytes=10)
                self.assertEqual(out["error"], "response_too_large")
                self.assertEqual(out["content"], b"")



@contextmanager
def timed_server(routes):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            delay, status, headers, body = routes.get(self.path, (0, 404, {}, b""))
            time.sleep(delay)
            try:
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}", seen
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join()
