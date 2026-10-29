"""평가 도구(선별 평가·검수 시험)의 계산 로직을 모델 없이 확인한다."""
from __future__ import annotations

import unittest

from tools import eval_selection as ev
from tools import negative_control as nc

BODY = ("The new runtime reaches 120 tokens per second on a single GPU and supports 4-bit quantization. "
        "It was released by the llama.cpp community and ships with Vulkan support.")
DRAFT = {"article_id": "a1", "attempt": 0, "headline": "새 런타임 공개",
         "summary": "새 런타임이 단일 GPU에서 초당 120 토큰을 처리합니다. 4비트 양자화를 지원합니다. llama.cpp 커뮤니티가 공개했습니다.",
         "why": "내 환경에서 바로 시험해 볼 수 있습니다.", "body": BODY}


class MutationTests(unittest.TestCase):
    def test_each_mutation_keeps_format_but_breaks_a_fact(self):
        for kind in nc.KINDS:
            m = nc.mutate(DRAFT, BODY, kind)
            self.assertIsNotNone(m, kind)
            self.assertEqual(len(nc.sentences(m["summary"])), 3, kind)          # 문장 수 유지 → 형식 검사에 안 걸림
            self.assertNotEqual(m["summary"], DRAFT["summary"], kind)
        number = nc.mutate(DRAFT, BODY, "number")["summary"]
        self.assertNotIn("120", number)
        self.assertTrue(nc.unmatched_numbers(number, BODY))                       # 코드의 숫자 대조 신호도 켜진다
        self.assertIn("엔비디아", nc.mutate(DRAFT, BODY, "wrong_entity")["summary"])
        self.assertTrue(nc.mutate(DRAFT, BODY, "overstate")["summary"].startswith("업계 최초로"))

    def test_mutation_skipped_when_not_possible(self):
        no_number = {**DRAFT, "summary": "첫 문장입니다. 둘째 문장입니다. 셋째 문장입니다."}
        self.assertIsNone(nc.mutate(no_number, BODY, "number"))
        short = {**DRAFT, "summary": "한 문장입니다. 두 문장입니다."}
        self.assertIsNone(nc.mutate(short, BODY, "fabricate"))
        self.assertEqual(nc.mutate(DRAFT, BODY + " Nvidia partnered too.", "wrong_entity")["summary"].count("엔비디아"), 0)

    def test_run_control_counts_catches_and_false_positives(self):
        def fake_check(d):                      # 변형된 초안(mutation 키가 있는 것)만 불합격시키는 이상적인 검수자 + 숫자 변형은 놓치는 검수자
            mutated = "mutation" in d
            caught = mutated and d["mutation"] != "number"
            return {"ok": not caught, "rule_problems": [], "judge_ok": not caught, "error": None, "problems": []}
        rows = nc.run_control([DRAFT], fake_check)
        s = nc.summarize(rows)
        self.assertEqual(s["control"], {"tried": 1, "false_positives": 0})
        self.assertEqual((s["fabricate"]["caught"], s["wrong_entity"]["caught"], s["overstate"]["caught"]), (1, 1, 1))
        self.assertEqual((s["number"]["tried"], s["number"]["caught"]), (1, 0))   # 놓친 것은 놓쳤다고 센다
        self.assertEqual(s["number_flag_by_code"], {"tried": 1, "flagged": 1})    # 대신 코드의 숫자 대조는 잡았다


class EvalTests(unittest.TestCase):
    def test_evaluate_run_metrics(self):
        items = [{"id": f"i{n}", "title": f"기사{n}"} for n in range(10)]
        labels = {f"i{n}": (2 if n < 3 else 1 if n < 6 else 0) for n in range(10)}      # 중요 3, 보통 3, 무관 4
        survivors = [{"id": f"i{n}"} for n in (0, 1, 3, 6)]
        picked = [{"id": f"i{n}", "title": f"기사{n}", "final": {"score": 9, "reason": "r"}} for n in (0, 3, 6)]
        r = ev.evaluate_run(items, labels, survivors, picked)
        self.assertEqual(r["precision"], round(1 / 3, 3))
        self.assertEqual(r["max_precision"], 1.0)               # 중요 라벨이 3건이라 3건 뽑으면 만점은 1.0
        self.assertEqual(r["random_baseline"], 0.3)
        self.assertEqual(r["irrelevant_picked"], 1)
        self.assertEqual(r["survivor_recall"], round(2 / 3, 3))
        outcomes = {o["id"]: o["outcome"] for o in r["important_outcomes"]}
        self.assertEqual(outcomes, {"i0": "picked", "i1": "survivor_not_picked", "i2": "dropped_at_prelim"})

    def test_summarize_overlap(self):
        run = lambda ids: {"precision": 0.4, "max_precision": 1.0, "random_baseline": 0.3, "mean_label_picked": 1.2,
                           "mean_label_pool": 1.0, "irrelevant_picked": 0, "survivor_recall": 0.5,
                           "picked": [{"id": i} for i in ids]}
        s = ev.summarize([run("abc"), run("abd"), run("abe")])
        self.assertEqual(s["pick_overlap_all_runs"], 2)         # 순서를 바꿔도 늘 뽑힌 기사: a, b


if __name__ == "__main__":
    unittest.main()
