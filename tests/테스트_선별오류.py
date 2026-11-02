"""2차 선별·본문 실패 경계. 실제 모델/네트워크와 기존 저장소는 쓰지 않는다."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from graph import Pipeline, Pick, Shortlist, initial_state, enforce_rules
from metrics import Metrics
from tests.support import NOW, SRC, FakeHttp, FakeLLM, FakePublisher, FakeRobots, make_settings
from tests import test_pipeline as fixtures


class SelectionFailureTests(unittest.TestCase):
    def run_case(self, handler=None, *, n=4, short=False):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        settings = make_settings(max_per_source=10)
        http = FakeHttp({SRC["url"]: fixtures.feed_of(n, body_chars=100 if short else 700)})
        pub = FakePublisher()
        pipe = Pipeline(settings, llm=FakeLLM(handler), http=http, robots=FakeRobots(),
                        metrics=Metrics(root / "events.jsonl", "test-run"), publisher=pub,
                        store=root, now_fn=lambda: NOW)
        final = pipe.build().invoke(initial_state("test-run", 48), config={"max_concurrency": 4})
        events = [json.loads(line) for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        return final, events, pub

    def test_empty_prelim_result_is_fallback_not_editorial_no_news(self):
        def handler(stage, system, user, schema):
            if stage == "select_prelim":
                return Shortlist(picks=[])
            return FakeLLM.default(stage, system, user, schema)
        final, events, pub = self.run_case(handler)
        self.assertEqual(final.get("selection_status"), "fallback")
        self.assertEqual(final["selection_fallbacks"][0]["stage"], "select_prelim")
        self.assertEqual(final["selection_fallbacks"][0]["reason"], "no_valid_picks")
        self.assertEqual(len(final["picked"]), 4)
        self.assertEqual(len(pub.embeds), 5)

    def test_invalid_final_indices_are_fallback(self):
        def handler(stage, system, user, schema):
            if stage == "select_final":
                return Shortlist(picks=[Pick(index=999, score=9, reason="bad", event="bad")])
            return FakeLLM.default(stage, system, user, schema)
        final, _, _ = self.run_case(handler)
        self.assertEqual(final.get("selection_status"), "fallback")
        self.assertEqual(final["selection_fallbacks"][0]["stage"], "select_final")
        self.assertEqual(len(final["picked"]), 4)

    def test_valid_model_and_empty_input_have_distinct_status(self):
        final, _, _ = self.run_case()
        self.assertEqual(final.get("selection_status"), "model")
        self.assertEqual(final["selection_fallbacks"], [])
        empty, _, _ = self.run_case(n=0)
        self.assertEqual(empty.get("selection_status"), "empty_input")
        self.assertEqual(empty["selection_fallbacks"], [])

    def test_body_error_reaches_report_and_final_rejection_without_regeneration(self):
        with patch("graph.fetch_body", return_value={"text": "", "status": 200, "robots": "allowed", "error": "response_too_large"}):
            final, events, pub = self.run_case(short=True)
        reports = [event for event in events if event["stage"] == "report"]
        self.assertEqual(len(reports), 4)
        self.assertTrue(all(event.get("body_error") == "response_too_large" for event in reports))
        self.assertTrue(all("response_too_large" in item["reason"] for item in final["rejected"]))
        self.assertEqual(final["verified"], [])
        self.assertEqual(pub.calls, 1)

    def test_source_backfill_cannot_duplicate_an_event_under_stricter_source_cap(self):
        ranked = [({"id": "a1", "source_id": "a"}, Pick(index=0, score=9, reason="", event="X")),
                  ({"id": "a2", "source_id": "a"}, Pick(index=1, score=8, reason="", event="Z")),
                  ({"id": "b1", "source_id": "b"}, Pick(index=2, score=7, reason="", event="Z"))]
        taken, dropped = enforce_rules(ranked, target=5, min_final=3, max_per_source=1)
        self.assertEqual([item["id"] for item, _ in taken], ["a1", "b1"])

if __name__ == "__main__":
    unittest.main()
