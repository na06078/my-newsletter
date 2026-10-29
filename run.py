"""파이프라인 실행 스크립트.

  python run.py                 수집→선별→요약→검수→발행 전체를 돌리되, 발송 없이 내용만 확인한다(dry-run)
  python run.py --publish       검수를 통과한 내용을 Discord 웹훅으로 실제 발송한다
  python run.py --hours 24      수집 시간 창(시간)을 바꾼다

종료 코드: 0 정상 · 1 실행 오류 · 2 발송 실패(--publish 일 때)
발송 여부를 기본값에 숨기지 않고 명령어로 드러낸다(강의 기본 코드는 DRY_RUN 환경변수 기본값이 '발송 안 함'이라
예약 실행에서도 조용히 아무것도 보내지 않을 수 있었다).
"""
from __future__ import annotations

import argparse
import sys

from config import load_settings
from graph import run_pipeline
from llm import LLMError


def preview(verified: list[dict]) -> str:
    if not verified:
        return "  (검수를 통과한 기사가 없습니다)"
    blocks = []
    for i, a in enumerate(verified, 1):
        blocks.append(f"  {i}. {a['headline']}  [{a['source']}]\n     {a['summary']}\n     💡 {a['why']}\n     {a['url']}")
    return "\n".join(blocks)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")        # Windows 콘솔(cp949)에서도 한글·이모지가 깨지지 않게
    parser = argparse.ArgumentParser(description="나만의 뉴스레터 에이전트")
    parser.add_argument("--publish", action="store_true", help="Discord 로 실제 발송 (기본: dry-run)")
    parser.add_argument("--hours", type=int, help="수집 시간 창(시간). 기본은 config.yaml 의 collect.hours")
    args = parser.parse_args()

    try:
        final = run_pipeline(load_settings(), publish=args.publish, hours=args.hours)
    except LLMError as exc:
        print(f"[오류] {exc}", file=sys.stderr)
        return 1
    except Exception as exc:                            # 기록(metrics)에는 run_end 오류로 이미 남았다
        print(f"[오류] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    for line in final["log"]:
        print(line)
    print("\n── 발행 내용 ──")
    print(preview(final["verified"]))
    if final["rejected"]:
        print("\n── 스킵한 기사 ──")
        for r in final["rejected"]:
            print(f"  - [{r['source']}] {r['title'][:50]} → {r['reason'][:80]}")
    status = final["published"]["status"]
    print(f"\n실행 {final['run_id']} · 발행 상태: {status}")
    return 2 if args.publish and status != "sent" else 0


if __name__ == "__main__":
    sys.exit(main())
