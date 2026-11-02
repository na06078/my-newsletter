"""2차 도구 보완: 임시 파일과 loopback만 사용한다."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tools import snapshot_candidates as snapshot
from tests import 테스트_도구경계 as boundaries
import 불변저장


class SnapshotPreservation(unittest.TestCase):
    def test_existing_output_refused_before_settings_and_http(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "표본.json"
            out.write_bytes(b"historical")
            with patch.object(snapshot, "OUT", out), patch("sys.argv", ["snapshot"]), \
                 patch.object(snapshot, "load_settings", side_effect=AssertionError("settings called")) as settings, \
                 patch.object(snapshot, "Http") as http:
                with self.assertRaises(FileExistsError):
                    snapshot.main()
                settings.assert_not_called(); http.assert_not_called()
            self.assertEqual(out.read_bytes(), b"historical")


    def test_out_support_and_final_race_preserve_winner(self):
        from types import SimpleNamespace
        cfg = {"user_agent": "fixture", "request_timeout_s": 1, "hours": 24}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); default, out = root / "기존.json", root / "새.json"
            default.write_bytes(b"historical")
            with patch.object(snapshot, "OUT", default), patch("sys.argv", ["snapshot", "--out", str(out)]), \
                 patch.object(snapshot, "load_settings", return_value={"cfg": {"collect": cfg}}), \
                 patch.object(snapshot, "Http", return_value=SimpleNamespace(count=0)), \
                 patch.object(snapshot.collectors, "collect", return_value=([], {})):
                snapshot.main()
            self.assertEqual(default.read_bytes(), b"historical")
            self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["items"], [])
            out.unlink()
            def competing_collect(*args):
                out.write_bytes(b"race-winner")
                return [], {}
            with patch.object(snapshot, "OUT", default), patch("sys.argv", ["snapshot", "--out", str(out)]), \
                 patch.object(snapshot, "load_settings", return_value={"cfg": {"collect": cfg}}), \
                 patch.object(snapshot, "Http", return_value=SimpleNamespace(count=0)), \
                 patch.object(snapshot.collectors, "collect", side_effect=competing_collect):
                with self.assertRaises(FileExistsError): snapshot.main()
            self.assertEqual(out.read_bytes(), b"race-winner")
            self.assertEqual(list(root.glob(".불변저장-*")), [])


class AtomicLabelPreservation(unittest.TestCase):
    runtime = boundaries.ReceiverRuntime.runtime
    post = boundaries.ReceiverRuntime.post
    payload = boundaries.ReceiverRuntime.payload

    def test_failed_flush_leaves_no_final_file_and_retry_succeeds(self):
        with self.runtime() as (server, thread, session, _, out, result):
            with patch.object(불변저장.os, "fsync", side_effect=OSError("disk failure")):
                code, body, _ = self.post(server, self.payload(session))
            self.assertEqual((code, body.get("error")), (500, "label_write_failed"))
            self.assertFalse(out.exists())
            self.assertEqual(list(out.parent.glob(".불변저장-*")), [])
            code, body, _ = self.post(server, self.payload(session))
            self.assertEqual(code, 200)
            saved = json.loads(out.read_text(encoding="utf-8"))
            self.assertNotIn("token", saved)
            self.assertNotIn("session_id", saved)

    def test_concurrent_posts_have_exactly_one_atomic_winner(self):
        import concurrent.futures
        import threading
        from tools import label_receiver as receiver
        original = receiver.write_bytes_once
        gate = threading.Barrier(2)
        def together(path, data):
            gate.wait(timeout=2)
            return original(path, data)
        with self.runtime() as (server, thread, session, _, out, result):
            with patch.object(receiver, "write_bytes_once", side_effect=together):
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(self.post, server, {**self.payload(session), "labels": {f"i{i}": value for i in range(25)}}) for value in (0, 2)]
                    replies = [f.result(timeout=3) for f in futures]
            self.assertEqual(sorted(r[0] for r in replies), [200, 409])
            self.assertEqual(next(r[1]["error"] for r in replies if r[0] == 409), "labels_exist")
            saved = json.loads(out.read_text(encoding="utf-8"))
            self.assertIn(set(saved["labels"].values()), [{0}, {2}])
            self.assertEqual(list(out.parent.glob(".불변저장-*")), [])


class EvaluationClockAndStatus(unittest.TestCase):
    def run_eval(self, root, statuses):
        import config, graph, llm
        from tools import eval_selection as ev
        from tests.테스트_도구경계 import candidates, labels
        cand, doc = candidates(), labels()
        doc["labels"]["i24"] = 0
        cp, lp = root / "candidates.json", root / "labels.json"
        cp.write_text(json.dumps(cand), encoding="utf-8")
        lp.write_text(json.dumps(doc), encoding="utf-8")
        seen = []
        class Model:
            calls = 0
            def __init__(self, *args): pass
        class Pipeline:
            def __init__(self, *args, **kwargs):
                seen.append(kwargs.get("now_fn"))
                self.n = 0
            def select(self, state):
                status = statuses[self.n]; self.n += 1
                item = next(i for i in state["collected"] if i["id"] == ("i24" if status == "fallback" else "i0"))
                result = {"shortlisted": state["collected"], "picked": [{**item, "final": {"score": 9, "reason": "fixture"}}]}
                if status is not None:
                    result.update(selection_status=status, selection_fallbacks=[] if status == "model" else [{"stage": "final", "reason": "llm_error", "batch": None}])
                return result
        settings = {"root": root, "cfg": {"run": {"store_dir": "store"}, "llm": {"models": {}}}, "audience": {"importance": "fixture"}}
        (root / "eval").mkdir(exist_ok=True)
        with patch.object(ev, "ROOT", root), patch.object(ev, "CANDIDATES", cp), patch.object(ev, "LABELS", lp), \
             patch.object(ev, "OUT", root / "eval/selection_eval.json"), patch("sys.argv", ["eval", "--repeats", str(len(statuses))]), \
             patch.object(config, "load_settings", return_value=settings), patch.object(graph, "Pipeline", Pipeline), patch.object(llm, "LLM", Model):
            code = ev.main()
        return code, seen, list((root / "eval").glob("selection_eval-*.json"))

    def test_snapshot_clock_and_model_only_summary(self):
        from datetime import datetime
        with tempfile.TemporaryDirectory() as tmp:
            code, clocks, paths = self.run_eval(Path(tmp), ["model", "fallback"])
            self.assertEqual(code, 0)
            self.assertTrue(callable(clocks[0]), "Pipeline must use snapshot now_fn")
            self.assertEqual(clocks[0](), datetime.fromisoformat("2026-11-02T10:00:00+09:00"))
            result = json.loads(paths[0].read_text(encoding="utf-8"))
            self.assertEqual(result["summary"]["precision"], 1.0)
            self.assertEqual(result["fallback_summary"]["precision"], 0.0)
            self.assertEqual(result["selection_status_counts"], {"model": 1, "fallback": 1, "empty_input": 0})
            self.assertEqual(result["clock_source"], "snapshot_at_kst")
            self.assertEqual([r["selection_status"] for r in result["runs"]], ["model", "fallback"])
            self.assertEqual(result["runs"][1]["selection_fallbacks"][0]["reason"], "llm_error")

    def test_missing_production_status_is_not_treated_as_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, paths = self.run_eval(Path(tmp), [None])
            self.assertEqual(code, 1)
            self.assertEqual(paths, [])

    def test_all_fallback_runs_have_no_normal_model_average(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, paths = self.run_eval(Path(tmp), ["fallback", "fallback"])
            self.assertEqual(code, 0)
            result = json.loads(paths[0].read_text(encoding="utf-8"))
            self.assertIsNone(result["summary"]["precision"])
            self.assertEqual(result["summary"]["pick_overlap_all_runs"], 0)
            self.assertEqual(result["fallback_summary"]["precision"], 0.0)
            self.assertEqual(result["selection_status_counts"]["model"], 0)


class PositiveCliBounds(unittest.TestCase):
    def test_nonpositive_arguments_stop_before_settings(self):
        import config
        from tools import eval_selection as ev, negative_control as nc
        for module, option in [(ev, "--repeats"), (nc, "--limit")]:
            for value in ("0", "-1"):
                with self.subTest(module=module.__name__, value=value), \
                     patch("sys.argv", ["tool", option, value]), \
                     patch.object(config, "load_settings", side_effect=AssertionError("settings called")) as settings:
                    with self.assertRaises(SystemExit) as caught:
                        module.main()
                    self.assertEqual(caught.exception.code, 2)
                    settings.assert_not_called()

    def test_copied_snapshot_and_invalid_bound_commands(self):
        import os
        import shutil
        import subprocess
        import sys
        from tools import eval_selection as ev, negative_control as nc
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "tools").mkdir(); (root / "eval").mkdir()
            for module in (snapshot, ev, nc):
                source = Path(module.__file__)
                shutil.copy2(source, root / "tools" / source.name)
            shutil.copy2(Path(snapshot.__file__).resolve().parent.parent / "불변저장.py", root / "불변저장.py")
            # 명시적 대역만 있는 복사 루트. 원본 dotenv/수집기/graph에 도달하지 않는다.
            (root / "collectors.py").write_text("def collect(*args): return [], {}\n", encoding="utf-8")
            (root / "net.py").write_text("class Http:\n    count=0\n    def __init__(self,*args): pass\n", encoding="utf-8")
            (root / "graph.py").write_text("def unmatched_numbers(*args): return []\n", encoding="utf-8")
            (root / "config.py").write_text("def load_settings():\n    return {'cfg': {'collect': {'user_agent':'fixture','request_timeout_s':1,'hours':24}}}\n", encoding="utf-8")
            out = root / "eval/candidates.json"; out.write_bytes(b"historical")
            env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "TMPDIR") if key in os.environ}
            env["PYTHONUTF8"] = "1"
            def run(*args):
                return subprocess.run([sys.executable, *args], cwd=root, env=env, text=True, capture_output=True, timeout=10)
            refused = run("tools/snapshot_candidates.py")
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("FileExistsError", refused.stderr)
            fresh = root / "다른폴더/새표본.json"
            made = run("tools/snapshot_candidates.py", "--out", str(fresh))
            self.assertEqual(made.returncode, 0, made.stderr)
            self.assertEqual(json.loads(fresh.read_text(encoding="utf-8"))["items"], [])
            self.assertEqual(out.read_bytes(), b"historical")
            # 각 CLI의 진짜 argparse가 데이터·모델 생성 전에 멈춘다.
            for filename, option in [("eval_selection.py", "--repeats"), ("negative_control.py", "--limit")]:
                for value in ("0", "-3"):
                    bad = run("tools/" + filename, option, value)
                    self.assertEqual(bad.returncode, 2, bad.stderr)
                    self.assertIn("양의 정수", bad.stderr)
            self.assertEqual(out.read_bytes(), b"historical")


class SavedWidgetState(unittest.TestCase):
    def test_success_reload_and_conflict_are_readonly(self):
        from tools.make_label_widget import TEMPLATE
        self.assertIn('disabled: submitted || saving', TEMPLATE)
        self.assertIn('if (submitted || saving || Object.keys(labels).length < 25) return;', TEMPLATE)
        self.assertIn('if (submitted || saving) return;', TEMPLATE)
        self.assertIn('submitted = sentFlag();', TEMPLATE)
        self.assertIn('이 브라우저의 이전 제출 기록입니다. 서버를 새로 조회한 결과는 아닙니다.', TEMPLATE)
        self.assertIn('j.error === "labels_exist"', TEMPLATE)
        self.assertIn('이미 저장된 라벨이 있어 덮어쓰지 않았습니다.', TEMPLATE)
        self.assertIn('JSON.stringify({snapshot: DATA.snapshot, labels: labels})', TEMPLATE)

    def test_javascript_success_reload_conflict_and_manual_state(self):
        import shutil
        import subprocess
        from tools.make_label_widget import TEMPLATE
        node = shutil.which("node")
        if node is None:
            self.skipTest("설치된 Node.js 없음: 소스 회귀 검사와 메인 브라우저 검증 사용")
        harness = r"""
