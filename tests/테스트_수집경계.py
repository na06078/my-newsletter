"""수집 정책의 공개 경계 시험. 외부 연결 없이 실행한다."""
import unittest
import gzip
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from collectors import collect, host_excluded
from net import Http, Robots, fetch_body
from tests.support import NOW, SRC, atom


@contextmanager
def server(routes):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            status, headers, body = routes.get(self.path, (404, {}, b""))
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            try:
                for offset in range(0, len(body), 4096):
                    self.wfile.write(body[offset:offset + 4096])
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
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


class SourceTests(unittest.TestCase):
    def test_encoded_excluded_article_is_filtered_even_with_inline_body(self):
        entry = {"title": "excluded", "link": "https://%74echcrunch.com/article", "hours_ago": 1,
                 "body": "inline source text " * 100}
        with server({"/feed": (200, {}, atom([entry]))}) as (base, seen):
            http = Http("test", gap_s=0)
            self.addCleanup(http.session.close)
            items, stats = collect({"per_source_cap": 2, "excluded_hosts": ["techcrunch.com"],
                                    "sources": [{**SRC, "url": base + "/feed"}]}, http, NOW, 48)
        self.assertEqual(items, [])
        self.assertEqual(stats[SRC["id"]]["excluded_host"], 1)

    def test_feed_redirect_exclusion_reaches_http_policy(self):
        with server({"/feed": (200, {}, atom([]))}) as (dest, dest_seen):
            location = dest.replace("127.0.0.1", "localhost") + "/feed"
            with server({"/feed": (302, {"Location": location}, b"")}) as (base, seen):
                http = Http("test", gap_s=0)
                http.session.trust_env = False
                self.addCleanup(http.session.close)
                items, stats = collect({"per_source_cap": 2, "excluded_hosts": ["localhost"],
                                        "sources": [{**SRC, "url": base + "/feed"}]}, http, NOW, 48)
        self.assertEqual(dest_seen, [])
        self.assertEqual(seen, ["/feed"])
        self.assertEqual(items, [])
        self.assertEqual(stats[SRC["id"]]["error"], "excluded_host")

    def test_oversized_feed_records_error_not_empty_success(self):
        with server({"/feed": (200, {}, b"x" * (8 * 1024 * 1024 + 1))}) as (base, seen):
            http = Http("test", gap_s=0)
            self.addCleanup(http.session.close)
            items, stats = collect({"per_source_cap": 2, "sources": [{**SRC, "url": base + "/feed"}]},
                                   http, NOW, 48)
        self.assertEqual(items, [])
        self.assertEqual(stats[SRC["id"]]["error"], "response_too_large")

    def test_query_overlap_dedupes_before_cap_and_counts_all_duplicates(self):
        a = {"title": "new", "link": "https://a.example/new", "hours_ago": 1}
        b = {"title": "second", "link": "https://a.example/second", "hours_ago": 2}
        c = {"title": "old", "link": "https://a.example/old", "hours_ago": 3}
        routes = {"/q1": (200, {}, atom([a, b])), "/q2": (200, {}, atom([a, b, c])),
                  "/other": (200, {}, atom([a]))}
        with server(routes) as (base, seen):
            sources = [{**SRC, "urls": [base + "/q1", base + "/q2"]},
                       {**SRC, "id": "other", "url": base + "/other", "tier": 2}]
            http = Http("test", gap_s=0)
            self.addCleanup(http.session.close)
            items, stats = collect({"sources": sources, "per_source_cap": 2}, http, NOW, 48)
        self.assertEqual([i["title"] for i in items], ["new", "second"])
        self.assertEqual(stats["_dedupe_removed"], 3)
        self.assertEqual(stats[SRC["id"]]["kept"], 2)
        self.assertEqual(stats["other"]["kept"], 0)
        self.assertEqual(seen, ["/q1", "/q2", "/other"])


