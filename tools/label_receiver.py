"""라벨 수신기 — 라벨링 위젯이 보내는 라벨을 한 번 받아 eval/labels.json 으로 저장하고 스스로 종료한다.

127.0.0.1(이 PC 안)에서만 열리고, 유효한 라벨 한 번을 받으면 바로 닫힌다. 외부 네트워크와 통신하지 않는다.
검증: 표본 시각(snapshot)이 eval/candidates.json 과 같고, 모든 id 가 표본 안에 있고, 값이 0·1·2 이며, 25건 이상일 것.

  python tools/label_receiver.py            # 포트 8765, 최대 30분 대기
"""
from __future__ import annotations

import argparse
import json
import hashlib
import hmac
import os
import re
import socket
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from 불변저장 import write_bytes_once  # noqa: E402
CANDIDATES = ROOT / "eval" / "candidates.json"
OUT = Path(os.environ.get("LABEL_OUT", ROOT / "eval" / "labels.json"))   # 시험할 때만 다른 경로로 바꾼다
PORT, MIN_LABELS, WAIT_SECONDS = int(os.environ.get("LABEL_PORT", 8765)), 25, 1800
KST = timezone(timedelta(hours=9))
MAX_BODY_BYTES, READ_SECONDS = 128 * 1024, 5


class HTTPServer(ThreadingHTTPServer):
    """대기 종료와 읽기 제한은 느린 연결의 데이터 도착 여부와 무관하다."""
    daemon_threads = True
    block_on_close = False

    def __init__(self, *args, **kwargs):
        self.connections = {}
        self.connection_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    @staticmethod
    def abort(connection):
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        connection.close()

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(READ_SECONDS)
        timer = threading.Timer(READ_SECONDS, self.abort, (connection,))
        timer.daemon = True
        with self.connection_lock:
            self.connections[connection] = timer
        timer.start()
        return connection, address

    def close_request(self, request):
        with self.connection_lock:
            timer = self.connections.pop(request, None)
        if timer:
            timer.cancel()
        super().close_request(request)

    def close_connections(self):
        with self.connection_lock:
            connections = list(self.connections.items())
        for connection, timer in connections:
            timer.cancel()
            self.abort(connection)

    def handle_error(self, request, client_address):
        # 클라이언트 입력·세션 원문은 traceback 에도 기록하지 않는다.
        pass


def validate(payload: dict, candidates: dict) -> tuple[dict | None, str | None]:
    """라벨 payload 를 검증한다. (정제된 라벨, 오류 문구)를 돌려준다."""
    if not isinstance(candidates, dict) or not isinstance(payload, dict) or payload.get("snapshot") != candidates.get("snapshot_at_kst") or not candidates.get("snapshot_at_kst"):
        return None, "표본 시각이 다릅니다"
    items = candidates.get("items", [])
    if not isinstance(items, list) or any(not isinstance(i, dict) or not isinstance(i.get("id"), str) or not i["id"].strip() for i in items):
        return None, "표본 id 형식이 잘못되었습니다"
    ids = {i["id"] for i in items}
    if len(ids) != len(items):
        return None, "표본 id 가 중복되었습니다"
    labels = payload.get("labels")
    if not isinstance(labels, dict) or not labels:
        return None, "라벨이 비어 있습니다"
    if any(k not in ids for k in labels) or any(type(v) is not int or v not in (0, 1, 2) for v in labels.values()):
        return None, "표본에 없는 id 이거나 값이 0·1·2 가 아닙니다"
    if len(labels) < MIN_LABELS:
        return None, f"{MIN_LABELS}건 이상 필요합니다 (현재 {len(labels)}건)"
    return {k: int(v) for k, v in labels.items()}, None


