"""외부 요청 없이 frozen publication의 승인 경계를 검증한다."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import requests
from publisher import DiscordPublisher


class Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code, self.payload, self.text = status, payload, text

    def json(self):
        return self.payload


class AdapterTests(unittest.TestCase):
    URL = "https://discord.com/api/webhooks/123/SECRET-TOKEN"

    def test_readback_uses_injected_get_and_redacts_failures(self):
        calls = []
        pub = DiscordPublisher(self.URL, live=True, get=lambda *a, **kw: (calls.append((a, kw)) or Resp(payload={"id": "99", "embeds": []})))
        self.assertEqual(pub.read_message("99"), {"status": "read", "http_status": 200, "message": {"id": "99", "embeds": []}})
        self.assertEqual(calls[0], ((self.URL + "/messages/99",), {"timeout": 20}))
        self.assertEqual(pub.read_message("../escape")["error"], "invalid_message_id")
        self.assertEqual(len(calls), 1)
        pub = DiscordPublisher(self.URL, live=True, get=lambda *a, **kw: Resp(403, text=self.URL))
        self.assertNotIn("SECRET", json.dumps(pub.read_message("99")))
        pub = DiscordPublisher(self.URL, live=True, post=lambda *a, **kw: Resp(400, text=self.URL))
        self.assertNotIn("SECRET", json.dumps(pub.publish([{"title": "t"}])))

    def test_untrusted_success_id_cannot_leak_webhook(self):
        pub = DiscordPublisher(self.URL, live=True, post=lambda *a, **kw: Resp(payload={"id": self.URL}))
        result = pub.publish([{"title": "t"}])
        self.assertIsNone(result.get("message_id"))
        self.assertNotIn("SECRET", json.dumps(result))
        pub = DiscordPublisher(self.URL, live=True, get=lambda *a, **kw: Resp(payload={"id": "99", "embeds": [], "error": self.URL}))
        result = pub.read_message("99")
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("SECRET", json.dumps(result))
        pub = DiscordPublisher(self.URL + "?wait=true", live=True, get=lambda *a, **kw: Resp(payload={"id": "99", "embeds": [], "error": self.URL}))
        result = pub.read_message("99")
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("SECRET", json.dumps(result))

    def test_nonobject_success_body_preserves_known_send(self):
        for body in (None, [], "private-marker", 123):
            with self.subTest(body=body):
                calls = []
                pub = DiscordPublisher(self.URL, live=True,
                                       post=lambda *a, **kw: calls.append(kw) or Resp(payload=body))
                result = pub.publish([{"title": "t"}])
                self.assertEqual(result["status"], "sent")
                self.assertEqual(result["http_status"], 200)
                self.assertIsNone(result["message_id"])
                self.assertEqual(len(calls), 1)
                self.assertNotIn("private-marker", json.dumps(result))

    def test_explicit_mentions_and_owned_copy(self):
        calls = []
        embeds = [{"title": "승인 제목", "description": "본문"}]
        pub = DiscordPublisher(self.URL, live=True, post=lambda *a, **kw: (calls.append(kw) or Resp(payload={"id": "99"})))
        self.assertEqual(pub.publish(embeds)["status"], "sent")
        self.assertEqual(calls[0]["json"], {"username": "편집실", "embeds": embeds, "allowed_mentions": {"parse": []}})
        embeds[0]["title"] = "변경"
        self.assertEqual(calls[0]["json"]["embeds"][0]["title"], "승인 제목")


class FrozenTests(unittest.TestCase):
    def setUp(self):
        scratch = Path.home() / "AppData/Local/hermes/cache/scratch"
        self.temp = tempfile.TemporaryDirectory(dir=scratch if scratch.is_dir() else None)
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name)
        self.embeds = [{"title": "승인 제목", "description": "본문", "url": "https://example.org/a", "color": 123,
                        "footer": {"text": "출처"}}]

    def publisher(self, *, readback=None, post_error=None):
        self.posts, self.gets = [], []
        def post(url, **kw):
            self.posts.append(copy.deepcopy(kw))
            if post_error:
                raise post_error
            return Resp(payload={"id": "99"})
        def get(url, **kw):
            self.gets.append(url)
            if readback is not None:
                return readback
            embeds = copy.deepcopy(self.embeds)
            embeds[0]["type"] = "rich"
            embeds[0]["footer"]["proxy_icon_url"] = "https://cdn.example.org/added"
            return Resp(payload={"id": "99", "embeds": embeds, "author": {"username": "편집실", "id": "321"}})
        return DiscordPublisher(AdapterTests.URL, live=True, post=post, get=get)

    def test_exact_stored_payload_sent_and_read_back(self):
        from 발행승인 import prepare_publication, publish_prepared, load_publication
        candidate = prepare_publication(self.store, "run-1", self.embeds)
        pub = self.publisher()
        result = publish_prepared(self.store, "run-1", candidate["sha256"], pub)
        self.assertEqual(result.get("status"), "sent")
        self.assertEqual(result["readback"]["status"], "verified")
        self.assertEqual(self.posts[0]["json"], candidate["payload"])
        self.assertEqual(self.posts[0]["params"], {"wait": "true"})
        self.assertEqual(len(self.gets), 1)
        self.assertEqual(load_publication(self.store, "run-1"), candidate)
        receipt = json.loads((self.store / "cache/publications/run-1.receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["sha256"], candidate["sha256"])
        self.assertEqual(receipt["result"], result)

    def test_rate_limit_posts_once_without_parsing_untrusted_body_or_sleep(self):
        from 발행승인 import prepare_publication, publish_prepared
        candidate = prepare_publication(self.store, "rate-limit", self.embeds)
        posts, sleeps, gets = [], [], []

        def post(*args, **kwargs):
            posts.append(copy.deepcopy(kwargs))
            return Resp(429, payload={"retry_after": 0, "error": AdapterTests.URL}, text=AdapterTests.URL)

        pub = DiscordPublisher(AdapterTests.URL, live=True, post=post,
                               get=lambda *a, **kw: gets.append(1), sleep=sleeps.append)
        result = publish_prepared(self.store, "rate-limit", candidate["sha256"], pub)
        self.assertEqual(len(posts), 1)
        self.assertEqual(sleeps, [])
        self.assertEqual(gets, [])
        self.assertEqual(posts[0]["json"], candidate["payload"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["http_status"], 429)
        self.assertEqual(result["error"], "HTTPError")
        self.assertEqual(result["send_outcome"], "rejected")
        self.assertEqual(result["readback"]["status"], "unavailable")
        self.assertNotIn("SECRET", json.dumps(result))
        receipt = json.loads((self.store / "cache/publications/rate-limit.receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["result"], result)
        with self.assertRaisesRegex(ValueError, "already_claimed"):
            publish_prepared(self.store, "rate-limit", candidate["sha256"], pub)
        self.assertEqual(len(posts), 1)

    def test_missing_username_never_verifies_or_resends(self):
        from 발행승인 import prepare_publication, publish_prepared
        for n, author in enumerate((None, {}, {"id": "321"}, {"username": None}, {"username": ""})):
            with self.subTest(author=author):
                run_id = f"missing-author-{n}"
                candidate = prepare_publication(self.store, run_id, self.embeds)
                message = {"id": "99", "embeds": copy.deepcopy(self.embeds)}
                if author is not None:
                    message["author"] = author
                pub = self.publisher(readback=Resp(payload=message))
                result = publish_prepared(self.store, run_id, candidate["sha256"], pub)
                self.assertEqual(result["readback"]["status"], "mismatch")
                with self.assertRaisesRegex(ValueError, "already_claimed"):
                    publish_prepared(self.store, run_id, candidate["sha256"], pub)
                self.assertEqual(len(self.posts), 1)
                self.assertEqual(len(self.gets), 1)
                receipt = json.loads((self.store / f"cache/publications/{run_id}.receipt.json").read_text(encoding="utf-8"))
                self.assertEqual(receipt["result"], result)

    def test_confirmation_and_live_preflight_do_not_burn_claim(self):
        from 발행승인 import prepare_publication, publish_prepared
        candidate = prepare_publication(self.store, "run-1", self.embeds)
        pub = self.publisher()
        for confirmation in (None, "", "0" * 64):
            with self.assertRaisesRegex(ValueError, "confirmation_mismatch"):
                publish_prepared(self.store, "run-1", confirmation, pub)
        self.assertEqual(self.posts, [])
        pub.live = False
        with self.assertRaisesRegex(ValueError, "publisher_not_ready"):
            publish_prepared(self.store, "run-1", candidate["sha256"], pub)
        pub.live = True
        self.assertFalse((self.store / "cache/publications/run-1.claim.json").exists())
        self.assertEqual(publish_prepared(self.store, "run-1", candidate["sha256"], pub)["status"], "sent")
        with self.assertRaisesRegex(ValueError, "already_claimed"):
            publish_prepared(self.store, "run-1", candidate["sha256"], pub)
        self.assertEqual(len(self.posts), 1)

    def test_malformed_author_preserves_known_sent_receipt(self):
        from 발행승인 import prepare_publication, publish_prepared
        for n, author in enumerate((None, "private-marker", [])):
            with self.subTest(author=author):
                run_id = f"malformed-author-{n}"
                candidate = prepare_publication(self.store, run_id, self.embeds)
                pub = self.publisher(readback=Resp(payload={"id": "99", "embeds": self.embeds,
                                                           "author": author, "username": "편집실"}))
                result = publish_prepared(self.store, run_id, candidate["sha256"], pub)
                self.assertEqual(result["status"], "sent")
                self.assertEqual(result["message_id"], "99")
                self.assertEqual(result["send_outcome"], "sent")
                self.assertEqual(result["readback"], {"status": "failed"})
                self.assertNotIn("private-marker", json.dumps(result))
                receipt = json.loads((self.store / f"cache/publications/{run_id}.receipt.json").read_text(encoding="utf-8"))
                self.assertEqual(receipt["result"], result)
                with self.assertRaisesRegex(ValueError, "already_claimed"):
                    publish_prepared(self.store, run_id, candidate["sha256"], pub)
                self.assertEqual(len(self.posts), 1)
                self.assertEqual(len(self.gets), 1)

    def test_receipt_io_failure_preserves_known_result_without_resending(self):
        import 발행승인 as publication
        candidate = publication.prepare_publication(self.store, "receipt-error", self.embeds)
        original = publication._create_immutable
        def write(path, value):
            if path.name.endswith(".receipt.json"):
                raise OSError(AdapterTests.URL)
            return original(path, value)
        pub = self.publisher()
        with patch.object(publication, "_create_immutable", side_effect=write):
            result = publication.publish_prepared(self.store, "receipt-error", candidate["sha256"], pub)
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["message_id"], "99")
        self.assertEqual(result["send_outcome"], "sent")
        self.assertEqual(result["readback"], {"status": "verified"})
        self.assertEqual(result["recording_error"], "receipt_write_failed")
        self.assertNotIn("SECRET", json.dumps(result))
        with self.assertRaisesRegex(ValueError, "already_claimed"):
            publication.publish_prepared(self.store, "receipt-error", candidate["sha256"], pub)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(len(self.gets), 1)
        self.assertFalse((self.store / "cache/publications/receipt-error.receipt.json").exists())

    def test_immutable_wrapper_keeps_canonical_policy_and_exception_semantics(self):
        import 발행승인 as publication
        from 불변저장 import write_bytes_once
        path = self.store / "wrapper.json"
        with patch.object(publication, "write_bytes_once", wraps=write_bytes_once) as write:
            publication._create_immutable(path, {"나": 2, "a": 1})
            write.assert_called_once_with(path, '{"a":1,"나":2}'.encode("utf-8"))
        with self.assertRaises(FileExistsError):
            publication._create_immutable(path, {"replacement": True})
        self.assertEqual(path.read_bytes(), '{"a":1,"나":2}'.encode("utf-8"))
        for value, reason in (({"key": AdapterTests.URL}, "secret_not_allowed"),
                              ({"value": float("nan")}, "invalid_json")):
            with self.subTest(reason=reason), patch.object(publication, "write_bytes_once") as write:
                with self.assertRaisesRegex(publication.PublicationError, reason):
                    publication._create_immutable(self.store / "unsafe.json", value)
                write.assert_not_called()
        with patch.object(publication, "write_bytes_once", side_effect=OSError("fake")):
            with self.assertRaises(OSError):
                publication._create_immutable(self.store / "error.json", {"safe": True})

    def test_concurrent_claims_post_exactly_once(self):
        from 발행승인 import prepare_publication, publish_prepared
        candidate = prepare_publication(self.store, "run-1", self.embeds)
        pub = self.publisher()
        def send(_):
            try:
                return publish_prepared(self.store, "run-1", candidate["sha256"], pub)["status"]
            except ValueError as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(send, range(8)))
        self.assertEqual(outcomes.count("sent"), 1)
        self.assertEqual(outcomes.count("already_claimed"), 7)
        self.assertEqual(len(self.posts), 1)

    def test_unknown_send_outcome_is_persisted_and_never_retried(self):
        from 발행승인 import prepare_publication, publish_prepared
        candidate = prepare_publication(self.store, "run-1", self.embeds)
        pub = self.publisher(post_error=requests.Timeout(AdapterTests.URL))
        result = publish_prepared(self.store, "run-1", candidate["sha256"], pub)
        self.assertEqual(result.get("send_outcome"), "unknown")
        self.assertEqual(result["readback"]["status"], "unavailable")
        with self.assertRaisesRegex(ValueError, "already_claimed"):
            publish_prepared(self.store, "run-1", candidate["sha256"], pub)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.gets, [])
        self.assertNotIn("SECRET", json.dumps(result))
        receipt = (self.store / "cache/publications/run-1.receipt.json").read_text(encoding="utf-8")
        self.assertNotIn("SECRET", receipt)

    def test_unexpected_transport_exception_retains_safe_unknown_receipt(self):
        from 발행승인 import prepare_publication, publish_prepared
        candidate = prepare_publication(self.store, "run-1", self.embeds)
        pub = self.publisher(post_error=RuntimeError(AdapterTests.URL))
        result = publish_prepared(self.store, "run-1", candidate["sha256"], pub)
        self.assertEqual(result.get("send_outcome"), "unknown")
        self.assertNotIn("SECRET", json.dumps(result))
        with self.assertRaisesRegex(ValueError, "already_claimed"):
            publish_prepared(self.store, "run-1", candidate["sha256"], pub)
        self.assertEqual(len(self.posts), 1)

    def test_readback_mismatches_and_failed_get_do_not_resend(self):
        from 발행승인 import prepare_publication, publish_prepared
        cases = []
        for field in ("title", "description", "url", "color", "footer"):
            embeds = copy.deepcopy(self.embeds)
            embeds[0][field] = {"text": "다른 출처"} if field == "footer" else (321 if field == "color" else "다른 내용")
            cases.append((Resp(payload={"id": "99", "embeds": embeds, "author": {"username": "편집실"}}), "mismatch"))
        cases.append((Resp(payload={"id": "99", "embeds": self.embeds, "author": {"username": "다른 편집자"}}), "mismatch"))
        cases.append((Resp(404, text=AdapterTests.URL), "failed"))
        for n, (response, expected) in enumerate(cases):
            run_id = f"run-{n}"
            candidate = prepare_publication(self.store, run_id, self.embeds)
            pub = self.publisher(readback=response)
            with self.subTest(n=n):
                result = publish_prepared(self.store, run_id, candidate["sha256"], pub)
                self.assertEqual(result["readback"]["status"], expected)
                with self.assertRaisesRegex(ValueError, "already_claimed"):
                    publish_prepared(self.store, run_id, candidate["sha256"], pub)
                self.assertEqual(len(self.posts), 1)
                self.assertEqual(len(self.gets), 1)

    def test_tampered_candidate_never_claims_or_posts(self):
        from 발행승인 import prepare_publication, publish_prepared
        candidate = prepare_publication(self.store, "run-1", self.embeds)
        path = self.store / "cache/publications/run-1.json"
        bad = copy.deepcopy(candidate)
        bad["payload"]["embeds"][0]["title"] = "조작"
        path.write_text(json.dumps(bad), encoding="utf-8")
        pub = self.publisher()
        with self.assertRaisesRegex(ValueError, "hash_mismatch"):
            publish_prepared(self.store, "run-1", candidate["sha256"], pub)
        self.assertEqual(self.posts, [])
        self.assertFalse((path.parent / "run-1.claim.json").exists())

    def test_stale_claim_without_receipt_is_never_recovered_by_resending(self):
        from 발행승인 import prepare_publication, publish_prepared
        candidate = prepare_publication(self.store, "run-1", self.embeds)
        (self.store / "cache/publications/run-1.claim.json").write_text("interrupted", encoding="utf-8")
        pub = self.publisher()
        with self.assertRaisesRegex(ValueError, "already_claimed"):
            publish_prepared(self.store, "run-1", candidate["sha256"], pub)
        self.assertEqual(self.posts, [])

    def test_secret_url_is_not_persisted_in_candidate_or_response(self):
        from 발행승인 import prepare_publication
        with self.assertRaisesRegex(ValueError, "secret_not_allowed"):
            prepare_publication(self.store, "run-1", self.embeds, metadata={"webhook": AdapterTests.URL})
        self.assertFalse((self.store / "cache/publications/run-1.json").exists())
        embeds = [{"title": "비밀을 저장하지 않음", "description": AdapterTests.URL}]
        with self.assertRaisesRegex(ValueError, "secret_not_allowed"):
            prepare_publication(self.store, "run-2", embeds)
        self.assertFalse((self.store / "cache/publications/run-2.json").exists())

    def test_get_exception_is_reported_without_resending(self):
        from 발행승인 import prepare_publication, publish_prepared
        candidate = prepare_publication(self.store, "run-1", self.embeds)
        pub = self.publisher()
        def fail_get(*args, **kwargs):
            raise requests.Timeout(AdapterTests.URL)
        pub._get = fail_get
        result = publish_prepared(self.store, "run-1", candidate["sha256"], pub)
        self.assertEqual(result["readback"]["status"], "failed")
        with self.assertRaisesRegex(ValueError, "already_claimed"):
            publish_prepared(self.store, "run-1", candidate["sha256"], pub)
        self.assertEqual(len(self.posts), 1)
        self.assertNotIn("SECRET", json.dumps(result))

    def test_metadata_is_json_safe_but_excluded_from_payload_hash(self):
        from 발행승인 import prepare_publication, load_publication
        one = prepare_publication(self.store, "run-1", self.embeds, metadata={"note": "첫 설명"})
        two = prepare_publication(self.store, "run-2", self.embeds, metadata={"note": "다른 설명"})
        self.assertEqual(one["sha256"], two["sha256"])
        path = self.store / "cache/publications/run-1.json"
        one["metadata"] = {"unsafe": AdapterTests.URL}
        path.write_text(json.dumps(one), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "secret_not_allowed"):
            load_publication(self.store, "run-1")

    def test_concurrent_preparation_cannot_replace_another_candidate(self):
        from 발행승인 import prepare_publication, load_publication
        def prepare(n):
            try:
                return prepare_publication(self.store, "run-1", [{"title": f"제목{n}"}])
            except ValueError as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(prepare, range(8)))
        successes = [r for r in outcomes if isinstance(r, dict)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(outcomes.count("candidate_exists"), 7, outcomes)
        self.assertEqual(load_publication(self.store, "run-1"), successes[0])

    def test_candidate_is_immutable_and_tamper_is_rejected(self):
        from 발행승인 import prepare_publication, load_publication
        original = prepare_publication(self.store, "run-1", self.embeds)
        changed = copy.deepcopy(self.embeds)
        changed[0]["title"] = "다른 제목"
        with self.assertRaisesRegex(ValueError, "candidate_exists"):
            prepare_publication(self.store, "run-1", changed)
        self.assertEqual(load_publication(self.store, "run-1"), original)
        self.assertEqual(prepare_publication(self.store, "run-1", self.embeds, metadata={"new": True}), original)
        path = self.store / "cache/publications/run-1.json"
        bad = copy.deepcopy(original)
        bad["payload"]["username"] = "다른 편집자"
        path.write_text(json.dumps(bad), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hash_mismatch"):
            load_publication(self.store, "run-1")

    def test_paths_and_schema_are_validated(self):
        from 발행승인 import prepare_publication, load_publication
        for run_id in ("../escape", "a/b", "a\\b", "", "CON", "run.", "x:stream"):
            with self.subTest(run_id=run_id), self.assertRaisesRegex(ValueError, "invalid_run_id"):
                prepare_publication(self.store, run_id, self.embeds)
        for embeds in ([], [{"title": 12}], [{"description": "x" * 4097}], [{"color": True}], [{"title": "ok", "unknown": "x"}]):
            with self.subTest(embeds=embeds), self.assertRaisesRegex(ValueError, "invalid_payload"):
                prepare_publication(self.store, "bad", embeds)
        prepare_publication(self.store, "run-1", self.embeds)
        path = self.store / "cache/publications/run-1.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["payload"]["allowed_mentions"] = {"parse": ["everyone"]}
        raw["sha256"] = hashlib.sha256(json.dumps(raw["payload"], sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid_payload"):
            load_publication(self.store, "run-1")

    def test_snapshot_is_deep_copied_and_canonical(self):
        from 발행승인 import prepare_publication, load_publication
        result = prepare_publication(self.store, "run-1", self.embeds, metadata={"note": "설명"})
        expected = {"username": "편집실", "embeds": copy.deepcopy(self.embeds), "allowed_mentions": {"parse": []}}
        canonical_bytes = ('{"allowed_mentions":{"parse":[]},"embeds":[{"color":123,"description":"본문",'
                           '"footer":{"text":"출처"},"title":"승인 제목","url":"https://example.org/a"}],"username":"편집실"}').encode("utf-8")
        digest = hashlib.sha256(canonical_bytes).hexdigest()
        self.assertEqual(result["payload"], expected)
        self.assertEqual(result["sha256"], digest)
        self.embeds[0]["title"] = "바뀜"
        result["payload"]["embeds"][0]["description"] = "바뀜"
        self.assertEqual(load_publication(self.store, "run-1")["payload"], expected)


if __name__ == "__main__":
    unittest.main()
