"""검수 프롬프트의 사실·해석 구분과 실패 시 차단을 검사한다.

이 검사는 모델 판단의 정확도를 증명하지 않는다. 실제 저장 초안의 재검수와
정상·오염 샘플 실호출 결과를 별도로 비교한다.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from graph import Pipeline, Verdict, Problem
from metrics import Metrics
from tests.support import FakeLLM, make_settings


DRAFT = {"article_id": "review-case", "attempt": 0, "headline": "새 도구 공개",
         "summary": "새 도구를 공개했습니다. 작업을 지원합니다. 사용법을 소개합니다.",
         "why": "개발자가 실습에 참고할 수 있습니다.",
         "body": "The new tool was released with a tutorial and support for development tasks."}


class ReviewCriteriaTests(unittest.TestCase):
    def pipeline(self, handler, root):
        return Pipeline(make_settings(), llm=FakeLLM(handler), http=None, robots=None,
                        metrics=Metrics(Path(root) / "metrics.jsonl", "criteria-test"), publisher=None)

    def test_prompt_separates_fact_checks_and_reader_interpretation(self):
        seen = []
        def handler(stage, system, user, schema):
            seen.append((system, user))
            return Verdict(ok=True, problems=[])
        with tempfile.TemporaryDirectory() as tmp:
            self.pipeline(handler, tmp).check_draft(DRAFT)
        self.assertEqual(len(seen), 1)
        self.assertIn("[원문 사실]", seen[0][0])
        self.assertIn("[독자 해석]", seen[0][0])
        self.assertIn("의미가 같은 번역", seen[0][0])
        self.assertIn("number_mismatch", seen[0][0])
        self.assertIn(DRAFT["body"], seen[0][1])

    def test_negative_model_verdict_still_blocks_publication(self):
        def handler(*args):
            return Verdict(ok=False, problems=[Problem(claim="근거 없는 주장", kind="unsupported", note="새 사실을 추가함")])
        with tempfile.TemporaryDirectory() as tmp:
            result = self.pipeline(handler, tmp).check_draft(DRAFT)
        self.assertFalse(result["ok"])
        self.assertFalse(result["judge_ok"])
        self.assertEqual(result["problems"][0]["kind"], "unsupported")

    def test_control_characters_and_unrendered_tex_are_rejected_before_model(self):
        for artifact in (chr(9), chr(92) + "textit{method}", chr(92) + "method"):
            with self.subTest(artifact=repr(artifact)), tempfile.TemporaryDirectory() as tmp:
                called = []
                def handler(*args):
                    called.append(True)
                    return Verdict(ok=True, problems=[])
                draft = {**DRAFT, "summary": DRAFT["summary"] + " " + artifact}
                result = self.pipeline(handler, tmp).check_draft(draft)
                self.assertFalse(result["ok"])
                self.assertTrue(result["rule_problems"])
                self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
