"""공통 불변 바이트 저장: 완성본 게시·기존 파일 보존·실패 정리를 확인한다."""
import importlib
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


class ImmutableWriteTests(unittest.TestCase):
    def writer(self):
        try:
            return importlib.import_module("불변저장").write_bytes_once
        except ModuleNotFoundError:
            self.fail("공통 불변 바이트 저장 진입점이 아직 없습니다")

    def test_complete_bytes_are_published(self):
        writer = self.writer()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested/결과.json"
            writer(target, b'{"complete":true}')
            self.assertEqual(target.read_bytes(), b'{"complete":true}')
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_existing_destination_is_never_replaced(self):
        writer = self.writer()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "결과.json"
            target.write_bytes(b"historical")
            with self.assertRaises(FileExistsError):
                writer(target, b"new")
            self.assertEqual(target.read_bytes(), b"historical")
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_flush_failure_publishes_nothing_and_cleans_only_owned_temp(self):
        writer = self.writer()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unrelated = root / ".이전작업.tmp"
            unrelated.write_bytes(b"keep")
            with patch("불변저장.os.fsync", side_effect=OSError("test-disk-error")):
                with self.assertRaises(OSError):
                    writer(root / "결과.json", b"complete")
            self.assertEqual(list(root.iterdir()), [unrelated])
            self.assertEqual(unrelated.read_bytes(), b"keep")

    def test_publish_failure_leaves_no_partial_destination(self):
        writer = self.writer()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("불변저장.os.link", side_effect=OSError("test-link-error")):
                with self.assertRaises(OSError):
                    writer(root / "결과.json", b"complete")
            self.assertEqual(list(root.iterdir()), [])

    def test_concurrent_writers_publish_exactly_one_complete_value(self):
        writer = self.writer()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "결과.json"
            start = threading.Barrier(2)
            outcomes = []
            def write(value):
                start.wait()
                try:
                    writer(target, value)
                    outcomes.append("written")
                except FileExistsError:
                    outcomes.append("exists")
            threads = [threading.Thread(target=write, args=(value,)) for value in (b"first", b"second")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(3)
                self.assertFalse(thread.is_alive())
            self.assertCountEqual(outcomes, ["written", "exists"])
            self.assertIn(target.read_bytes(), [b"first", b"second"])
            self.assertEqual(list(root.iterdir()), [target])

if __name__ == "__main__":
    unittest.main()
