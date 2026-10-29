"""실행 기록 — store/metrics.jsonl 에 한 줄(JSON)씩 이벤트를 이어 붙인다.

각 줄은 run_id·ts(UTC)·stage 와 단계별 필드를 가진다.
선별 근거(예선·본선 점수와 이유), 검수 결과, 발행 응답을 나중에 다시 열어 확인하려는 용도다.
기사 본문 전문은 저작권과 용량 때문에 여기에 쓰지 않고 길이만 남긴다.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path


class Metrics:
    def __init__(self, path: Path, run_id: str) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self._lock = threading.Lock()          # 병렬 워커가 동시에 쓰므로 줄이 섞이지 않게 잠근다
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, stage: str, **fields) -> dict:
        record = {"run_id": self.run_id, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "stage": stage, **fields}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return record
