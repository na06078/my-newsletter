# 나만의 뉴스레터 에이전트

AI 에이전트·로컬 LLM 개발 소식을 모아 독자 기준에 맞는 3~5건을 고르고, 본문을 읽어 요약·인사이트를 만든 뒤, 원문과 대조해 통과한 것만 Discord 로 발행하는 LangGraph 에이전트입니다. `python run.py` 한 번으로 수집부터 발행까지 사람 개입 없이 끝납니다.

설계 판단·소스 채택 근거·평가 수치·한계는 [REPORT.md](REPORT.md) 에 있습니다.

## 하는 일

| 단계 | 노드 | 내용 |
|---|---|---|
| ① 수집 | `collect` | 소스 7곳(RSS·Atom·GitHub Releases·Hacker News·arXiv)에서 시간 창·중복·제외 도메인을 걸러 후보를 만듭니다 |
| ② 선별 | `select` | 예선(묶음별)→본선 2단계로 3~5건을 고르고, 같은 소스·같은 사건은 코드 규칙으로 제한합니다 |
| ③ 요약·인사이트 | `report` | 본문을 확보해 headline·summary·why(독자 관점의 의미)를 만듭니다 |
| ④ 검수 | `verify` | 형식·숫자 대조 후 원문 대조 모델이 판정합니다. 불합격이면 사유를 붙여 1회 재생성하고, 그래도 불합격이면 그 기사를 스킵합니다 |
| ⑤ 발행 | `prepare_publication`, `send_publication` | 보낼 내용을 불변 준비본(SHA256)으로 저장한 뒤 그대로 Discord 로 전송하고, 올라간 메시지를 다시 읽어 일치를 확인합니다 |

## 준비물

- Python 3.12 (3.12.10 에서 시험)
- OpenAI API 키 — 모델은 `gpt-4o-mini` (`config.yaml` 에서 바꿀 수 있습니다)
- Discord 웹훅 주소 — 채널 설정 → 연동 → 웹후크

