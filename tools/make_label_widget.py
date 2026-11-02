"""라벨링 위젯 만들기 — eval/candidates.json 의 표본 30건을 한 장의 HTML(버튼)로 만든다.

사용자가 '중요 / 보통 / 무관'을 고르고 제출하면 라벨이 JSON 으로 돌아온다(Hermes 대화창 위젯의 window.hermes.send 사용).
대화창 밖에서 열면 제출 대신 JSON 을 복사할 수 있는 칸이 나온다. 외부 리소스는 쓰지 않는다.
피드에서 온 제목·요약·주소는 외부 입력이므로 innerHTML 을 쓰지 않고 textContent 로만 넣으며,
링크는 http(s) 주소만 허용한다(javascript: 같은 주소 차단).

  python tools/make_label_widget.py --out <저장할 HTML 경로>
"""
from __future__ import annotations

import argparse
import json
import hashlib
import secrets
import time
from uuid import uuid4
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "eval" / "candidates.json"

TEMPLATE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<style>
  * { box-sizing: border-box; }
  body { margin: 0; color: var(--foreground); font-size: 14px; }
  .hint { color: var(--muted-foreground); line-height: 1.55; margin: 0 0 10px; }
  .bar { display: flex; align-items: center; gap: 10px; margin: 0 0 10px; flex-wrap: wrap; }
  .progress { font-weight: 600; }
  .item { border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; margin: 0 0 8px; background: var(--card); }
  .meta { font-size: 12px; color: var(--muted-foreground); margin-bottom: 2px; }
  .title a { color: var(--foreground); font-weight: 600; text-decoration: none; }
  .title a:hover { text-decoration: underline; }
  .sum { font-size: 13px; color: var(--muted-foreground); margin: 4px 0 8px; line-height: 1.45; }
  .opts { display: flex; gap: 6px; }
  .opts button { padding: 4px 14px; border-radius: 999px; border: 1px solid var(--border);
                 background: transparent; color: var(--foreground); cursor: pointer; font-size: 13px; }
  .opts button.on { background: var(--accent, #0b6e77); color: var(--accent-foreground, #fff); border-color: var(--accent, #0b6e77); }
  .nav button, .submit { padding: 6px 16px; border-radius: 8px; border: 1px solid var(--border); background: var(--card);
                         color: var(--foreground); cursor: pointer; font-size: 13px; }
  .submit { background: var(--accent, #0b6e77); color: var(--accent-foreground, #fff); border-color: var(--accent, #0b6e77); font-weight: 600; }
  button:disabled { opacity: .45; cursor: not-allowed; }
  textarea { width: 100%; height: 90px; margin-top: 8px; font-size: 12px; }
</style></head>
<body>
<p class="hint">내 기준으로 <b>"오늘 내가 알아야 할 소식인가"</b>를 직관대로 골라 주세요. 모델이 무엇을 골랐는지는 보이지 않습니다(blind).
 제목을 누르면 원문이 열립니다. 모르겠으면 <b>무관</b>으로 두세요. 30건 중 25건 이상 고르면 제출할 수 있습니다.</p>
<div class="bar"><button class="submit" id="submit">라벨 제출</button><span id="status" class="hint"></span></div>
<div class="bar"><span class="progress" id="progress"></span><span class="nav" id="nav"></span></div>
<div id="list"></div>
<div id="fallback"></div>
<script>
const DATA = /*DATA*/;
const KEY = "label-" + DATA.snapshot;
const PER = 10;
const NAMES = [["중요", 2], ["보통", 1], ["무관", 0]];
let labels = {}, page = 0, submitted = false;
try { labels = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) {}
function save() { try { localStorage.setItem(KEY, JSON.stringify(labels)); } catch (e) {} }
// 문자열 자식은 텍스트 노드로 들어가 HTML 로 해석되지 않는다 (외부 피드 내용을 안전하게 넣기 위함)
function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") e.className = v;
    else if (k.startsWith("data-")) e.setAttribute(k, v);
    else e[k] = v;
  }
  e.append(...kids);
  return e;
}
function safeUrl(u) { return /^https?:\/\//i.test(u) ? u : "#"; }
function render() {
  const total = DATA.items.length, done = Object.keys(labels).length, pages = Math.ceil(total / PER);
  document.getElementById("progress").textContent = `${done}/${total} 완료`;
  document.getElementById("nav").replaceChildren(
    el("button", {id: "prev", disabled: page === 0, textContent: "이전"}), ` ${page + 1}/${pages} `,
    el("button", {id: "next", disabled: page === pages - 1, textContent: "다음"}));
  document.getElementById("list").replaceChildren(...DATA.items.slice(page * PER, page * PER + PER).map((it, i) => {
    const opts = el("div", {class: "opts"}, ...NAMES.map(([name, v]) =>
      el("button", {"data-id": it.id, "data-v": String(v), class: labels[it.id] === v ? "on" : "", textContent: name})));
    const link = el("a", {href: safeUrl(it.url), target: "_blank", rel: "noopener", textContent: it.title});
    return el("div", {class: "item"},
      el("div", {class: "meta", textContent: `${page * PER + i + 1}. ${it.source} · ${it.age}`}),
      el("div", {class: "title"}, link), el("div", {class: "sum", textContent: it.summary}), opts);
  }));
  document.getElementById("submit").disabled = done < 25 || submitted;
}
document.addEventListener("click", ev => {
  const b = ev.target.closest("button"); if (!b) return;
  if (b.dataset.id) { labels[b.dataset.id] = Number(b.dataset.v); save(); render(); }
  else if (b.id === "prev") { page = Math.max(0, page - 1); render(); }
  else if (b.id === "next") { page = Math.min(Math.ceil(DATA.items.length / PER) - 1, page + 1); render(); }
  else if (b.id === "submit") { submit(); }
});
const ENDPOINT = "http://127.0.0.1:8765/labels";
function sentFlag() { try { return localStorage.getItem("sent-" + DATA.snapshot) === "1"; } catch (e) { return false; } }
function markSent() { try { localStorage.setItem("sent-" + DATA.snapshot, "1"); } catch (e) {} }
function showFallback(text) {
  document.getElementById("fallback").replaceChildren(
    el("p", {class: "hint", textContent: "자동 저장이 안 됩니다. 아래 내용을 복사해 대화창에 붙여 넣어 주세요."}),
    el("textarea", {readOnly: true, value: text}));
}
function submit() {
  const text = "[라벨링 제출] " + JSON.stringify({snapshot: DATA.snapshot, labels: labels});
  const status = document.getElementById("status");
  if (window.hermes && window.hermes.send) {
    window.hermes.send(text); submitted = true;
    status.textContent = "제출했습니다. 저장 결과가 곧 표시됩니다.";
    render();
    return;
  }
  status.textContent = "저장 중...";
  // 대화창 밖에서 열었을 때: 이 PC 안(127.0.0.1)의 임시 수신기로만 보낸다
  fetch(ENDPOINT, {method: "POST", headers: {"Content-Type": "text/plain;charset=UTF-8"},
                   body: JSON.stringify({snapshot: DATA.snapshot, labels: labels, session_id: DATA.session_id, token: DATA.token})})
    .then(r => r.json())
    .then(j => {
      if (!j.ok) throw new Error(j.error || "저장 실패");
      markSent(); submitted = true; status.textContent = `저장했습니다 (${j.count}건).`; render();
    })
    .catch(() => { status.textContent = "자동 저장에 실패했습니다."; showFallback(text); });
}
render();
if (!(window.hermes && window.hermes.send) && Object.keys(labels).length >= 25 && !sentFlag()) { submit(); }
</script></body></html>
"""


def build(out: Path) -> None:
    if out.exists():
        raise FileExistsError("기존 위젯을 보존합니다. 새 HTML 경로를 지정하세요.")
    snap = json.loads(SRC.read_text(encoding="utf-8"))
    taken_at = datetime.fromisoformat(snap["snapshot_at_kst"])
    items = []
    for it in snap["items"]:
        age = "시각 미상"
        if it["published_at"]:
            hours = (taken_at - datetime.fromisoformat(it["published_at"])).total_seconds() / 3600
            age = f"{int(hours)}시간 전" if hours < 48 else f"{int(hours // 24)}일 전"
        items.append({"id": it["id"], "title": it["title"], "url": it["url"], "source": it["source"],
                      "age": age, "summary": it["summary"][:220]})
    session_id = uuid4().hex
    now = time.time()
    session = {"session_id": session_id, "token": secrets.token_urlsafe(32),
               "snapshot": snap["snapshot_at_kst"], "created_at": now, "expires_at": now + 1800,
               "candidates_sha256": hashlib.sha256(json.dumps(snap, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()}
    session_dir = ROOT / "store" / "cache" / "label_sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    with (session_dir / (session_id + ".json")).open("x", encoding="utf-8") as stream:
        json.dump(session, stream, ensure_ascii=False)
    data = json.dumps({"snapshot": snap["snapshot_at_kst"], "items": items,
                       "session_id": session_id, "token": session["token"]}, ensure_ascii=False).replace("</", "<\\/")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x", encoding="utf-8") as stream:
        stream.write(TEMPLATE.replace("/*DATA*/", data))
    print(f"위젯 저장: {out} ({out.stat().st_size:,} bytes, 표본 {len(items)}건)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "eval" / "label_widget.html"))
    build(Path(parser.parse_args().out))
