# Changelog

이 프로젝트의 눈에 보이는 변경 기록입니다. 형식은 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/)를 따릅니다.

## [Unreleased]

### 추가
- LangGraph 5노드 파이프라인(`graph.py`): 수집 → 선별(예선·본선) → 요약 → 검수(재생성 1회 후 스킵) → 발행
- 수집(`collectors.py`, `net.py`): RSS·Atom·GitHub Releases·Hacker News API·arXiv API, 시간 창·미래 날짜·제외 도메인·중복 필터, robots.txt 준수
- 발행(`publisher.py`): Discord 웹훅 임베드, dry_run / sent / failed / skipped_no_webhook 구분
- 설정 분리(`config.yaml`, `audience.yaml`, `.env`)와 실행 기록(`store/metrics.jsonl`)
- 소스 실측 도구와 결과(`tools/survey_sources.py`, `store/source_survey.json`), 채택표 생성(`tools/render_survey_table.py`)
- 정답 라벨용 표본 추출·라벨링 위젯(`tools/snapshot_candidates.py`, `tools/make_label_widget.py`), 선별 평가·검수 시험 도구(`tools/eval_selection.py`, `tools/negative_control.py`)
- 오프라인 시험 26건(`tests/`)
