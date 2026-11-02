"""HTTP 도우미 — 요청 창구를 한 곳으로 모으고(User-Agent·시간 제한·요청 수),
robots.txt 를 지키며, 웹 페이지에서 본문을 추출한다."""
from __future__ import annotations

import time
from urllib import robotparser
from urllib.parse import urlparse, urljoin

import requests
import trafilatura

from collectors import host_excluded

FEED_MAX_BYTES = 8 * 1024 * 1024
BODY_MAX_BYTES = 2 * 1024 * 1024
ROBOTS_MAX_BYTES = 512 * 1024
BODY_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")


class _ControlledSession(requests.Session):
    def resolve_redirects(self, *args, **kwargs):
        # Requests는 allow_redirects=False여도 다음 PreparedRequest를 만들며
        # 이동 응답 본문을 읽는다. 직접 이동하므로 그 숨은 무제한 읽기도 끈다.
        return iter(())


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

    def __init__(self, user_agent: str, timeout_s: int = 20, gap_s: float = 0.3) -> None:
        self.user_agent = user_agent
        self.timeout_s = timeout_s
        self.gap_s = gap_s                # 연달아 보내는 요청이 한꺼번에 몰리지 않게 두는 간격
        self.count = 0
        self.session = _ControlledSession()
        self.session.headers.update({"User-Agent": user_agent})

    def get(self, url: str, *, excluded_hosts=(), robots=None, max_bytes=None, content_types=None) -> dict:
        started = time.perf_counter()
        limit = FEED_MAX_BYTES if max_bytes is None else max_bytes
        current = url
        result = {"status": None, "content": b"", "final_url": current,
                  "content_type": "", "error": None}
        try:
            for hop in range(6):
                result.update(status=None, content=b"", final_url=current, content_type="")
                error = _url_error(current, excluded_hosts)
                if error is None and robots is not None:
                    verdict = _robots_verdict(robots, current, excluded_hosts)
                    if verdict not in ("allowed", "no_robots"):
                        error = "robots_" + ("disallowed" if verdict == "disallowed" else "unreachable")
                if error:
                    result["error"] = error
                    break
                self.count += 1
                resp = None
                try:
                    resp = self.session.get(current, timeout=self.timeout_s, allow_redirects=False, stream=True)
                    result.update(status=resp.status_code, content_type=resp.headers.get("Content-Type", ""))
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location")
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
                    media_type = result["content_type"].split(";", 1)[0].strip().lower()
                    if media_type and content_types is not None and media_type not in content_types:
                        result["error"] = "content_type_not_allowed"
                        break
                    content = bytearray()
                    for chunk in resp.iter_content(chunk_size=min(65536, limit + 1)):
                        if len(content) + len(chunk) > limit:
                            result["error"] = "response_too_large"
                            break
                        content.extend(chunk)
                    if not result["error"]:
                        result["content"] = bytes(content)
                    break
                finally:
                    if resp is not None:
                        resp.close()
                    time.sleep(self.gap_s)
        except requests.RequestException as exc:
            # 예외 문구에 주소가 섞일 수 있어 종류만 남긴다
            result.update(content=b"", error=type(exc).__name__)
        result["ms"] = round((time.perf_counter() - started) * 1000)
        return result


class Robots:
    """호스트별 robots.txt 를 한 번만 받아 허용 여부를 판정한다."""

    def __init__(self, http: Http) -> None:
        self.http = http
        # 병렬 워커가 동시에 채워도 같은 값이 다시 들어갈 뿐이라 따로 잠그지 않는다
        self._cache: dict[tuple, tuple] = {}

    def verdict(self, url: str) -> str:
        return self._verdict(url, ())

    def _verdict(self, url: str, excluded_hosts) -> str:
        parts = urlparse(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        key = (origin, tuple(sorted(excluded_hosts)))
        if key not in self._cache:
            # robots=None을 유지: robots 조회에 다시 robots 검사를 적용하지 않는다.
            resp = self.http.get(f"{origin}/robots.txt", max_bytes=ROBOTS_MAX_BYTES,
                                 excluded_hosts=excluded_hosts)
            if resp["error"]:
                self._cache[key] = (None, "unreachable")
            elif resp["status"] == 200:
                parser = robotparser.RobotFileParser()
                parser.parse(resp["content"].decode("utf-8", "replace").splitlines())
                self._cache[key] = (parser, "parsed")
            elif resp["status"] is not None and 400 <= resp["status"] < 500:
                self._cache[key] = (None, "no_robots")       # 4xx 는 제한 없음 (RFC 9309)
            else:
                self._cache[key] = (None, "unreachable")     # 5xx·네트워크 오류는 확인 불가
        parser, state = self._cache[key]
        if state == "parsed":
            return "allowed" if parser.can_fetch(self.http.user_agent, url) else "disallowed"
        return state


def extract_text(content: bytes | str) -> str:
    return trafilatura.extract(content) or ""


def fetch_body(http: Http, robots: Robots, url: str, max_chars: int, *, excluded_hosts=()) -> dict:
    """기사 페이지를 받아 본문을 추출한다. robots.txt 가 막은 주소는 받아 오지 않는다."""
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