class NetworkTests(unittest.TestCase):
    def test_encoded_destination_is_blocked_in_initial_redirect_and_robots_paths(self):
        with server({"/body": (200, {}, b"must not be requested")}) as (dest, dest_seen):
            location = dest.replace("127.0.0.1", "%6cocalhost") + "/body"
            http = self.http()
            out = http.get(location, excluded_hosts=["localhost"])
            self.assertEqual(out["error"], "excluded_host")
            with server({"/start": (302, {"Location": location}, b""),
                         "/robots.txt": (302, {"Location": location}, b"")}) as (base, seen):
                out = http.get(base + "/start", excluded_hosts=["localhost"])
                self.assertEqual(out["error"], "excluded_host")
                body = fetch_body(http, Robots(http), base + "/body", 6000, excluded_hosts=["localhost"])
                self.assertEqual(body["robots"], "unreachable")
                self.assertEqual(seen, ["/start", "/robots.txt"])
        self.assertEqual(dest_seen, [])

    def test_malformed_redirect_returns_safe_error_and_other_source_continues(self):
        responses = []
        routes = {"/bad": (302, {"Location": "http://[broken"}, b""),
                  "/robots.txt": (302, {"Location": "http://[broken"}, b""),
                  "/good": (200, {}, atom([{"title": "good", "link": "https://safe.example/good", "hours_ago": 1}]))}
        with server(routes) as (base, seen):
            http = self.http()
            http.session.hooks["response"].append(lambda response, **kw: responses.append(response))
            out = http.get(base + "/bad")
            self.assertEqual(out["error"], "redirect_location")
            body = fetch_body(http, Robots(http), base + "/body", 6000)
            self.assertEqual(body["robots"], "unreachable")
            items, stats = collect({"per_source_cap": 2, "sources": [
                {**SRC, "id": "bad", "url": base + "/bad"}, {**SRC, "id": "good", "url": base + "/good"}]}, http, NOW, 48)
        self.assertEqual([item["title"] for item in items], ["good"])
        self.assertEqual(stats["bad"]["error"], "redirect_location")
        self.assertTrue(all(response.raw.closed for response in responses))

    def test_robots_redirect_cannot_bypass_excluded_host(self):
        with server({"/policy": (200, {}, b"User-agent: *\nAllow: /\n")}) as (dest, dest_seen):
            location = dest.replace("127.0.0.1", "localhost") + "/policy"
            with server({"/robots.txt": (302, {"Location": location}, b""),
                         "/body": (200, {}, b"forbidden")}) as (base, seen):
                http = self.http()
                robots = Robots(http)
                self.assertEqual(robots.verdict(base + "/body"), "allowed")
                self.assertEqual(dest_seen, ["/policy"])
                seen.clear()
                dest_seen.clear()
                # 느슨한 정책으로 채운 캐시를 엄격한 정책에서 재사용하지 않는다.
                out = fetch_body(http, robots, base + "/body", 6000, excluded_hosts=["localhost"])
        self.assertEqual(dest_seen, [])
        self.assertEqual(seen, ["/robots.txt"])
        self.assertEqual(out["robots"], "unreachable")

    def test_allowed_redirect_extracts_body_and_closes_every_response(self):
        article = b"<html><body><article><p>" + b"A useful local model release improves inference. " * 50 + b"</p></article></body></html>"
        routes = {"/start": (302, {"Location": "/article"}, b"redirect body must not be buffered"),
                  "/article": (200, {"Content-Type": "Text/HTML; charset=utf-8"}, article)}
        responses = []
        with server(routes) as (base, seen):
            http = self.http()
            http.session.hooks["response"].append(lambda response, **kw: responses.append(response))
            out = fetch_body(http, Robots(http), base + "/start", 6000)
        self.assertIsNone(out["error"])
        self.assertIn("improves inference", out["text"])
        self.assertEqual(seen, ["/robots.txt", "/start", "/article"])
        self.assertTrue(all(response.raw.closed for response in responses))
        self.assertIs(responses[1]._content, False)  # 이동 응답을 Requests 내부에서 미리 읽지 않음.

    def test_excluded_redirect_destination_receives_no_robots_or_body(self):
        with server({"/body": (200, {}, b"forbidden")}) as (dest, dest_seen):
            location = dest.replace("127.0.0.1", "localhost") + "/body"
            with server({"/start": (302, {"Location": location}, b"")}) as (base, seen):
                http = self.http()
                out = fetch_body(http, Robots(http), base + "/start", 6000, excluded_hosts=["localhost"])
        self.assertEqual(out["error"], "excluded_host")
        self.assertEqual(dest_seen, [])
        self.assertEqual(seen, ["/robots.txt", "/start"])

    def test_redirect_to_unreachable_robots_never_requests_target_body(self):
        with server({"/robots.txt": (503, {}, b"unavailable"), "/body": (200, {}, b"forbidden")}) as (dest, dest_seen):
            with server({"/start": (302, {"Location": dest + "/body"}, b"")}) as (base, seen):
                http = self.http()
                out = fetch_body(http, Robots(http), base + "/start", 6000)
        self.assertEqual(out["error"], "robots_unreachable")
        self.assertEqual(dest_seen, ["/robots.txt"])
        self.assertEqual(seen, ["/robots.txt", "/start"])

    def test_initial_policy_rejects_before_robots_or_request(self):
        with server({}) as (base, seen):
            http = self.http()
            for url, hosts, expected in ((base + "/body", ["127.0.0.1"], "excluded_host"),
                                         (base.replace("://", "://user:pass@") + "/body", [], "url_credentials"),
                                         ("file:///private", [], "unsupported_url")):
                out = fetch_body(http, Robots(http), url, 6000, excluded_hosts=hosts)
                self.assertEqual(out["error"], expected)
        self.assertEqual(seen, [])

    def test_redirect_scheme_and_credentials_are_blocked_before_target(self):
        for destination, error in (("file:///private", "unsupported_url"),
                                   ("http://user:pass@127.0.0.1/private", "url_credentials")):
            with self.subTest(destination=destination), server({"/start": (302, {"Location": destination}, b"")}) as (base, seen):
                out = self.http().get(base + "/start")
                self.assertEqual(out["error"], error)
                self.assertEqual(seen, ["/start"])

    def test_five_redirect_limit_closes_all_without_reading_bodies(self):
        routes = {f"/{i}": (302, {"Location": f"/{i + 1}"}, b"x" * 100000) for i in range(7)}
        responses = []
        with server(routes) as (base, seen):
            http = self.http()
            http.session.hooks["response"].append(lambda response, **kw: responses.append(response))
            out = http.get(base + "/0")
        self.assertEqual(out["error"], "redirect_limit")
        self.assertEqual(seen, ["/0", "/1", "/2", "/3", "/4", "/5"])
        self.assertEqual(http.count, 6)
        self.assertTrue(all(response.raw.closed and response._content is False for response in responses))

    def test_accepted_body_types_and_missing_header_have_positive_control(self):
        article = b"<html><body><article><p>" + b"Useful local model release improves inference. " * 50 + b"</p></article></body></html>"
        for media in (None, "text/html", "application/xhtml+xml", "text/plain"):
            headers = {"Content-Type": media} if media else {}
            with self.subTest(media=media), server({"/body": (200, headers, article)}) as (base, seen):
                http = self.http()
                out = fetch_body(http, Robots(http), base + "/body", 6000)
                self.assertIsNone(out["error"])
                self.assertIn("improves inference", out["text"])

    def test_body_rejects_non_text_content_type(self):
        responses = []
        with server({"/body": (200, {"Content-Type": "application/pdf"}, b"not an article")}) as (base, seen):
            http = self.http()
            http.session.hooks["response"].append(lambda response, **kw: responses.append(response))
            out = fetch_body(http, Robots(http), base + "/body", 6000)
        self.assertEqual(out["error"], "content_type_not_allowed")
        self.assertEqual(out["text"], "")
        self.assertTrue(all(response.raw.closed for response in responses))
        self.assertIs(responses[-1]._content, False)

    def test_decoded_stream_limit_rejects_plain_and_gzip(self):
        expanded = b"x" * (2 * 1024 * 1024 + 1)
        for encoding, payload in ((None, expanded), ("gzip", gzip.compress(expanded))):
            with self.subTest(encoding=encoding):
                headers = {"Content-Type": "text/plain"}
                if encoding:
                    headers["Content-Encoding"] = encoding
                responses = []
                with server({"/body": (200, headers, payload)}) as (base, seen):
                    http = self.http()
                    http.session.hooks["response"].append(lambda response, **kw: responses.append(response))
                    out = fetch_body(http, Robots(http), base + "/body", 6000)
                self.assertEqual(out["error"], "response_too_large")
                self.assertEqual(out["text"], "")
                self.assertEqual(seen, ["/robots.txt", "/body"])
                self.assertTrue(all(response.raw.closed for response in responses))

    def test_explicit_byte_limit_allows_exact_length_then_rejects_one_extra(self):
        with server({"/exact": (200, {}, b"1234567890"), "/extra": (200, {}, b"12345678901")}) as (base, seen):
            http = self.http()
            exact = http.get(base + "/exact", max_bytes=10)
            extra = http.get(base + "/extra", max_bytes=10)
        self.assertEqual(exact["content"], b"1234567890")
        self.assertIsNone(exact["error"])
        self.assertEqual(extra["content"], b"")
        self.assertEqual(extra["error"], "response_too_large")
        self.assertEqual(set(exact), {"status", "content", "final_url", "content_type", "error", "ms"})

    def http(self):
        http = Http("test-bot", timeout_s=2, gap_s=0)
        http.session.trust_env = False
        self.addCleanup(http.session.close)
        return http

    def test_oversized_robots_is_unreachable_not_allow_all(self):
        with server({"/robots.txt": (200, {}, b"#" * (512 * 1024 + 1)),
                     "/body": (200, {}, b"body")}) as (base, seen):
            http = self.http()
            out = fetch_body(http, Robots(http), base + "/body", 6000)
        self.assertEqual(out["robots"], "unreachable")
        self.assertEqual(seen, ["/robots.txt"])

    def test_unreachable_robots_never_requests_body(self):
        with server({"/robots.txt": (503, {}, b"unavailable"),
                     "/body": (200, {}, b"body")}) as (base, seen):
            http = self.http()
            out = fetch_body(http, Robots(http), base + "/body", 6000)
        self.assertEqual(out["robots"], "unreachable")
        self.assertEqual(seen, ["/robots.txt"])
        self.assertEqual(out["text"], "")

    def test_redirect_checks_destination_robots_before_request(self):
        routes = {"/robots.txt": (200, {}, b"User-agent: *\nDisallow: /private\n"),
                  "/start": (302, {"Location": "/private"}, b""),
                  "/private": (200, {}, b"forbidden")}
        with server(routes) as (base, seen):
            http = self.http()
            out = fetch_body(http, Robots(http), base + "/start", 6000)
        self.assertEqual(seen, ["/robots.txt", "/start"])
        self.assertEqual(out["error"], "robots_disallowed")
        self.assertEqual(out["text"], "")


class HostTests(unittest.TestCase):
    def test_real_hostname_excludes_port_userinfo_and_terminal_dot(self):
        for url in ("https://TECHCRUNCH.com:443/a", "https://user:pass@techcrunch.com/a",
                    "https://sub.techcrunch.com./a", "https://%74echcrunch.com/a"):
            with self.subTest(url=url):
                self.assertTrue(host_excluded(url, ["techcrunch.com"]))
        for url in ("https://techcrunch.com@safe.example/a", "https://nottechcrunch.com/a",
                    "https://techcrunch.com.safe.example/a"):
            self.assertFalse(host_excluded(url, ["techcrunch.com"]))
