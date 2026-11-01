# Changelog

이 프로젝트의 눈에 보이는 변경 기록입니다. 형식은 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/)를 따릅니다.

## [Unreleased]

### 추가
- `README.md`: 하는 일, 준비물, 설치·실행·시험 방법, 설정, 폴더 구조, 한계. `--dry-run`을 먼저 쓰도록 안내한다.
- 별도 발송 CLI의 선택 이유·유지보수 한계와 LangGraph 노드 통합의 미적용 대안을 문서화하고, 실제 결과 보존·오늘 종료·미완료 제출 항목 기록(프로그램 동작 변경 없음)
- 실사용 출력에서 자동 검수가 놓친 비교 대상·제공 범위 사례2건 기록(`eval/검수_누락_사례.json`). 당시 요약·통과 판정·원문 근거를 보존하며 워크플로·기존 결과·게시물은 변경하지 않음
- 실제 Discord 승인 발행·메시지 재조회 대조 증거(`store/발행_확인.json`)
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
- `REPORT.md`를 발송 노드 통합 구조에 맞춰 갱신: 구조도·노드 표·예외 처리 표·4절 발송 경로 설명·재현 방법·파일 구성을 새 명령 기준으로 고치고, 2026-11-01 실제 전체 실행 기록을 5절에 추가했다(1절·6절의 사용자 작성 문장은 그대로).
- 통합 시험(`테스트_명령통합.py`)이 환경변수 `TMPDIR` 없이도 시스템 기본 임시 폴더로 실행되도록 수정(없으면 `KeyError`로 실패하던 문제).
- 발행을 그래프 안으로 통합: `publish` 노드를 `prepare_publication`(보낼 내용을 불변 준비본으로 저장)과 `send_publication`(저장한 내용 그대로 Discord 전송·메시지 재조회)으로 나누고, 그래프 하나가 수집부터 발송까지 끝까지 실행한다(명세서 11절).
- `python run.py`의 기본 동작을 자동 발송으로 변경. 보내지 않고 준비본까지만 만드는 `--dry-run`을 추가했다. 발행 기록 이벤트는 `publication_prepared`·`publication_sent`로 통일했다.
- 웹훅이 설정되지 않았으면 유료 모델 호출 전에 `webhook_not_configured`로 중단한다.
- 시험 갱신: 발송기 대역(`FakePublisher`)이 실전 계약을 갖추도록 하고, 승인용 CLI 시험을 기본 발송·`--dry-run` 시험으로 바꿨다. 준비본은 시험에서 임시 폴더에만 쓴다.
- 실제 실행에서 드러난 검수 오탐을 줄이도록 원문 사실·번역과 독자 해석의 지시를 구분하고, 탭·미처리 수식 명령은 형식 검사에서 차단
- 사용자 분야·독자 설명과 회고, 실제 선별 평가·오류 삽입 검수·실행 기록을 REPORT에 반영. 문장 수는 사용자 선택으로 기존2~4문장 허용 유지
- 즉시 재생성·발송하는 `--publish`를 차단하고 준비본 조회·확인 해시를 사용한 발송으로 분리
- Discord 멘션 기본 허용을 명시적으로 끄고 오류·재조회 결과에서 비밀 웹훅 주소 노출 차단
- 라벨링 위젯: 제출 버튼을 맨 위로 옮기고, 대화창 밖에서 열어 이미 25건 이상 골라 둔 상태면 수신기로 자동 제출. 실패하면 복사 칸을 보여 줌
- `requirements.txt`를 시험한 버전(Python 3.12.10)으로 고정
- OpenAI 실제 요청 누적 상한을 65→95로 상향(사용자 지시, 실제 전체 실행 시험용). 장부의 `used`는 초기화하지 않고 cap만 같은 값으로 올렸다.

### 제거
- 사람이 해시를 입력해 승인하던 명령 `--send-prepared`·`--confirm-hash`·`--publish`와 `run.py`의 `prepare_result`. 발송 실패 뒤 같은 준비본을 다시 보내는 복구 경로는 아직 없다(명세서 11.10).