const vm = require('node:vm'), assert = require('node:assert/strict');
let input = ''; process.stdin.setEncoding('utf8'); process.stdin.on('data', x => input += x);
process.stdin.on('end', async () => {
 const template = JSON.parse(input);
 const data = {snapshot:'fixture', session_id:'private-session', token:'private-token', items:Array.from({length:25}, (_,i)=>({id:'i'+i,title:'article',url:'https://example.org',source:'fixture',age:'now',summary:'text'}))};
 const source = template.split('<script>')[1].split('</script>')[0].replace('/*DATA*/', JSON.stringify(data));
 function boot(storage, response, hermes=false) {
  const nodes = {}, handlers = {}, sent = []; let calls = 0, resolve;
  function element(tag) { return {tag, children:[], dataset:{}, disabled:false, setAttribute(k,v){this.dataset[k.slice(5)] = v}, append(...xs){this.children.push(...xs)}, replaceChildren(...xs){this.children=xs}} }
  for (const id of ['progress','nav','list','submit','status','fallback']) nodes[id]=element('div');
  const context={console, document:{createElement:element,getElementById:id=>nodes[id],addEventListener:(name,cb)=>handlers[name]=cb},localStorage:{getItem:k=>storage[k]||null,setItem:(k,v)=>storage[k]=v},window:hermes?{hermes:{send:t=>sent.push(t)}}:{},fetch:()=>{calls++;return new Promise(r=>resolve=()=>r({json:()=>Promise.resolve(response)}))}};
  vm.createContext(context); vm.runInContext(source,context);
  return {nodes,context,sent,get calls(){return calls},finish:async()=>{resolve();await new Promise(r=>setImmediate(r))},click:b=>handlers.click({target:{closest:()=>b}})};
 }
 const initial = ()=>({'label-fixture':JSON.stringify(Object.fromEntries(data.items.map(i=>[i.id,2])))});
 const options = app=>app.nodes.list.children.flatMap(item=>item.children.at(-1).children);
 const saved=initial(), app=boot(saved,{ok:true,count:25});
 assert.equal(app.calls,1); vm.runInContext('submit(); submit();',app.context); assert.equal(app.calls,1);
 assert.ok(options(app).every(b=>b.disabled)); await app.finish();
 assert.ok(app.nodes.status.textContent.includes('저장했습니다 (25건)'));
 assert.ok(options(app).every(b=>b.disabled)); assert.equal(app.nodes.submit.disabled,true);
 app.click({dataset:{id:'i0',v:'0'}}); assert.equal(JSON.parse(saved['label-fixture']).i0,2);
 app.click({id:'next',dataset:{}}); assert.ok(options(app).every(b=>b.disabled));
 vm.runInContext('submit()',app.context); assert.equal(app.calls,1);
 const reload=boot(saved,{ok:true}); assert.equal(reload.calls,0); assert.ok(options(reload).every(b=>b.disabled));
 assert.ok(reload.nodes.status.textContent.includes('서버를 새로 조회한 결과는 아닙니다'));
 const conflict=boot(initial(),{ok:false,error:'labels_exist'}); await conflict.finish();
 assert.ok(conflict.nodes.status.textContent.includes('이미 저장된 라벨')); assert.equal(conflict.nodes.fallback.children.length,0); assert.ok(options(conflict).every(b=>b.disabled));
 const manual=boot(initial(),{},true); vm.runInContext('submit(); submit();',manual.context);
 assert.equal(manual.sent.length,1); assert.equal(manual.calls,0); assert.ok(!manual.sent[0].includes('token')); assert.ok(!manual.sent[0].includes('session_id')); assert.ok(options(manual).every(b=>b.disabled));
 console.log('saved/reload/conflict/manual: OK');
});
"""
        result = subprocess.run([node, "-e", harness], input=json.dumps(TEMPLATE), text=True,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("saved/reload/conflict/manual: OK", result.stdout)
