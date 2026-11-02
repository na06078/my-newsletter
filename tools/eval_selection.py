"""선별 노드 평가 — 사용자 정답 라벨(eval/labels.json)과 선별 결과를 견준다.

같은 표본 30건을 순서만 바꿔 여러 번(기본 3) 돌려, 기준이 의도대로 동작하는지와 위치 편향·불안정성을 함께 본다.
지표
  - precision        : 최종 선별 건 중 사용자가 '중요'로 라벨한 비율
  - max_precision    : '중요' 라벨이 선별 건수보다 적으면 만점도 1.0 이 아니므로 그 상한
  - random_baseline  : 아무렇게나 뽑았을 때 기대 precision (표본 안의 '중요' 비율)
  - survivor_recall  : '중요' 라벨 전체 중 예선을 통과한 비율 (예선에서 좋은 글을 잃지 않았는가)
표본이 30건이라 통계적 입증이 아니라 '동작 확인'이다. 같은 표본으로 기준 문장을 고치면 그 표본에는 맞춰진 것이므로 따로 기록한다.

  python tools/eval_selection.py --repeats 3      # 실제 모델 호출(예선 2회+본선 1회 = 반복당 3회)
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

KST = timezone(timedelta(hours=9))
LABEL_NAME = {2: "중요", 1: "보통", 0: "무관"}
CANDIDATES = ROOT / "eval" / "candidates.json"
LABELS = ROOT / "eval" / "labels.json"
OUT = ROOT / "eval" / "selection_eval.json"


def _mean(values):
    values = [v for v in values if v is not None]
    return round(statistics.mean(values), 3) if values else None


def evaluate_run(items: list[dict], labels: dict, survivors: list[dict], picked: list[dict]) -> dict:
    """선별 한 번의 결과(예선 통과분·최종 선별)를 사용자 라벨과 견준다."""
    important = {i["id"] for i in items if labels.get(i["id"]) == 2}
    survivor_ids = {s["id"] for s in survivors}
    picked_ids = {p["id"] for p in picked}
    pick_labels = [labels.get(p["id"]) for p in picked]
    n = len(picked)
    outcome = lambda i: ("picked" if i in picked_ids else "survivor_not_picked" if i in survivor_ids else "dropped_at_prelim")
    return {
        "n_picked": n,
        "precision": round(sum(1 for l in pick_labels if l == 2) / n, 3) if n else None,
        "max_precision": round(min(len(important), n) / n, 3) if n else None,
        "random_baseline": round(len(important) / len(items), 3) if items else None,
        "mean_label_picked": _mean(pick_labels), "mean_label_pool": _mean(labels.get(i["id"]) for i in items),
        "irrelevant_picked": sum(1 for l in pick_labels if l == 0),
        "survivor_recall": round(len(important & survivor_ids) / len(important), 3) if important else None,
        "picked": [{"id": p["id"], "title": p["title"][:70], "label": LABEL_NAME.get(labels.get(p["id"])),
                    "score": p["final"]["score"], "reason": p["final"]["reason"]} for p in picked],
        "important_outcomes": [{"id": i["id"], "title": i["title"][:70], "outcome": outcome(i["id"])}
                               for i in items if i["id"] in important],
    }


def summarize(runs: list[dict]) -> dict:
    keys = ("precision", "max_precision", "random_baseline", "mean_label_picked", "mean_label_pool",
            "irrelevant_picked", "survivor_recall")
    out = {k: _mean(r[k] for r in runs) for k in keys}
    sets = [frozenset(p["id"] for p in r["picked"]) for r in runs]
    out["pick_overlap_all_runs"] = len(frozenset.intersection(*sets)) if sets else 0     # 순서를 바꿔도 늘 뽑힌 건수
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="선별 노드 평가 (실제 모델 호출)")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if not (CANDIDATES.exists() and LABELS.exists()):
        print("eval/candidates.json 과 eval/labels.json 이 필요합니다.", file=sys.stderr)
        return 1
    from tools.label_receiver import validate, unique_object
    try:
        cand = json.loads(CANDIDATES.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
        label_doc = json.loads(LABELS.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    except (ValueError, OSError):
        print("표본 또는 라벨 JSON 형식이 잘못되었습니다", file=sys.stderr)
        return 1
    labels, error = validate(label_doc, cand)
    if error:
        print(error, file=sys.stderr)
        return 1
    items = cand["items"]
    items = [i for i in items if i["id"] in labels]

    from config import load_settings
    from graph import Pipeline
    from llm import LLM
    from metrics import Metrics

    settings = load_settings()
    run_id = "selection_eval-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex
    out = OUT.with_name(run_id + ".json")
    metrics = Metrics(ROOT / settings["cfg"]["run"]["store_dir"] / "metrics.jsonl", run_id)
    metrics.emit("run_start", mode="eval_selection", items=len(items), repeats=args.repeats,
                 importance=settings["audience"]["importance"].strip(),
                 audience_status=settings["audience"].get("status"), models=settings["cfg"]["llm"]["models"])
    llm = LLM(settings["cfg"]["llm"], metrics)
    pipe = Pipeline(settings, llm=llm, http=None, robots=None, metrics=metrics, publisher=None)

    runs = []
    for k in range(args.repeats):
        shuffled = items[:]
        random.Random(f"eval-{k}").shuffle(shuffled)              # 순서만 바꿔 위치 편향을 본다
        state = pipe.select({"collected": shuffled, "run_id": f"{run_id}-{k}"})
        result = evaluate_run(items, labels, state["shortlisted"], state["picked"])
        runs.append(result)
        print(f"반복 {k + 1}: 정밀도 {result['precision']} (상한 {result['max_precision']}, 무작위 {result['random_baseline']})"
              f" · 무관 {result['irrelevant_picked']}건 · 예선 재현율 {result['survivor_recall']}")
    summary = summarize(runs)
    payload = {"snapshot": cand["snapshot_at_kst"], "evaluated_at_kst": datetime.now(KST).isoformat(timespec="seconds"),
               "run_id": run_id, "items": len(items), "label_counts": {LABEL_NAME[v]: sum(1 for x in labels.values() if x == v) for v in (2, 1, 0)},
               "importance": settings["audience"]["importance"].strip(), "audience_status": settings["audience"].get("status"),
               "llm_calls": llm.calls, "summary": summary, "runs": runs,
               "caveat": "표본 30건의 동작 확인이며 통계적 입증이 아니다. 같은 표본에서 기준을 고쳤다면 그 표본에 맞춰진 결과다."}
    with out.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
    metrics.emit("eval_selection", **{"summary": summary, "label_counts": payload["label_counts"], "llm_calls": llm.calls})
    print("\n요약:", summary, f"\n모델 호출 {llm.calls}회 · 저장: {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
