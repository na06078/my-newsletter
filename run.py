"""파이프라인 실행 스크립트.

  python run.py                 수집→선별→요약→검수→발행까지 사람 개입 없이 끝낸다(Discord 발송·메시지 재조회 포함)
  python run.py --dry-run       보내지 않고 준비본(보낼 내용)까지만 만든다 — 시험·내용 확인용
  python run.py --show-prepared RUN_ID   저장된 준비본을 다시 본다 (읽기 전용, 모델 호출 없음)
  python run.py --hours 24      수집 시간 창(시간)을 바꾼다

종료 코드: 0 정상 · 1 실행 오류 · 2 발송 실패·재조회 불일치 또는 잘못된 인자
뉴스 에이전트는 실행하면 발행까지 끝내는 것이 기본이다. 보내지 않는 쪽(--dry-run)만 옵션으로 둔다.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from config import load_settings
from graph import run_pipeline
from llm import LLMError
from 발행승인 import load_publication


def publication_preview(manifest: dict) -> str:
    blocks = []
    for embed in manifest["payload"]["embeds"]:
        lines = [embed.get("title", ""), embed.get("description", ""), embed.get("url", ""),
                 embed.get("footer", {}).get("text", "")]
        blocks.append("\n".join(line for line in lines if line))
    return "\n\n".join(blocks)


def delivered(published: dict) -> bool:
    """실제로 보냈고, 보낸 메시지를 다시 조회해 준비본과 일치함을 확인했는가."""
    return (published.get("status") == "sent" and published.get("readback", {}).get("status") == "verified"
            and not published.get("recording_error"))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")        # Windows 콘솔(cp949)에서도 한글·이모지가 깨지지 않게
    parser = argparse.ArgumentParser(description="나만의 뉴스레터 에이전트")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--dry-run", action="store_true", help="발송하지 않고 준비본까지만 만든다")
    action.add_argument("--show-prepared", metavar="RUN_ID", help="저장된 준비본 내용과 해시 확인 (모델 호출 없음)")
    parser.add_argument("--hours", type=int, help="수집 시간 창(시간). 기본은 config.yaml 의 collect.hours")
    args = parser.parse_args()
    if args.hours is not None and args.hours <= 0:
        parser.error("--hours 는 0보다 큰 정수여야 합니다.")
    if args.hours is not None and args.show_prepared:
        parser.error("저장된 준비본은 --hours 로 다시 생성하지 않습니다.")

    try:
        settings = load_settings()
        store = Path(settings["root"]) / settings["cfg"]["run"]["store_dir"]
        if args.show_prepared:
            manifest = load_publication(store, args.show_prepared)
            print(publication_preview(manifest))
            print(f"준비본 SHA256: {manifest['sha256']}")
            return 0
        final = run_pipeline(settings, publish=not args.dry_run, hours=args.hours)
        manifest = load_publication(store, final["run_id"])     # 실제로 저장·발송한 내용을 다시 읽어 보여 준다
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
    published = final["published"]
    print(f"\n실행 {final['run_id']} · 발행 상태: {published['status']}")
    print(f"준비본 SHA256: {manifest['sha256']}")
    if published.get("recording_error"):
        print(f"[오류] 전송 결과 기록 실패: {published['recording_error']}. 같은 내용을 자동 재전송하지 않습니다.")
    if final.get("cache_status", {}).get("status") == "failed":
        print("[경고] 검수 시험용 캐시 저장 실패: cache_write_failed. 알려진 발행 결과는 유지됩니다.")
    for error in final.get("cleanup_errors", []):
        print(f"[경고] 연결 정리 실패: {error['resource']}/{error['error']}")
    if args.dry_run:
        return 0
    if published.get("message_id"):
        print(f"Discord 메시지: {published['message_id']} · 재조회: {published.get('readback', {}).get('status')}")
    return 0 if delivered(published) else 2


if __name__ == "__main__":
    sys.exit(main())
