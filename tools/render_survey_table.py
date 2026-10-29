"""소스 채택표 만들기 — store/source_survey.json 의 실측값과 tools/survey_decisions.yaml 의 판정을 합쳐 Markdown 표로 낸다.

  python tools/render_survey_table.py            # 표를 출력하고 store/source_survey_table.md 로 저장
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SURVEY = ROOT / "store" / "source_survey.json"
DECISIONS = ROOT / "tools" / "survey_decisions.yaml"
OUT = ROOT / "store" / "source_survey_table.md"
ROUTE_NAME = {"rss": "RSS", "atom": "Atom", "github_atom": "GitHub Atom", "api_hn": "API(HN)", "api_arxiv": "API(arXiv)"}


def gate(r: dict) -> str:
    g = r.get("gates") or {}
    return " ".join(f"{k}{'o' if g.get(k) else 'x'}" for k in ("G1", "G2", "G3"))


def render() -> str:
    survey = json.loads(SURVEY.read_text(encoding="utf-8"))
    decisions = yaml.safe_load(DECISIONS.read_text(encoding="utf-8"))
    lines = [f"실측 시각 {survey['surveyed_at_kst']} · 요청 {survey['requests_made_this_run']}건 · 본문 기준 {survey['min_body_chars']}자 · "
             f"소스당 본문 표본 {survey['body_samples']}건 · User-Agent `{survey['user_agent']}`", "",
             "| 소스 | 경로·tier | HTTP | 항목 | 24h/72h/7d | 최신(h) | 본문 | robots | 키워드 | 관문 | 판정 | 근거 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    order = sorted(survey["sources"], key=lambda r: (decisions[r["id"]]["decision"] != "채택", r["id"]))
    for r in order:
        http = (r.get("http") or {}).get("status")
        vol = f"{r.get('items_24h', '-')}/{r.get('items_72h', '-')}/{r.get('items_7d', '-')}"
        newest = r.get("newest_age_h")
        d = decisions[r["id"]]
        lines.append(f"| {r['name']} | {ROUTE_NAME.get(r['route'], r['route'])} · {r['tier']} | {http if http else '—'} | "
                     f"{r.get('entries', '—')} | {vol} | {newest if newest is not None else '—'} | {r.get('body_ok', '—')} | "
                     f"{str(r.get('robots_feed'))[:11]} | {r.get('keyword_hit_7d') if r.get('keyword_hit_7d') is not None else '—'} | "
                     f"{gate(r)} | **{d['decision']}** | {d['reason']} |")
    return "\n".join(lines)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    table = render()
    OUT.write_text(table + "\n", encoding="utf-8")
    print(table)
