"""검수 시험(오염 샘플, negative control) — 정상 초안의 문장 하나를 일부러 틀리게 바꿔 검수가 잡아내는지 잰다.

검수 불합격이 계속 0건이면 '문제가 없어서'인지 '검사가 못 잡아서'인지 알 수 없다. 그래서 정답을 아는 오류를
직접 넣어 본다. 변형은 규칙(코드)으로 만들며, 문장 수를 그대로 둬 형식 검사가 아니라 '원문 대조'를 시험한다.

  kind        변형 내용
  number      요약의 첫 숫자를 원문에 없는 다른 숫자로 바꿈
  fabricate   둘째 문장을 원문에 없는 벤치마크 주장으로 바꿈
  wrong_entity 셋째 문장을 원문에 없는 회사가 주도했다는 주장으로 바꿈
  overstate   첫 문장 앞에 원문이 뒷받침하지 않는 '업계 최초로' 같은 과장을 붙임
  control     변형 없는 정상 초안(오탐 확인: 정상인데 불합격하면 오탐)

  python tools/negative_control.py --limit 5      # 실제 모델 호출 (초안당 5회 = 정상 1 + 변형 4)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from uuid import uuid4
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from graph import unmatched_numbers  # noqa: E402

KINDS = ("number", "fabricate", "wrong_entity", "overstate")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?。])\s+")
NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
FABRICATED_NUMBERS = ("38", "47", "53", "61")
COMPANIES = (("엔비디아", ("nvidia",)), ("구글", ("google", "구글")), ("애플", ("apple", "애플")), ("메타", ("meta ", "메타")))
OUT = ROOT / "eval" / "negative_control.json"


def sentences(text: str) -> list[str]:
    return [s for s in SENTENCE_SPLIT.split(text.strip()) if s]


def mutate(draft: dict, body: str, kind: str) -> dict | None:
    """정상 초안에서 문장 하나를 바꿔 '정답을 아는 오류'를 만든다. 만들 수 없으면 None."""
    sents = sentences(draft["summary"])
    if len(sents) < 3:
        return None
    low = body.lower()
    if kind == "number":
        for i, s in enumerate(sents):
            m = NUMBER_RE.search(s)
            if m:
                original = float(m.group(0).replace(",", ""))
                for candidate in (original * 3 + 7, original + 17, original * 11):
                    text = str(int(candidate)) if candidate == int(candidate) else f"{candidate:.1f}"
                    if text not in body.replace(",", ""):
                        sents[i] = s.replace(m.group(0), text, 1)
                        break
                else:
                    return None
                break
        else:
            return None
    elif kind == "fabricate":
        number = next((n for n in FABRICATED_NUMBERS if n not in body), None)
        if number is None:
            return None
        sents[1] = f"공식 벤치마크에서 기존 대비 {number}% 빠른 처리 속도를 기록했다고 밝혔습니다."
    elif kind == "wrong_entity":
        company = next((name for name, keys in COMPANIES if not any(k in low for k in keys)), None)
        if company is None:
            return None
        sents[2] = f"이 프로젝트는 {company}가 주도해 개발했습니다."
    elif kind == "overstate":
        prefix = "업계 최초로 " if ("first" not in low and "최초" not in body) else "압도적으로 "
        sents[0] = prefix + sents[0]
    else:
        raise ValueError(kind)
    return {**draft, "summary": " ".join(sents), "mutation": kind}


def run_control(drafts: list[dict], check, kinds=KINDS) -> list[dict]:
    """check(draft) → 검수 결과 dict(ok·rule_problems·judge_ok·error). 초안마다 정상 1건과 변형을 검수한다."""
    rows = []
    for d in drafts:
        base = {"article_id": d["article_id"]}
        control = check(d)
        rows.append({**base, "kind": "control", "ok": control["ok"], "error": control.get("error"),
                     "false_positive": None if control.get("error") else not control["ok"]})
        for kind in kinds:
            mutated = mutate(d, d["body"], kind)
            if mutated is None:
                rows.append({**base, "kind": kind, "skipped": True})
                continue
            result = check(mutated)
            by = ("error" if result["error"] else "rule" if result["rule_problems"]
                  else "judge" if not result["ok"] else "missed")
            rows.append({**base, "kind": kind, "caught": None if result["error"] else not result["ok"],
                         "error": result.get("error"), "by": by,
                         "code_number_flag": bool(unmatched_numbers(mutated["summary"], d["body"])) if kind == "number" else None,
                         "mutated_summary": mutated["summary"][:300], "judge_problems": result["problems"]})
    return rows


def summarize(rows: list[dict]) -> dict:
    out = {}
    for kind in KINDS:
        tried = [r for r in rows if r["kind"] == kind and not r.get("skipped")]
        out[kind] = {"tried": len(tried), "evaluated": sum(1 for r in tried if not r.get("error") and r["by"] != "error"),
                     "caught": sum(1 for r in tried if r["caught"] and not r.get("error") and r["by"] != "error"),
                     "by_judge": sum(1 for r in tried if r["by"] == "judge"),
                     "by_rule": sum(1 for r in tried if r["by"] == "rule"),
                     "errors": sum(1 for r in tried if r["by"] == "error"),
                     "skipped": sum(1 for r in rows if r["kind"] == kind and r.get("skipped"))}
    controls = [r for r in rows if r["kind"] == "control"]
    out["control"] = {"tried": len(controls), "evaluated": sum(1 for r in controls if not r.get("error")),
                      "errors": sum(1 for r in controls if r.get("error")),
                      "false_positives": sum(1 for r in controls if r["false_positive"] and not r.get("error"))}
    flagged = [r for r in rows if r["kind"] == "number" and not r.get("skipped")]
    out["number_flag_by_code"] = {"tried": len(flagged), "flagged": sum(1 for r in flagged if r["code_number_flag"])}
    return out


def latest_cache() -> Path | None:
    caches = sorted((ROOT / "store" / "cache").glob("*.json"), key=lambda p: p.stat().st_mtime)
    return caches[-1] if caches else None


def main() -> int:
    parser = argparse.ArgumentParser(description="검수 시험 (실제 모델 호출)")
    parser.add_argument("--run", help="store/cache/<run_id>.json 의 run_id (기본: 가장 최근)")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    path = (ROOT / "store" / "cache" / f"{args.run}.json") if args.run else latest_cache()
    if not path or not path.exists():
        print("store/cache/ 에 실행 캐시가 없습니다. 먼저 python run.py 를 실제 실행해 검수를 통과한 초안을 만드세요.", file=sys.stderr)
        return 1
    cache = json.loads(path.read_text(encoding="utf-8"))
    passed = {(v["article_id"], v["attempt"]) for v in cache["verified"]}
    drafts = [d for d in cache["drafted"] if (d["article_id"], d["attempt"]) in passed][:args.limit]
    if not drafts:
        print("검수를 통과한 초안이 없어 시험할 수 없습니다.", file=sys.stderr)
        return 1

    from config import load_settings
    from graph import Pipeline
    from llm import LLM
    from metrics import Metrics

    settings = load_settings()
    run_id = "negative_control-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex
    out = OUT.with_name(run_id + ".json")
    metrics = Metrics(ROOT / settings["cfg"]["run"]["store_dir"] / "metrics.jsonl", run_id)
    metrics.emit("run_start", mode="negative_control", source_cache=path.name, drafts=len(drafts),
                 models=settings["cfg"]["llm"]["models"])
    llm = LLM(settings["cfg"]["llm"], metrics)
    pipe = Pipeline(settings, llm=llm, http=None, robots=None, metrics=metrics, publisher=None)
    rows = run_control(drafts, pipe.check_draft)
    summary = summarize(rows)
    payload = {"run_id": run_id, "source_cache": path.name, "drafts": len(drafts), "llm_calls": llm.calls,
               "summary": summary, "rows": rows,
               "caveat": "변형 4종·초안 소수의 시험이다. 잡았다고 모든 환각을 잡는다는 뜻은 아니고, 놓쳤다면 그 유형은 검수가 약하다는 신호다."}
    with out.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
    metrics.emit("negative_control", summary=summary, llm_calls=llm.calls)
    for kind in (*KINDS, "control"):
        print(f"{kind:<13}{summary[kind]}")
    print(f"숫자 대조 신호(코드): {summary['number_flag_by_code']}\n모델 호출 {llm.calls}회 · 저장: {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
