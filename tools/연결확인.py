"""구조화 응답 한 번으로 연결을 확인한다. 인증값은 반환·출력하지 않는다."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import load_settings
from llm import LLM
from metrics import Metrics


class ConnectionReply(BaseModel):
    marker: Literal["newsletter-connection-ok"]


def check_connection(settings, *, llm_factory=None):
    store = Path(settings["root"]) / settings["cfg"]["run"]["store_dir"]
    run_id = "connection-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    metrics = Metrics(store / "metrics.jsonl", run_id)
    cfg = {**settings["cfg"]["llm"], "max_retries": 0, "temperature": None}
    result = {"run_id": run_id, "status": "error", "requested_model": cfg["models"]["select"],
              "observed_model": None, "budget_before": None, "budget_after": None}
    llm = None
    metrics.emit("run_start", mode="connection_check", request_plan=1, model=cfg["models"]["select"])
    try:
        llm = (llm_factory or LLM)(cfg, metrics)
        result["budget_before"] = llm.budget.snapshot()
        reply = llm.parse("select_connection", "정해진 스키마로 연결 확인 표시만 답하세요.",
                          "marker 를 newsletter-connection-ok 로 답하세요.", ConnectionReply)
        if reply.marker != "newsletter-connection-ok":
            raise ValueError("invalid_connection_marker")
        result["status"] = "ok"
    except Exception as exc:
        result["error"] = type(exc).__name__
        # 제공자 오류 원문은 저장하지 않는다. 알려진 오류 분류명만 허용한다.
        safe_kinds = {"AuthenticationError", "PermissionDeniedError", "RateLimitError", "APIConnectionError",
                      "APITimeoutError", "BadRequestError", "NotFoundError", "InternalServerError"}
        if str(exc) in safe_kinds:
            result["provider_error"] = str(exc)
    finally:
        if llm is not None:
            try:
                result["budget_after"] = llm.budget.snapshot()
            finally:
                close = getattr(getattr(llm, "client", None), "close", None)
                if close:
                    close()
    events = [json.loads(line) for line in metrics.path.read_text(encoding="utf-8").splitlines() if line.strip()]
    calls = [row for row in events if row.get("run_id") == run_id and row.get("stage") == "llm_call"]
    if calls:
        result["observed_model"] = calls[-1].get("model")
    metrics.emit("connection_check", **{k: v for k, v in result.items() if k != "run_id"})
    (store / "연결확인.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    result = check_connection(load_settings())
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
