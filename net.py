"""HTTP 도우미 — 요청 창구를 한 곳으로 모으고(User-Agent·시간 제한·요청 수),
robots.txt 를 지키며, 웹 페이지에서 본문을 추출한다."""
from __future__ import annotations

import time
from urllib import robotparser
from urllib.parse import urlparse

import requests
import trafilatura


class Http:
    """GET 요청 래퍼. 실패는 예외 대신 결과 dict 로 돌려줘 호출한 쪽이 기록하게 한다."""

    def __init__(self, user_agent: str, timeout_s: int = 20, gap_s: float = 0.3) -> None:
        self.user_agent = user_agent
        self.timeout_s = timeout_s
        self.gap_s = gap_s                # 연달아 보내는 요청이 한꺼번에 몰리지 않게 두는 간격
        self.count = 0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})

    def get(self, url: str) -> dict:
        self.count += 1
        started = time.perf_counter()
        try:
            resp = self.session.get(url, timeout=self.timeout_s)
            result = {"status": resp.status_code, "content": resp.content, "final_url": resp.url,
                      "content_type": resp.headers.get("Content-Type", ""), "error": None}
        except requests.RequestException as exc:
            # 예외 문구에 주소가 섞일 수 있어 종류만 남긴다
            result = {"status": None, "content": b"", "final_url": url, "content_type": "",
                      "error": type(exc).__name__}
        result["ms"] = round((time.perf_counter() - started) * 1000)
        time.sleep(self.gap_s)
        return result


class Robots:
    """호스트별 robots.txt 를 한 번만 받아 허용 여부를 판정한다."""

    def __init__(self, http: Http) -> None:
        self.http = http
        # 병렬 워커가 동시에 채워도 같은 값이 다시 들어갈 뿐이라 따로 잠그지 않는다
        self._cache: dict[str, tuple] = {}

    def verdict(self, url: str) -> str:
        parts = urlparse(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._cache:
            resp = self.http.get(f"{origin}/robots.txt")
            if resp["status"] == 200:
                parser = robotparser.RobotFileParser()
                parser.parse(resp["content"].decode("utf-8", "replace").splitlines())
                self._cache[origin] = (parser, "parsed")
            elif resp["status"] is not None and 400 <= resp["status"] < 500:
                self._cache[origin] = (None, "no_robots")       # 4xx 는 제한 없음 (RFC 9309)
            else:
                self._cache[origin] = (None, "unreachable")     # 5xx·네트워크 오류는 확인 불가
        parser, state = self._cache[origin]
        if state == "parsed":
            return "allowed" if parser.can_fetch(self.http.user_agent, url) else "disallowed"
        return state


def extract_text(content: bytes | str) -> str:
    return trafilatura.extract(content) or ""


def fetch_body(http: Http, robots: Robots, url: str, max_chars: int) -> dict:
    """기사 페이지를 받아 본문을 추출한다. robots.txt 가 막은 주소는 받아 오지 않는다."""
    verdict = robots.verdict(url)
    out = {"text": "", "status": None, "robots": verdict, "error": None}
    if verdict == "disallowed":
        return out
    resp = http.get(url)
    out["status"], out["error"] = resp["status"], resp["error"]
    if resp["status"] == 200:
        out["text"] = extract_text(resp["content"])[:max_chars]
    return out
