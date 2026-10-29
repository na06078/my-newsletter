실측 시각 2026-10-29T12:14:43+09:00 · 요청 50건 · 본문 기준 600자 · 소스당 본문 표본 3건 · User-Agent `newsletter-agent-course/0.1 (personal learning project)`

| 소스 | 경로·tier | HTTP | 항목 | 24h/72h/7d | 최신(h) | 본문 | robots | 키워드 | 관문 | 판정 | 근거 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| arXiv API (cs.CL, keyword agent) | API(arXiv) · 1 | 200 | 30 | 0/30/30 | 33.3 | 3/3 | n/a(API 약관  | 0.8 | G1o G2o G3o | **채택** | API 경로. 초록(abstract)이 응답에 들어 있어 본문 확보가 확실하다. 논문 위주라 선별에서 걸러질 가능성이 높아 소스당 상한을 둔다. |
| GitHub Releases langchain-ai/langgraph | GitHub Atom · 1 | 200 | 10 | 0/5/8 | 55.2 | 2/3 | allowed | 1.0 | G1o G2o G3o | **채택** | 1차 소스. 독자가 직접 쓰는 도구라 키워드 일치율 1.00. 발행량은 적고(오늘 48시간 창에는 0건) 릴리스 노트는 피드 본문을 우선 쓴다. |
| Hugging Face Blog | RSS · 1 | 200 | 891 | 1/3/6 | 12.3 | 3/3 | allowed | 0.17 | G1o G2o G3o | **채택** | 1차 소스. 오픈 모델·도구의 직접 발표가 실린다. 관문 세 개 통과. |
| Hacker News (Algolia API, query llama.cpp) | API(HN) · 2 | 200 | 30 | 2/3/4 | 6.9 | 3/3 | n/a(API 약관  | 0.75 | G1o G2o G3o | **채택** | API 경로. 쿼리 llama.cpp 하나 기준 7일 4건이지만 키워드 일치율 0.75. 운영에서는 쿼리 3개(llama.cpp·langgraph·local llm)를 합친다. |
| Latent Space | RSS · 2 | 200 | 20 | 2/6/12 | 7.4 | 3/3 | allowed | 0.42 | G1o G2o G3o | **채택** | 2차 소스. AI 엔지니어링 주간 정리. 키워드 일치율이 높은 편(0.42). 관문 통과. |
| Simon Willison's Weblog | Atom · 2 | 200 | 30 | 4/8/15 | 3.9 | 3/3 | allowed | 0.2 | G1o G2o G3o | **채택** | 2차 소스. 7일 15건으로 발행 빈도가 가장 높고 모델·도구의 실사용 후기가 많다. 관문 통과. |
| The Decoder | RSS · 2 | 200 | 10 | 8/10/10 | 7.9 | 3/3 | allowed | 0.5 | G1o G2o G3o | **채택** | 2차 소스. 범용 AI 뉴스 보강용. 피드 10건이 전부 72시간 안이라 하루치를 다 담지 못할 수 있다(포화). 관문 통과. |
| AMD ROCm Blogs | RSS · 1 | 404 | — | -/-/- | — | — | allowed | — | G1x G2x G3x | **탈락** | 피드 주소가 HTTP 404. 대체 주소는 확인하지 못했다. |
| GeekNews | RSS · 2 | 200 | 50 | 47/50/50 | 0.6 | 0/3 | allowed | 0.16 | G1x G2o G3o | **탈락** | 피드는 정상(50건, 24시간 47건)이지만 기사 페이지 표본 3건이 모두 HTTP 403 이라 본문을 받을 수 없다. |
| GitHub Releases ggml-org/llama.cpp | GitHub Atom · 1 | 200 | 10 | 10/10/10 | 0.7 | 2/3 | allowed | 0.5 | G1o G2o G3o | **탈락** | 피드 10건이 전부 24시간 안이라 하루치를 담지 못한다(포화). 글자 수 관문은 2/3 로 통과했지만 추출문을 직접 열어 보니 변경 설명 한 줄과 빌드 파일 목록·로그인 배너뿐이라 요약할 본문이 아니다(수치와 실제 내용의 괴리). |
| GitHub Releases ROCm/ROCm | GitHub Atom · 1 | 200 | 10 | 0/0/0 | 2519.6 | 2/3 | allowed | — | G1o G2x G3o | **탈락** | 최근 7일 0건, 최신 글이 약 105일 전(2,520시간)이다. 정체된 소스. |
| LangChain Blog | RSS · 1 | 200 | 0 | 0/0/0 | — | 0/0 | no_robots | — | G1x G2x G3o | **탈락** | HTTP 200 이지만 피드가 아닌 HTML(약 212KB)이 돌아와 항목 0건. 추측한 주소가 틀렸을 수 있으며 대체 주소는 확인하지 못했다. |
| Reddit r/LocalLLaMA | Atom · 2 | — | — | -/-/- | — | — | disallowed | — | G1x G2x G3x | **탈락** | robots.txt 가 자동 접근을 금지(disallowed)한다. 허용되지 않은 접근이라 요청을 보내지 않았다. |
