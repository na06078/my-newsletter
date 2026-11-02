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
- 종료 코드: `0` 정상 · `1` 실행 오류 · `2` 발송 실패·재조회 불일치·전송 결과 기록 실패. `--hours`는 양수만 허용합니다.
- 메시지를 보낸 뒤 전송 결과 기록에 실패해도 이미 알려진 메시지 ID와 상태를 보여 주며 자동 재발송하지 않습니다. 선택적인 검수 시험용 캐시만 저장하지 못한 경우에는 경고와 함께 알려진 발행 성공을 유지합니다.
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
python -m unittest tests.test_pipeline tests.test_rules tests.test_tools tests.테스트_호출예산 tests.테스트_발행승인 tests.테스트_실행승인 tests.테스트_연결확인 tests.테스트_검수기준 tests.테스트_명령통합 tests.테스트_수집경계 tests.테스트_모델발행경계 tests.테스트_도구경계 tests.테스트_불변저장 tests.테스트_선별오류 tests.테스트_응답복구 tests.테스트_평가보존 tests.테스트_통신공통화 tests.테스트_새표본연결 tests.테스트_실행효율 tests.테스트_접근정책동시조회
```

- 모듈을 직접 적어야 합니다. `python -m unittest` 만 실행하면 한국어로 시작하는 파일을 찾지 못해 일부만 실행됩니다.
- 2026-11-02 2차 전체 단위는20모듈224건 OK입니다(기준141건 유지·신규83건). 안정성 단위206건과 성능 단위의 추가18건을 포함합니다. 실제 OpenAI·Discord 를 호출하지 않고 설치 SDK·합성 로컬 HTTP 서버·격리 저장소를 사용합니다. 모델의 판단 품질은 이 시험이 증명하지 않습니다. 상세 범위는 REPORT5절에 있습니다.

## 수집·검수·평가 보호

- 검수는 현재 회차만 기본2개 worker로 처리합니다. `config.yaml`의 기존 `verify` 아래 `max_concurrency`를 선택적으로 추가하면 정수1~4를 사용할 수 있습니다(1은 직렬). 원본 설정 파일은 변경하지 않았으며 항목이 없으면 기본2입니다. 결과는 입력 순서로 모으고 예산 소진은 전체 중단을 유지합니다.
- 재생성은 같은 실행의 해당 기사·실패 회차에서 확보한 본문을 재사용합니다. 사이트를 다시 읽거나 전역 캐시를 만들지 않으므로 같은 근거로 교정하며, 새 실행은 다시 수집합니다. 같은 origin/제외 정책의 robots 캐시 채우기만 한 번씩 묶고 다른 origin은 병렬로 허용합니다.

- 검수 모델이 `ok=true`라고 해도 `problems`에 문제가 있으면 불합격입니다. 잘못된 구조화 응답은 안전한 모델 오류로 처리하고, 요청 예산·장부 오류는 전체 실행을 중단합니다.
- 모델 응답의 JSON 해독·빈 choices·잘못된 메시지 형식도 안전한 모델 오류로 처리합니다. 빈·무효 선별은 독자에게 맞는 뉴스가 없다는 뜻으로 표시하지 않고 최신순 대체로 구별하며, 본문 실패 원인도 최종 제외 사유에 남깁니다.
- 제외 도메인은 포트·하위 도메인·후행 점과 리다이렉트에서도 검사합니다. 본문 이동 대상의 robots.txt도 확인하며, 5xx·통신 장애로 확인하지 못하면 그 기사를 건너뜁니다. robots 파일이 없는 4xx와는 구별합니다.
- 응답은 수신 중 크기를 제한합니다(피드 8 MiB·본문 2 MiB·robots 512 KiB, 리다이렉트 최대 5회). 본문은 HTML·XHTML·일반 텍스트만 사용합니다. 이는 본문 요약용 6,000자 제한과 별개입니다.
- 운영 수집과 소스 조사는 같은 통신 보호를 사용합니다. 유휴 통신 timeout과 별도로 기본 전체 기한60초를 적용하며 헤더·느린 본문·이동·중첩 robots 대기를 포함해 취소합니다. 내부 전송은 설치된 HTTPX의 재사용 비동기 클라이언트이며 외부 호출 방식은 동기 그대로입니다. gzip/deflate만 지원하고 암묵적 환경 프록시는 사용하지 않습니다.
- 한 소스 안에서 중복 기사를 제거한 뒤 최신 기사 상한을 적용합니다. Discord 429도 자동 재시도하지 않고 실패로 기록합니다.
- 선별 평가는 후보·라벨의 표본 시각, ID, 정수 라벨, 최소 25건을 모델 호출 전에 확인합니다. 검수 시험의 통신 장애는 적발이나 오탐으로 세지 않고 `errors`·실제 판정 수 `evaluated`로 구별합니다.
- 선별 평가의 기사 나이는 표본 생성 시각으로 고정합니다. `summary`는 모델 선별 반복만, `fallback_summary`는 장애 대체 반복만 집계하며 `selection_status_counts`·반복별 대체 사유를 함께 남깁니다. `--repeats`·`--limit`도 양수만 허용합니다.
- 새 평가 결과는 `eval/selection_eval-<실행ID>.json`·`eval/negative_control-<실행ID>.json`에 별도로 남깁니다. 과거 `selection_eval.json`·`negative_control.json`은 덮어쓰지 않습니다. 소스 부분 재측정 표에는 소스별 측정 시각을 표시합니다.
- 라벨 저장 후에는 같은 표본의 기사 내용을 직접 고치지 마세요. 수신 시점은 내용 해시를 검사하지만, 기존 라벨→평가는 시각·ID를 기준으로 연결하며 과거 라벨에 내용 해시를 소급해 보장하지 않습니다.

### 라벨 위젯을 다시 사용할 때

기존 기본 표본·라벨·위젯은 덮어쓰지 않습니다. 새 표본을 쓰려면 다음처럼 같은 후보 경로와 새 라벨 경로를 이어 사용합니다. 아래 수집은 실제 소스에 요청하고, 마지막 평가는 실제 모델을 호출합니다.

```bash
python tools/snapshot_candidates.py --out eval/새표본.json
python tools/make_label_widget.py --candidates eval/새표본.json --out eval/새위젯.html
python tools/label_receiver.py --candidates eval/새표본.json --out eval/새라벨.json
# 같은 PC 브라우저에서 eval/새위젯.html을 열어 25건 이상 고른 뒤 제출
python tools/eval_selection.py --candidates eval/새표본.json --labels eval/새라벨.json --repeats 3
```

위젯 생성 이후 30분 동안만 제출할 수 있으며 현재 표본 내용과 연결된 임의 세션을 검사합니다. `store/cache/label_sessions/`와 생성 HTML에 제출용 토큰이 있으므로 공유하거나 Git에 추가하지 않습니다. `eval/` 하위 생성 HTML 전체는 Git에서 제외하며, 다른 폴더로 출력한 HTML도 직접 공유·추가하지 마세요.

기존 위젯 파일과 `eval/labels.json`은 덮어쓰지 않습니다. 기존 위젯이 있으면 `--out`으로 새 HTML 경로를 지정하세요. 이미 라벨이 있으면 `labels_exist`로 거부하므로, 기존 표본을 다시 라벨링하려면 먼저 보존·별도 결과 위치를 정해야 합니다. 기존 라벨을 임의 삭제하지 마세요. 수신기는 요청 크기 128 KiB·연결 시간 5초·전체 대기 30분을 제한하고, 정상 저장 후 종료합니다. 브라우저 자동 제출이 실패하면 기존 수동 복사 경로를 사용할 수 있으며 복사 내용에는 세션 토큰이 없습니다.

표본과 라벨은 완성본만 원자적으로 게시하므로 쓰기 중 실패해도 불완전한 최종 파일로 재제출을 막지 않습니다. 저장 성공 후 선택/제출은 잠기며, 다시 열면 이전 브라우저 제출 기록임을 안내합니다. 이 안내는 서버 저장본을 새로 조회했다는 뜻이 아닙니다.

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
├── 불변저장.py          # 완성된 바이트를 기존 파일 교체 없이 원자적으로 게시
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
