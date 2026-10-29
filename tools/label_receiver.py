"""라벨 수신기 — 라벨링 위젯이 보내는 라벨을 한 번 받아 eval/labels.json 으로 저장하고 스스로 종료한다.

127.0.0.1(이 PC 안)에서만 열리고, 유효한 라벨 한 번을 받으면 바로 닫힌다. 외부 네트워크와 통신하지 않는다.
검증: 표본 시각(snapshot)이 eval/candidates.json 과 같고, 모든 id 가 표본 안에 있고, 값이 0·1·2 이며, 25건 이상일 것.

  python tools/label_receiver.py            # 포트 8765, 최대 30분 대기
"""
from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CANDIDATES = ROOT / "eval" / "candidates.json"
OUT = Path(os.environ.get("LABEL_OUT", ROOT / "eval" / "labels.json"))   # 시험할 때만 다른 경로로 바꾼다
PORT, MIN_LABELS, WAIT_SECONDS = int(os.environ.get("LABEL_PORT", 8765)), 25, 1800
KST = timezone(timedelta(hours=9))


def validate(payload: dict, candidates: dict) -> tuple[dict | None, str | None]:
    """라벨 payload 를 검증한다. (정제된 라벨, 오류 문구)를 돌려준다."""
    if payload.get("snapshot") != candidates["snapshot_at_kst"]:
        return None, "표본 시각이 다릅니다"
    ids = {i["id"] for i in candidates["items"]}
    labels = payload.get("labels")
    if not isinstance(labels, dict) or not labels:
        return None, "라벨이 비어 있습니다"
    if any(k not in ids for k in labels) or any(v not in (0, 1, 2) for v in labels.values()):
        return None, "표본에 없는 id 이거나 값이 0·1·2 가 아닙니다"
    if len(labels) < MIN_LABELS:
        return None, f"{MIN_LABELS}건 이상 필요합니다 (현재 {len(labels)}건)"
    return {k: int(v) for k, v in labels.items()}, None


def make_handler(server_ref: dict):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")   # file:// 에서 연 위젯이 응답을 읽게 한다
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_POST(self):
            if self.path != "/labels":
                return self._reply(404, {"ok": False, "error": "없는 경로"})
            try:
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8"))
                labels, error = validate(payload, json.loads(CANDIDATES.read_text(encoding="utf-8")))
            except (ValueError, KeyError) as exc:
                return self._reply(400, {"ok": False, "error": f"형식 오류: {type(exc).__name__}"})
            if error:
                return self._reply(400, {"ok": False, "error": error})
            OUT.write_text(json.dumps({"snapshot": payload["snapshot"], "labeled_via": "widget(127.0.0.1 수신기)",
                                       "received_at_kst": datetime.now(KST).isoformat(timespec="seconds"),
                                       "count": len(labels), "labels": labels}, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            self._reply(200, {"ok": True, "count": len(labels)})
            print(f"수신 완료: {len(labels)}건 → {OUT.name}", flush=True)
            threading.Thread(target=server_ref["server"].shutdown, daemon=True).start()   # 한 번 받으면 닫는다

        def log_message(self, *args):       # 요청 로그는 남기지 않는다
            pass

    return Handler


def main() -> int:
    ref: dict = {}
    server = HTTPServer(("127.0.0.1", PORT), make_handler(ref))
    ref["server"] = server
    timer = threading.Timer(WAIT_SECONDS, server.shutdown)          # 오래 방치돼도 스스로 닫힌다
    timer.daemon = True                                             # 수신을 마치면 타이머가 프로세스를 붙잡지 않게 한다
    timer.start()
    print(f"대기 중: http://127.0.0.1:{PORT}/labels (최대 {WAIT_SECONDS // 60}분)", flush=True)
    server.serve_forever()
    return 0 if OUT.exists() else 1


if __name__ == "__main__":
    sys.exit(main())
