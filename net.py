"""HTTP 도우미 — 요청 창구를 한 곳으로 모으고(User-Agent·시간 제한·요청 수),
robots.txt 를 지키며, 웹 페이지에서 본문을 추출한다."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
import threading
import time
import zlib
from urllib import robotparser
from urllib.parse import urlparse, urljoin

import httpx
import trafilatura

from collectors import host_excluded

FEED_MAX_BYTES = 8 * 1024 * 1024
BODY_MAX_BYTES = 2 * 1024 * 1024
ROBOTS_MAX_BYTES = 512 * 1024
BODY_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
REQUEST_DEADLINE_S = 60


class _DeadlineExceeded(Exception):
    pass


def _url_error(url, excluded_hosts):
    try:
        parts = urlparse(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return "unsupported_url"
        if parts.username is not None or parts.password is not None:
            return "url_credentials"
        parts.port  # 잘못된 포트도 요청 전에 거절한다.
        if host_excluded(url, excluded_hosts):
            return "excluded_host"
        if "%" in parts.hostname:
            return "unsupported_url"  # 준비/전송 단계마다 달리 해석할 수 있는 encoded hostname은 지원하지 않는다.
    except ValueError:
        return "unsupported_url"
    return None


def _robots_verdict(robots, url, excluded_hosts):
    # 내장 Robots의 조회 이동에도 같은 제외 정책을 전달하되,
    # 기존 외부 어댑터의 verdict(url) 인터페이스는 바꾸지 않는다.
    if isinstance(robots, Robots):
        return robots._verdict(url, excluded_hosts)
    return robots.verdict(url)


class Http:
    """GET 요청 래퍼. 실패는 예외 대신 결과 dict 로 돌려줘 호출한 쪽이 기록하게 한다."""

    def __init__(self, user_agent: str, timeout_s: int = 20, gap_s: float = 0.3,
                 *, deadline_s: float = REQUEST_DEADLINE_S) -> None:
        self.user_agent = user_agent
        self.timeout_s = timeout_s
        self.gap_s = gap_s                # 연달아 보내는 요청이 한꺼번에 몰리지 않게 두는 간격
        self.count = 0
        self.deadline_s = deadline_s
        self._deadline = ContextVar("http_deadline", default=None)
        self._deadline_expired = ContextVar("http_deadline_expired", default=False)
        self._lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._thread = None
        self._closed = False
        # 한 실행에서 TLS/연결 풀을 재사용한다. 자동 이동·암묵적 프록시는 사용하지 않는다.
        self._client = httpx.AsyncClient(headers={"User-Agent": user_agent,
                                                  "Accept-Encoding": "gzip, deflate"},
                                         timeout=timeout_s, follow_redirects=False, trust_env=False)

    @contextmanager
    def _budget(self):
        token = None
        expired_token = None
        if self._deadline.get() is None:
            token = self._deadline.set(time.monotonic() + self.deadline_s)
            expired_token = self._deadline_expired.set(False)
        try:
            yield
        finally:
            if token is not None:
                self._deadline.reset(token)
                self._deadline_expired.reset(expired_token)

    def _remaining(self):
        remaining = self._deadline.get() - time.monotonic()
        if self._deadline_expired.get() or remaining <= 0:
            raise _DeadlineExceeded
        return remaining

    def _submit(self, coroutine):
        with self._lock:
            if self._closed:
                coroutine.close()
                raise RuntimeError("http_closed")
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop.run_forever, daemon=True,
                                                name="newsletter-http")
                self._thread.start()
            future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        # Future가 timeout으로 버려지는 것이 아니라 실제 async context 종료까지 기다린다.
        return future.result()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
        if thread is not None:
            asyncio.run_coroutine_threadsafe(self._client.aclose(), self._loop).result()
            self._loop.call_soon_threadsafe(self._loop.stop)
            thread.join()
        else:
            asyncio.run(self._client.aclose())
        self._loop.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    async def _request(self, url, limit, content_types, result, deadline):
        # 헤더 한 바이트씩의 수신도 포함해 전체 대기 자체를 취소한다.
        if time.monotonic() >= deadline:
            raise TimeoutError
        async with asyncio.timeout(max(0, deadline - time.monotonic())):
            with self._lock:
                self.count += 1
            async with self._client.stream("GET", url, timeout=self.timeout_s) as resp:
                result.update(status=resp.status_code, content_type=resp.headers.get("Content-Type", ""))
                if resp.status_code in (301, 302, 303, 307, 308):
                    return resp.headers.get("Location")
                media_type = result["content_type"].split(";", 1)[0].strip().lower()
                if media_type and content_types is not None and media_type not in content_types:
                    result["error"] = "content_type_not_allowed"
                    return None
                encoding = resp.headers.get("Content-Encoding", "identity").strip().lower()
                if encoding not in ("", "identity", "gzip", "deflate"):
                    result["error"] = "unsupported_content_encoding"
                    return None
                decoder = (zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS)
                           if encoding in ("gzip", "deflate") else None)
                content = bytearray()
                first_decode = True
                # HTTPX aiter_bytes는 해제된 큰 청크를 먼저 만든다. raw+max_length로
                # 압축 폭탄도 최대 limit+1 바이트만 할당한 뒤 거절한다.
                async for raw in resp.aiter_raw(chunk_size=65536):
                    pending = raw
                    while pending:
                        if decoder and decoder.eof:
                            if encoding != "gzip":
                                result["error"] = "DecodingError"
                                return None
                            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                        try:
                            chunk = decoder.decompress(pending, limit + 1 - len(content)) if decoder else pending
                        except zlib.error:
                            if encoding != "deflate" or not first_decode:
                                raise
                            # 기존 Requests/HTTPX와 같은 raw-deflate 호환 경로도 상한을 유지한다.
                            decoder = zlib.decompressobj(-zlib.MAX_WBITS)
                            chunk = decoder.decompress(pending, limit + 1 - len(content))
                        first_decode = False
                        if len(content) + len(chunk) > limit:
                            result["error"] = "response_too_large"
                            return None
                        content.extend(chunk)
                        pending = decoder.unused_data if decoder else b""
                if decoder and not decoder.eof:
                    result["error"] = "DecodingError"
                    return None
                result["content"] = bytes(content)
                return None

    def get(self, url: str, *, excluded_hosts=(), robots=None, max_bytes=None, content_types=None) -> dict:
        with self._budget():
            return self._get(url, excluded_hosts, robots, max_bytes, content_types)

    def _get(self, url, excluded_hosts, robots, max_bytes, content_types):
        started = time.perf_counter()
        limit = FEED_MAX_BYTES if max_bytes is None else max_bytes
        current = url
        result = {"status": None, "content": b"", "final_url": current,
                  "content_type": "", "error": None}
        try:
            for hop in range(6):
                result.update(status=None, content=b"", final_url=current, content_type="")
                self._remaining()
                error = _url_error(current, excluded_hosts)
                if error is None and robots is not None:
                    verdict = _robots_verdict(robots, current, excluded_hosts)
                    if verdict not in ("allowed", "no_robots"):
                        error = "robots_" + ("disallowed" if verdict == "disallowed" else "unreachable")
                self._remaining()
                if error:
                    result["error"] = error
                    break
                try:
                    location = self._submit(self._request(current, limit, content_types, result, self._deadline.get()))
                    if result["status"] in (301, 302, 303, 307, 308):
                        if not location:
                            result["error"] = "redirect_location"
                            break
                        if hop == 5:
                            result["error"] = "redirect_limit"
                            break
                        try:
                            current = urljoin(current, location)
                        except ValueError:
                            result["error"] = "redirect_location"
                            break
                        continue
                    break
                finally:
                    remaining = self._deadline.get() - time.monotonic()
                    time.sleep(max(0, min(self.gap_s, remaining)))
                    if self.gap_s > 0 and remaining <= self.gap_s:
                        # Windows의 sleep이 살짝 일찍 깨어나도 짧아진 간격으로 다음 요청을 보내지 않는다.
                        raise _DeadlineExceeded
        except (TimeoutError, _DeadlineExceeded):
            # 중첩 robots의 명시적 취소를 벽시계 경계의 미세 오차 때문에 다른 사유로 바꾸지 않는다.
            self._deadline_expired.set(True)
            result.update(content=b"", error="total_deadline_exceeded")
        except httpx.InvalidURL:
            result.update(content=b"", error="unsupported_url")
        except (httpx.HTTPError, zlib.error) as exc:
            # 예외 문구에 주소가 섞일 수 있어 종류만 남긴다
            result.update(content=b"", error=type(exc).__name__)
        result["ms"] = round((time.perf_counter() - started) * 1000)
        return result


class Robots:
    """호스트별 robots.txt 를 한 번만 받아 허용 여부를 판정한다."""

    def __init__(self, http: Http) -> None:
        self.http = http
        self._cache: dict[tuple, tuple] = {}
        self._cache_guard = threading.Lock()
        self._key_locks: dict[tuple, threading.Lock] = {}

    def verdict(self, url: str) -> str:
        return self._verdict(url, ())

    def _verdict(self, url: str, excluded_hosts) -> str:
        # 공개 verdict도 자체 예산을 갖되, get/fetch_body의 활성 예산은 재사용한다.
        with self.http._budget() if isinstance(self.http, Http) else nullcontext():
            try:
                return self._cached_verdict(url, excluded_hosts)
            except _DeadlineExceeded:
                self.http._deadline_expired.set(True)
                return "unreachable"

    def _cached_verdict(self, url, excluded_hosts):
        parts = urlparse(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        key = (origin, tuple(sorted(excluded_hosts)))
        with self._cache_guard:
            key_lock = self._key_locks.setdefault(key, threading.Lock())
        if isinstance(self.http, Http):
            acquired = key_lock.acquire(timeout=self.http._remaining())
            if not acquired:
                raise _DeadlineExceeded
        else:
            key_lock.acquire()
        try:
            if isinstance(self.http, Http):
                self.http._remaining()
            with self._cache_guard:
                cached = self._cache.get(key)
            if cached is None:
                cached = self._load(origin, excluded_hosts)
                if cached is None:
                    # 소유자의 개인 기한은 사이트 상태가 아니다. 다음 대기자가 재시도한다.
                    return "unreachable"
                with self._cache_guard:
                    self._cache[key] = cached
        finally:
            key_lock.release()
        parser, state = cached
        if state == "parsed":
            return "allowed" if parser.can_fetch(self.http.user_agent, url) else "disallowed"
        return state

    def _load(self, origin, excluded_hosts):
        # robots=None을 유지: robots 조회에 다시 robots 검사를 적용하지 않는다.
        resp = self.http.get(f"{origin}/robots.txt", max_bytes=ROBOTS_MAX_BYTES,
                             excluded_hosts=excluded_hosts)
        if resp["error"] == "total_deadline_exceeded":
            if isinstance(self.http, Http):
                self.http._deadline_expired.set(True)
            return None
        if resp["error"]:
            return (None, "unreachable")
        if resp["status"] == 200:
            parser = robotparser.RobotFileParser()
            parser.parse(resp["content"].decode("utf-8", "replace").splitlines())
            return (parser, "parsed")
        if resp["status"] is not None and 400 <= resp["status"] < 500:
            return (None, "no_robots")       # 4xx 는 제한 없음 (RFC 9309)
        return (None, "unreachable")         # 5xx·네트워크 오류는 확인 불가


def extract_text(content: bytes | str) -> str:
    return trafilatura.extract(content) or ""


def fetch_body(http: Http, robots: Robots, url: str, max_chars: int, *, excluded_hosts=()) -> dict:
    """기사 페이지를 받아 본문을 추출한다. robots.txt 가 막은 주소는 받아 오지 않는다."""
    with http._budget() if isinstance(http, Http) else nullcontext():
        out = _fetch_body(http, robots, url, max_chars, excluded_hosts)
        if isinstance(http, Http) and (http._deadline_expired.get() or time.monotonic() >= http._deadline.get()):
            out.update(text="", error="total_deadline_exceeded")
        return out


def _fetch_body(http, robots, url, max_chars, excluded_hosts):
    policy_error = _url_error(url, excluded_hosts)
    if policy_error:
        return {"text": "", "status": None, "robots": "not_checked", "error": policy_error}
    verdict = _robots_verdict(robots, url, excluded_hosts)
    out = {"text": "", "status": None, "robots": verdict, "error": None}
    if verdict not in ("allowed", "no_robots"):
        return out
    resp = http.get(url, excluded_hosts=excluded_hosts, robots=robots, max_bytes=BODY_MAX_BYTES,
                    content_types=BODY_CONTENT_TYPES)
    out["status"], out["error"] = resp["status"], resp["error"]
    if resp["status"] == 200 and not resp["error"]:
        out["text"] = extract_text(resp["content"])[:max_chars]
    return out