## 설치

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows (macOS·Linux: source .venv/bin/activate)
pip install -r requirements.txt
cp .env.example .env            # OPENAI_API_KEY, DISCORD_WEBHOOK_URL 입력
```

`.env` 는 Git 에 올라가지 않습니다. 키 값은 코드·명령행·로그에 적지 않습니다.

## 실행

```bash
python run.py                          # 수집→선별→요약→검수→Discord 발송·재조회까지 자동
python run.py --dry-run                # Discord 에만 보내지 않고 준비본까지 만들어 내용·SHA256 확인
python run.py --show-prepared RUN_ID   # 저장된 준비본을 다시 보기 (읽기 전용, 모델 호출 없음)
python run.py --hours 24               # 수집 시간 창 변경 (기본 48시간)
```

- `python run.py` 는 **실제로 OpenAI 를 호출하고 Discord 채널에 게시합니다.** 처음 써 볼 때는 `--dry-run` 을 쓰세요.
- `--dry-run` 도 수집과 모델 호출은 실제로 합니다. 보내지 않는 것은 Discord 전송뿐이고, `DISCORD_WEBHOOK_URL` 이 없어도 실행됩니다.
- 웹훅이 없는데 `--dry-run` 없이 실행하면 모델을 부르기 전에 `webhook_not_configured` 로 멈춥니다.
- 종료 코드: `0` 정상 · `1` 실행 오류 · `2` 발송 실패 또는 재조회 불일치.
- 실행 기록은 `store/metrics.jsonl` 에 한 줄씩 쌓이고, 준비본·전송 결과는 `store/cache/publications/` (Git 제외)에 저장됩니다.

## 설정

| 파일 | 내용 |
|---|---|
| `config.yaml` | 소스 목록, 모델명·주소, 묶음 크기, 검수 규칙, 요청 상한 등 운영 설정 |
| `audience.yaml` | 타깃 독자, 중요도 판단 문장, 제외 조건, 문체 |
| `.env` | `OPENAI_API_KEY`, `DISCORD_WEBHOOK_URL` (`.env.example` 참고) |

`config.yaml` 의 `llm.request_limit` 은 이 PC 의 `store/호출예산.sqlite3` (Git 제외)에 누적하는 실제 OpenAI 요청 상한입니다. 실패·재시도도 한 번으로 셉니다. 장부 파일이 없는 새 환경에서는 처음부터 새로 시작합니다.

## 시험

```bash
python -m unittest tests.test_pipeline tests.test_rules tests.test_tools tests.테스트_호출예산 tests.테스트_발행승인 tests.테스트_실행승인 tests.테스트_연결확인 tests.테스트_검수기준 tests.테스트_명령통합 tests.테스트_수집경계 tests.테스트_모델발행경계 tests.테스트_도구경계
```

- 모듈을 직접 적어야 합니다. `python -m unittest` 만 실행하면 한국어로 시작하는 파일을 찾지 못해 일부만 실행됩니다.
- 2026-11-02 보완본은 141건입니다. 실제 OpenAI·Discord 를 호출하지 않고, 설치 SDK·합성 로컬 HTTP 서버·격리 저장소를 사용합니다. 모델의 판단 품질은 이 시험이 증명하지 않습니다(`tools/negative_control.py`, `tools/eval_selection.py` 가 따로 잽니다).

## 수집·검수·평가 보호

- 검수 모델이 `ok=true`라고 해도 `problems`에 문제가 있으면 불합격입니다. 잘못된 구조화 응답은 안전한 모델 오류로 처리하고, 요청 예산·장부 오류는 전체 실행을 중단합니다.
- 제외 도메인은 포트·하위 도메인·후행 점과 리다이렉트에서도 검사합니다. 본문 이동 대상의 robots.txt도 확인하며, 5xx·통신 장애로 확인하지 못하면 그 기사를 건너뜁니다. robots 파일이 없는 4xx와는 구별합니다.
- 응답은 수신 중 크기를 제한합니다(피드 8 MiB·본문 2 MiB·robots 512 KiB, 리다이렉트 최대 5회). 본문은 HTML·XHTML·일반 텍스트만 사용합니다. 이는 본문 요약용 6,000자 제한과 별개입니다.
- 한 소스 안에서 중복 기사를 제거한 뒤 최신 기사 상한을 적용합니다. Discord 429도 자동 재시도하지 않고 실패로 기록합니다.
- 선별 평가는 후보·라벨의 표본 시각, ID, 정수 라벨, 최소 25건을 모델 호출 전에 확인합니다. 검수 시험의 통신 장애는 적발이나 오탐으로 세지 않고 `errors`·실제 판정 수 `evaluated`로 구별합니다.
- 새 평가 결과는 `eval/selection_eval-<실행ID>.json`·`eval/negative_control-<실행ID>.json`에 별도로 남깁니다. 과거 `selection_eval.json`·`negative_control.json`은 덮어쓰지 않습니다. 소스 부분 재측정 표에는 소스별 측정 시각을 표시합니다.
- 라벨 저장 후에는 같은 표본의 기사 내용을 직접 고치지 마세요. 수신 시점은 내용 해시를 검사하지만, 기존 라벨→평가는 시각·ID를 기준으로 연결하며 과거 라벨에 내용 해시를 소급해 보장하지 않습니다.

### 라벨 위젯을 다시 사용할 때

`tools/make_label_widget.py`로 새 HTML을 만든 뒤 `tools/label_receiver.py`를 실행하고 같은 PC의 브라우저에서 위젯을 엽니다. 위젯 생성 이후 30분 동안만 제출할 수 있으며, 현재 표본 내용과 연결된 임의 세션을 검사합니다. `store/cache/label_sessions/`와 생성 HTML에 제출용 토큰이 있으므로 공유하거나 Git에 추가하지 않습니다. 기본 생성본 `eval/label_widget.html`은 Git에서 제외합니다.

기존 위젯 파일과 `eval/labels.json`은 덮어쓰지 않습니다. 기존 위젯이 있으면 `--out`으로 새 HTML 경로를 지정하세요. 이미 라벨이 있으면 `labels_exist`로 거부하므로, 기존 표본을 다시 라벨링하려면 먼저 보존·별도 결과 위치를 정해야 합니다. 기존 라벨을 임의 삭제하지 마세요. 수신기는 요청 크기 128 KiB·연결 시간 5초·전체 대기 30분을 제한하고, 정상 저장 후 종료합니다. 브라우저 자동 제출이 실패하면 기존 수동 복사 경로를 사용할 수 있으며 복사 내용에는 세션 토큰이 없습니다.

## 폴더 구조

```text
my-newsletter/
├── graph.py            # State, 노드, 코드가 확정하는 규칙, 그래프 조립
├── run.py              # 실행 스크립트
├── audience.yaml       # 독자·중요도 기준·제외 조건
├── config.yaml         # 운영 설정
├── requirements.txt    # 버전을 고정한 의존성
├── collectors.py · net.py · llm.py · metrics.py · publisher.py · config.py
├── 발행승인.py · 호출예산.py   # 불변 준비본·일회 전송·재조회 / 요청 상한 장부
├── tools/              # 소스 실측, 표본 추출, 라벨링 위젯, 선별 평가, 검수 시험
├── eval/               # 정답 라벨 30건과 평가 결과
├── store/              # metrics.jsonl, 소스 실측 결과, 구조도(Mermaid)
├── tests/
└── REPORT.md
```

## 한계

- 예약 자동 실행(매일 정해진 시각)은 만들지 않았습니다. 지금은 명령을 직접 실행합니다.
- 발송이 실패하면 같은 준비본을 다시 보내는 경로가 없어, 복구하려면 전체를 다시 생성해야 합니다.
- 자동 검수가 모든 오류를 잡는 것은 아닙니다. 놓친 사례는 `eval/검수_누락_사례.json` 에 있습니다.
- 정답 라벨 30건은 통계적 입증이 아니라 동작 확인용입니다.

자세한 내용과 근거는 [REPORT.md](REPORT.md) 의 부록 C 를 보세요.