def unique_object(pairs):
    """JSON 내 중복 키를 조용히 덮어쓰지 않는다. 원문은 오류에 넣지 않는다."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def make_handler(server_ref: dict):
    class Handler(BaseHTTPRequestHandler):
        def allowed_origin(self):
            return self.headers.get("Origin") in {"null", f"http://127.0.0.1:{self.server.server_port}",
                                                   f"http://localhost:{self.server.server_port}"}

        def _reply(self, code: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            if self.allowed_origin():
                self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
                self.send_header("Vary", "Origin")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except OSError:
                pass

        def do_OPTIONS(self):
            if not self.allowed_origin():
                return self._reply(403, {"ok": False, "error": "origin_not_allowed"})
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_POST(self):
            if not self.allowed_origin():
                return self._reply(403, {"ok": False, "error": "origin_not_allowed"})
            if self.path != "/labels":
                return self._reply(404, {"ok": False, "error": "없는 경로"})
            try:
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) != 1 or self.headers.get("Transfer-Encoding") or not lengths[0].isdigit():
                    return self._reply(400, {"ok": False, "error": "invalid_length"})
                length = int(lengths[0])
                if length > MAX_BODY_BYTES:
                    return self._reply(413, {"ok": False, "error": "body_too_large"})
                payload = json.loads(self.rfile.read(length).decode("utf-8"), object_pairs_hook=unique_object)
                candidates = json.loads(server_ref.get("candidates_path", CANDIDATES).read_text(encoding="utf-8"))
                if not valid_session(payload, candidates):
                    return self._reply(403, {"ok": False, "error": "invalid_session"})
                labels, error = validate(payload, candidates)
            except (ValueError, KeyError, TypeError):
                return self._reply(400, {"ok": False, "error": "invalid_payload"})
            except OSError:
                return
            if error:
                return self._reply(400, {"ok": False, "error": error})
            try:
                saved = {"snapshot": payload["snapshot"], "labeled_via": "widget(127.0.0.1 수신기)",
                         "received_at_kst": datetime.now(KST).isoformat(timespec="seconds"),
                         "count": len(labels), "labels": labels}
                out_path = server_ref.get("out_path", OUT)
                write_bytes_once(out_path, json.dumps(saved, ensure_ascii=False, sort_keys=True,
                                                separators=(",", ":")).encode("utf-8"))
            except FileExistsError:
                return self._reply(409, {"ok": False, "error": "labels_exist"})
            except OSError:
                return self._reply(500, {"ok": False, "error": "label_write_failed"})
            server_ref["received"] = True
            self._reply(200, {"ok": True, "count": len(labels)})
            print(f"수신 완료: {len(labels)}건 → {out_path.name}", flush=True)
            threading.Thread(target=server_ref["server"].shutdown, daemon=True).start()   # 한 번 받으면 닫는다

        def log_message(self, *args):       # 요청 로그는 남기지 않는다
            pass

    return Handler


def valid_session(payload, candidates):
    if not isinstance(payload, dict):
        return False
    session_id, token = payload.get("session_id"), payload.get("token")
    if not isinstance(session_id, str) or not re.fullmatch(r"[a-f0-9]{32}", session_id) or not isinstance(token, str):
        return False
    try:
        session = json.loads((ROOT / "store/cache/label_sessions" / (session_id + ".json")).read_text(encoding="utf-8"))
        digest = hashlib.sha256(json.dumps(candidates, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        return (hmac.compare_digest(token, session["token"]) and session["session_id"] == session_id
                and session["snapshot"] == payload.get("snapshot") == candidates.get("snapshot_at_kst")
                and session["candidates_sha256"] == digest and time.time() < session["expires_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="라벨 수신기")
    parser.add_argument("--candidates", type=Path, default=CANDIDATES)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args([] if argv is None else argv)
    ref: dict = {"candidates_path": args.candidates, "out_path": args.out}
    server = HTTPServer(("127.0.0.1", PORT), make_handler(ref))
    ref["server"] = server
    timer = threading.Timer(WAIT_SECONDS, server.shutdown)
    timer.daemon = True                                             # 수신을 마치면 타이머가 프로세스를 붙잡지 않게 한다
    timer.start()
    print(f"대기 중: http://127.0.0.1:{PORT}/labels (최대 {WAIT_SECONDS // 60}분)", flush=True)
    try:
        server.serve_forever(poll_interval=0.05)
    finally:
        timer.cancel()
        server.close_connections()
        server.server_close()
    return 0 if ref.get("received") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
