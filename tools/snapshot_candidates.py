"""라벨링용 표본 만들기 — 실제 소스에서 후보 기사를 모아(모델 호출 없음) 30건을 저장한다.

표본은 소스별로 골고루 섞은 뒤 고정 시드로 뽑는다. 사용자가 '모델이 무엇을 골랐는지' 모르는 채로
정답 라벨을 달 수 있게(blind) 하고, 같은 입력으로 선별 노드를 다시 돌려 비교하려는 용도다.

  python tools/snapshot_candidates.py            # 읽기 전용 HTTP GET 으로 수집 → eval/candidates.json
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import collectors                      # noqa: E402
from config import load_settings       # noqa: E402
from net import Http                   # noqa: E402
from 불변저장 import write_bytes_once  # noqa: E402

SEED = "label-2026-10-29"
SAMPLE_SIZE = 30
OUT = ROOT / "eval" / "candidates.json"
KST = timezone(timedelta(hours=9))
KEEP_FIELDS = ("id", "title", "url", "source", "source_id", "route", "tier", "lang", "published_at", "summary")


def stratified_sample(items: list[dict], n: int, seed: str) -> list[dict]:
    """소스를 번갈아 가며 뽑아 한 소스가 표본을 독차지하지 않게 한다. 마지막에 순서를 섞는다."""
    rng = random.Random(seed)
    by_source: dict[str, list[dict]] = {}
    for it in items:
        by_source.setdefault(it["source_id"], []).append(it)
    for lst in by_source.values():
        rng.shuffle(lst)
    picked: list[dict] = []
    while len(picked) < n and any(by_source.values()):
        for sid in sorted(by_source):
            if by_source[sid] and len(picked) < n:
                picked.append(by_source[sid].pop())
    rng.shuffle(picked)
    return picked


def main() -> None:
    parser = argparse.ArgumentParser(description="라벨링 표본 수집")
    parser.add_argument("--out", type=Path, default=OUT)
    out = parser.parse_args().out
    if out.exists():
        raise FileExistsError("기존 표본을 보존합니다. --out으로 새 경로를 지정하세요.")
    settings = load_settings()
    cfg = settings["cfg"]["collect"]
    now = datetime.now(timezone.utc)
    http = Http(cfg["user_agent"], cfg["request_timeout_s"])
    try:
        pool, stats = collectors.collect(cfg, http, now, cfg["hours"])
    finally:
        close = getattr(http, "close", None)
        if callable(close):
            close()
    sample = stratified_sample(pool, SAMPLE_SIZE, SEED)

    payload = {
        "snapshot_at_kst": now.astimezone(KST).isoformat(timespec="seconds"),
        "hours": cfg["hours"], "seed": SEED, "pool_size": len(pool), "http_requests": http.count,
        "pool_by_source": dict(Counter(it["source_id"] for it in pool)),
        "sample_by_source": dict(Counter(it["source_id"] for it in sample)),
        "source_stats": {k: v for k, v in stats.items() if not k.startswith("_")},
        "items": [{k: it[k] for k in KEEP_FIELDS} for it in sample],
    }
    write_bytes_once(out, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    print(f"요청 {http.count}건 · 후보 풀 {len(pool)}건 → 표본 {len(sample)}건 · 저장: {out}")
    print("풀   :", payload["pool_by_source"])
    print("표본 :", payload["sample_by_source"])
    print("소스 오류:", {k: v["error"] for k, v in payload["source_stats"].items() if v["error"]} or "없음")


if __name__ == "__main__":
    main()
