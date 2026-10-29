"""파이프라인 실행 스크립트.

  python run.py                 수집→선별→요약→검수→발행 전체를 돌리되, 발송 없이 내용만 확인한다(dry-run)
  python run.py --show-prepared RUN_ID   저장한 실제 발송 내용을 다시 확인한다 (모델 호출 없음)
  python run.py --send-prepared RUN_ID --confirm-hash SHA256   확인한 준비본만 발송한다
  python run.py --hours 24      수집 시간 창(시간)을 바꾼다

종료 코드: 0 정상 · 1 실행 오류 · 2 발송·재조회 실패 또는 잘못된 인자
발송 여부를 기본값에 숨기지 않고 명령어로 드러낸다(강의 기본 코드는 DRY_RUN 환경변수 기본값이 '발송 안 함'이라
예약 실행에서도 조용히 아무것도 보내지 않을 수 있었다).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import load_settings
from graph import run_pipeline
from llm import LLMError
from metrics import Metrics
from publisher import DiscordPublisher, build_embeds
from 발행승인 import load_publication, prepare_publication, publish_prepared


def preview(verified: list[dict]) -> str:
    if not verified:
        return "  (검수를 통과한 기사가 없습니다)"
    blocks = []
    for i, a in enumerate(verified, 1):
        blocks.append(f"  {i}. {a['headline']}  [{a['source']}]\n     {a['summary']}\n     💡 {a['why']}\n     {a['url']}")
    return "\n".join(blocks)


def prepare_result(settings: dict, final: dict) -> dict:
    """한 번 생성·검수한 결과로 보낼 내용을 확정한다. 이후에는 모델을 부르지 않는다."""
    now = datetime.now(timezone.utc)
    stats = {key: len(final.get(key, [])) for key in ("collected", "shortlisted", "picked", "verified")}
    pcfg = settings["cfg"]["publish"]
    label = now.astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
    embeds, trimmed = build_embeds(label, pcfg["title"], final["verified"], stats,
                                   total_max=pcfg["total_chars_max"], now=now)
    store = Path(settings["root"]) / settings["cfg"]["run"]["store_dir"]
    manifest = prepare_publication(store, final["run_id"], embeds,
                                   metadata={"counts": stats, "trimmed": trimmed})
    Metrics(store / "metrics.jsonl", final["run_id"]).emit(
        "publication_prepared", sha256=manifest["sha256"], counts=stats, trimmed=trimmed)
    return manifest


def publication_preview(manifest: dict) -> str:
    blocks = []
    for embed in manifest["payload"]["embeds"]:
        lines = [embed.get("title", ""), embed.get("description", ""), embed.get("url", ""),
                 embed.get("footer", {}).get("text", "")]
        blocks.append("\n".join(line for line in lines if line))
    return "\n\n".join(blocks)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")        # Windows 콘솔(cp949)에서도 한글·이모지가 깨지지 않게
    parser = argparse.ArgumentParser(description="나만의 뉴스레터 에이전트")
    parser.add_argument("--publish", action="store_true", help="이전 즉시 발송 명령 (사용 중지)")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--send-prepared", metavar="RUN_ID", help="승인한 불변 준비본만 발송 (모델 호출 없음)")
    action.add_argument("--show-prepared", metavar="RUN_ID", help="저장된 발송 내용과 해시 확인 (모델 호출 없음)")
    parser.add_argument("--confirm-hash", metavar="SHA256", help="내용을 확인하고 승인한 준비본의 SHA256")
    parser.add_argument("--hours", type=int, help="수집 시간 창(시간). 기본은 config.yaml 의 collect.hours")
    args = parser.parse_args()
    if args.publish:
        parser.error("--publish 즉시 발송은 중지했습니다. 준비본을 확인한 뒤 --send-prepared 와 --confirm-hash 를 사용하세요.")
    if args.send_prepared and not args.confirm_hash:
        parser.error("발송에는 내용을 확인한 준비본의 --confirm-hash 가 필요합니다.")
    if args.confirm_hash and not args.send_prepared:
        parser.error("--confirm-hash 는 --send-prepared 와 함께 사용해야 합니다.")
    if args.hours is not None and (args.send_prepared or args.show_prepared):
        parser.error("저장된 준비본은 --hours 로 다시 생성하지 않습니다.")

    try:
        settings = load_settings()
        store = Path(settings["root"]) / settings["cfg"]["run"]["store_dir"]
        if args.show_prepared:
            manifest = load_publication(store, args.show_prepared)
            print(publication_preview(manifest))
            print(f"준비본 SHA256: {manifest['sha256']}")
            return 0
        if args.send_prepared:
            # 이 분기는 수집·생성 경로로 돌아가지 않는다.
            manifest = load_publication(store, args.send_prepared)
            publisher = DiscordPublisher.from_env(settings["cfg"]["publish"], live=True)
            result = publish_prepared(store, args.send_prepared, args.confirm_hash, publisher)
            Metrics(store / "metrics.jsonl", args.send_prepared).emit(
                "publication_sent", sha256=manifest["sha256"], **result)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result.get("status") == "sent" and result.get("readback", {}).get("status") == "verified" else 2
        final = run_pipeline(settings, publish=False, hours=args.hours)
        manifest = prepare_result(settings, final)
    except LLMError as exc:
        print(f"[오류] {exc}", file=sys.stderr)
        return 1
    except Exception as exc:                            # 기록(metrics)에는 run_end 오류로 이미 남았다
        print(f"[오류] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    for line in final["log"]:
        print(line)
    print("\n── 발행 내용 ──")
    print(publication_preview(manifest))
    if final["rejected"]:
        print("\n── 스킵한 기사 ──")
        for r in final["rejected"]:
            print(f"  - [{r['source']}] {r['title'][:50]} → {r['reason'][:80]}")
    status = final["published"]["status"]
    print(f"\n실행 {final['run_id']} · 발행 상태: {status}")
    print(f"준비본 SHA256: {manifest['sha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
