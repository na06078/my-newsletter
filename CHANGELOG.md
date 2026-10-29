# Changelog

이 프로젝트의 눈에 보이는 변경 기록입니다. 형식은 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/)를 따릅니다.

## [Unreleased]

### 추가
- 영속 실제 요청 예산(`호출예산.py`): 실패·SDK 재시도·온도 옵션 재요청을 SQLite에 사전 예약하고 누적 65회에서 차단
- 불변 발행 준비본(`발행승인.py`): payload 전체 해시 확인, 일회성 전송 예약, 저장 내용만 전송, 메시지 재조회·일치 확인
- 연결 확인 도구(`tools/연결확인.py`)와 예산·승인·CLI·연결 보완 검사
- LangGraph 5노드 파이프라인(`graph.py`): 수집 → 선별(예선·본선) → 요약 → 검수(재생성 1회 후 스킵) → 발행
- 수집(`collectors.py`, `net.py`): RSS·Atom·GitHub Releases·Hacker News API·arXiv API, 시간 창·미래 날짜·제외 도메인·중복 필터, robots.txt 준수
- 발행(`publisher.py`): Discord 웹훅 임베드, dry_run / sent / failed / skipped_no_webhook 구분
- 설정 분리(`config.yaml`, `audience.yaml`, `.env`)와 실행 기록(`store/metrics.jsonl`)
- 소스 실측 도구와 결과(`tools/survey_sources.py`, `store/source_survey.json`), 채택표 생성(`tools/render_survey_table.py`)
- 정답 라벨용 표본 추출·라벨링 위젯(`tools/snapshot_candidates.py`, `tools/make_label_widget.py`), 선별 평가·검수 시험 도구(`tools/eval_selection.py`, `tools/negative_control.py`)
- 오프라인 시험 26건(`tests/`)
- `REPORT.md` 초안: 소스 채택표(실측), 선별 로직 설계, 파이프라인 구조도(코드에서 추출), 예외 처리표. 모델 실행 결과·사용자 작성 항목은 `[작성 대기]`로 표시
- `store/graph_mermaid.txt`: 컴파일된 그래프에서 뽑은 Mermaid 구조도

- 라벨 수신기(`tools/label_receiver.py`): 위젯이 보낸 라벨을 127.0.0.1 에서 한 번 받아 `eval/labels.json`으로 저장하고 종료. 잘못된 표본 시각·모르는 id·25건 미만·값 범위 밖은 거부
- 사용자 정답 라벨 30건(`eval/labels.json`: 중요 19 · 보통 8 · 무관 3)

### 변경
- 즉시 재생성·발송하는 `--publish`를 차단하고 준비본 조회·확인 해시를 사용한 발송으로 분리
- Discord 멘션 기본 허용을 명시적으로 끄고 오류·재조회 결과에서 비밀 웹훅 주소 노출 차단
- 라벨링 위젯: 제출 버튼을 맨 위로 옮기고, 대화창 밖에서 열어 이미 25건 이상 골라 둔 상태면 수신기로 자동 제출. 실패하면 복사 칸을 보여 줌
- `requirements.txt`를 시험한 버전(Python 3.12.10)으로 고정
