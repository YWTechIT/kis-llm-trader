# KIS LLM 자동매매 봇 — 전체 스켈레톤 구현 계획

## Context

"디테일한 매매 전략을 주면 LLM이 사람보다 수익을 잘 낼까?"를 검증하는 토이프로젝트. 실자금 ~50만원 소액 운용, 학습·재미 목적.

핵심 설계(확정):
- LLM은 **BUY/SELL/HOLD 중 선택만**. 매매 위임 아님.
- 손절·한도·횟수 등 **안전장치는 전부 코드가 강제**.
- KIS 연동은 공식 `koreainvestment/open-trading-api`를 vendoring.
- 알림은 디스코드 Webhook. 기록은 SQLite. 모의투자(`vps`) 먼저, 실전(`prod`)은 검증 후.

**세션 결정사항(사용자 확정):**
1. 빌드 위치: **현재 디렉토리** `~/Desktop/open-trading-bot` (git init 포함).
2. 범위: **전체 스켈레톤** — 모든 모듈을 실제 동작 코드로 작성. 단, 실 크리덴셜이 필요한 모의투자 E2E 검증은 코드 완성 후 ted님이 직접 실행.
3. 크리덴셜 주입: **.env → kis_devlp.yaml 자동생성**. 비밀값은 `.env`에만, `config.py`가 기동 시 `kis_devlp.yaml` 생성해 vendor `kis_auth`에 넘김. `.env`/`kis_devlp.yaml`/토큰캐시/`*.db` 전부 gitignore.

### 조사로 확인된 공식 레포 사실 (계획 정확도 근거)
- 공식 인증: `import kis_auth as ka; ka.auth(svr="vps")` (모의) / `"prod"` (실전), 웹소켓은 `ka.auth_ws()`.
- `kis_auth.py`는 **import 시점에 `~/KIS/config/kis_devlp.yaml`에서 앱키/시크릿/HTS ID/계좌번호를 읽고**, access token을 디스크에 캐시(분당 재발급 1회 제한).
- 국내주식 REST: `examples_user/domestic_stock/domestic_stock_functions.py` (현재가 `inquire_price`, 주문 `order_cash`, 잔고 `inquire_balance`, 체결 `inquire_daily_ccld`).
- 국내주식 WS: `examples_user/domestic_stock/domestic_stock_functions_ws.py` + `ka.KISWebSocket(api_url="/tryitout")` → `subscribe(request=ccnl_krx, data=[...])` → `start(on_result=...)`.

---

## 디렉토리 구조

```
open-trading-bot/
├─ .env.example              # 키 자리표시자만 (실값 금지)
├─ .gitignore                # .env, kis_devlp.yaml, *.db, 토큰캐시, __pycache__
├─ pyproject.toml            # uv 의존성
├─ README.md                 # 셋업·실행·검증 절차
├─ docs/plan.md              # (이 문서)
├─ config.py                 # .env 로딩+검증, .env→kis_devlp.yaml 생성, 설정 객체 제공
├─ kis_bootstrap.py          # config→vendor/kis 로드 순서를 강제하는 단일 진입점
├─ main.py                   # 장중 메인 루프(스케줄러) + 그레이스풀 종료
├─ reference/open-trading-api/  # 공식 레포 (git submodule, 참조 전용 / import 금지)
├─ vendor/kis/               # reference에서 실제 쓰는 파일만 복사 (앱이 import)
│   ├─ kis_auth.py
│   ├─ domestic_stock_functions.py
│   ├─ domestic_stock_functions_ws.py
│   └─ VENDORING.md          # 원본 경로·복사 시점 커밋 기록
├─ adapter/
│   ├─ broker.py             # vendor 래핑: get_price/get_balance/place_order/get_filled (try/catch·throttle·backoff)
│   └─ market_stream.py      # 웹소켓 실시간 시세 + 자동 재연결 + 하트비트
├─ strategy/
│   ├─ context.py            # 전략 명세(상세 룰) + 시세/포지션/현금 스냅샷 구성
│   └─ llm_decider.py        # Anthropic tool use → {action, quantity, reason} 강제
├─ risk/
│   └─ guardrails.py         # 코드 강제 안전장치(최종 검문대)
├─ notify/
│   └─ discord.py            # Webhook embed 전송
├─ store/
│   └─ journal.py            # SQLite: decisions/orders/fills/daily_pnl
└─ tests/
    └─ test_guardrails.py    # 가드레일 단위테스트(크리덴셜 불필요)
```

---

## 구현 상세 (파일별)

### 0. 골격
- `pyproject.toml`(uv, py3.11+): 의존성 `anthropic`, `requests`, `python-dotenv`, `pyyaml`, `websockets`(공식 WS 의존), `pycryptodome`(공식 `kis_auth`의 `Crypto` 의존), `pandas`(공식 함수 의존). `sqlite3`는 표준.
- `.gitignore`: `.env`, `kis_devlp.yaml`, `*.db`, 토큰캐시, `__pycache__/`, `.venv/`.
- `.env.example`: `KIS_ENV`, `KIS_PAPER_APP_KEY/SECRET`, `KIS_PROD_APP_KEY/SECRET`, `KIS_HTS_ID`, `KIS_ACCOUNT_8`, `KIS_ACCOUNT_PD`, `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL=claude-haiku-4-5`, `DISCORD_WEBHOOK_URL`, 가드레일 파라미터(`MAX_ORDER_KRW`, `MAX_POSITION_PCT`, `DAILY_MAX_LOSS_KRW`, `MAX_TRADES_PER_DAY`, `HARD_STOP_LOSS_PCT`), `WATCH_CODES`, `DECISION_INTERVAL_MIN`, `JOURNAL_DB`.

### 1. KIS submodule + vendoring + config
- `git submodule add https://github.com/koreainvestment/open-trading-api reference/open-trading-api`.
- `kis_auth.py` + `domestic_stock_functions.py` + `domestic_stock_functions_ws.py`를 `vendor/kis/`로 **무수정 복사**. 세 파일이 서로 `import kis_auth as ka`로 참조하므로 `kis_bootstrap.py`가 `sys.path`에 `vendor/kis`를 삽입.
- `config.py`:
  - `python-dotenv`로 `.env` 로드, 필수 키 누락 시 **명확한 메시지로 기동 거부**.
  - `KIS_ENV`(vps/prod)에 따라 해당 앱키/시크릿 선택.
  - `.env` 값으로 vendor `kis_auth`가 기대하는 `kis_devlp.yaml`(키: `paper_app/paper_sec/my_app/my_sec/my_htsid/my_paper_stock/my_acct_stock/my_prod/prod/vps/ops/vops/...`)을 **`~/KIS/config/`에 생성**(파일 권한 0600). kis_auth가 import 시점에 읽으므로 `kis_bootstrap`이 config를 먼저 import해 순서 보장.
  - 크리덴셜은 **로그/print/디스코드 어디에도 출력 금지**.

### 2. `adapter/broker.py` — KIS 래퍼
- 기동 시 `ka.auth(svr=settings.kis_env)` 1회. **모든 KIS REST 호출 try/catch는 이 계층에서만.**
- 노출: `get_price(code)`, `get_balance()`, `place_order(code, side, qty)`, `get_filled(...)`.
- 초당 호출 제한 대응 throttle + 지수 backoff 재시도, 토큰 만료 감지 시 재인증. 실패 시 디스코드 경고 후 예외 전파.

### 3. `adapter/market_stream.py` — 실시간 스트림
- `ka.auth_ws()` + `ka.KISWebSocket` + `ccnl_krx`로 `WATCH_CODES` 실시간 체결가 구독.
- 별도 스레드 + 외곽 supervisor 루프로 자동 재연결(backoff) + 하트비트 + **끊김 시 디스코드 경고**. 최신가를 스레드 안전 스냅샷으로 노출.

### 4. `risk/guardrails.py` — 코드 강제 안전장치 (LLM 위 최종 검문대)
순수 함수(테스트 용이). 주문 직전 `check(decision, state, cfg, price) -> GuardrailResult`:
- 1회 매수금액 한도 / 종목당 최대 비중 → 초과 시 한도 내 수량 자동 축소(0이면 차단).
- 하루 최대 거래 횟수 초과 차단.
- 일일 최대 손실 도달 시 **당일 매매 중단(kill switch)**.
- 하드 손절 도달 → **LLM 의견 무시 즉시 청산**(별도 함수 `force_stop_loss(state, cfg)`).
- 멱등성(같은 종목/방향 중복 주문 방지), 장운영시간 외 주문 차단.
- 모든 차단/축소는 사유와 함께 반환 → 디스코드 알림 + journal 기록.

### 5. `strategy/context.py` + `strategy/llm_decider.py`
- `context.py`: 상세 전략 명세(진입/청산/분할매수 룰) 텍스트 + 시세 스냅샷 + 보유 포지션 + 가용 현금 구성.
- `llm_decider.py`: Anthropic SDK, 기본 `claude-haiku-4-5`, **프롬프트 캐싱 적용**(전략 명세 블록 `cache_control`). **tool use(`tool_choice`)로 출력 스키마 강제**: `{action: BUY|SELL|HOLD, quantity: int, reason: str}`. 스키마 외/파싱 실패/API 오류 시 **안전하게 HOLD 폴백**. 호출은 `DECISION_INTERVAL_MIN` 간격에만(매 틱 금지). 프롬프트·모델·응답·스냅샷 전부 journal 기록.

### 6. `notify/discord.py`
- `requests` POST embed 카드. 이벤트: 매수/매도 체결, 손절 발동, 일일 요약, 에러/웹소켓 끊김. 전송 실패도 try/catch(알림 실패가 메인 루프를 죽이지 않게). 메시지에 **크리덴셜 절대 미포함**.

### 7. `store/journal.py`
- SQLite 테이블: `decisions`(LLM 판단), `orders`/`fills`(주문·체결), `daily_pnl`. 모든 쓰기 try/catch.

### 8. `main.py` — 메인 루프
- 기동: config 검증 → yaml 생성 → broker auth → 스트림 시작 → journal 초기화 → 디스코드 "기동" 알림.
- 루프: 스냅샷 갱신 → **매 사이클 손절 우선** → (간격 도달 시) LLM decider 호출 → **guardrails 검문** → 통과 시 broker 주문 → journal + 디스코드.
- 장 마감 시 일일 요약 알림 + 종료. SIGINT/SIGTERM 그레이스풀 종료. 최상위 try/except로 미처리 예외도 디스코드 경고 후 안전 종료.

---

## 구현 순서
1. 골격(pyproject/.env.example/.gitignore/README/config) + `git init`.
2. submodule 추가 → vendor 복사 → `kis_auth.py` yaml 키 매핑 확정 → config + kis_bootstrap 완성.
3. broker 래퍼.
4. guardrails (LLM 없이 단위 동작 + 테스트).
5. discord + journal.
6. context + llm_decider (tool use).
7. market_stream.
8. main 루프 통합.

---

## 검증 방법 (코드 완성 후 ted님이 실행)
- **인증/시세:** `KIS_ENV=vps`로 `uv run python -m adapter.broker --smoke 005930`.
- **모의 주문:** `place_order`로 모의 매수/매도 1건 체결.
- **가드레일(크리덴셜 불필요):** `uv run pytest` — 한도 초과→축소/차단, 횟수 초과→차단, 손절가 도달→강제청산, 중복주문→차단.
- **LLM 폴백:** 스키마 외 응답 시 HOLD 처리 확인.
- **장애주입:** 웹소켓 강제 종료 → 자동 재연결 + 디스코드 경고.
- **최종:** 모의투자 1일 풀 사이클(기동→모니터링→판단→주문→알림→일일요약) 무인 실행 후 journal/디스코드 점검.
- 실전(`prod`, 50만원) 전환은 **모의 검증 통과 후에만**.

---

## 주의
- 모의 먼저, 실전은 검증 후. 크리덴셜은 `.env`에만 — 코드/로그/디스코드 출력 금지.
- 무인 매매이므로 모든 외부 호출(REST·WS·LLM·디스코드)에 에러핸들링·재시도·실패 알림 필수.
- 공식 샘플 면책: 운용 책임은 본인.

---

## 구현 시 확정/변경된 사항 (계획 대비)
- **의존성:** 계획의 `websocket-client` → 실제는 `websockets`(공식 `kis_auth`가 async `websockets` 사용) + `pycryptodome`(`Crypto.Cipher` 의존) 추가.
- **kis_devlp.yaml 경로:** 공식 `kis_auth`가 import 시점에 하드코딩된 `~/KIS/config/kis_devlp.yaml`을 읽으므로, `config.py`가 그 경로에 생성. vendor 파일은 **무수정 유지**(업데이트 추적 용이), 순서 보장은 `kis_bootstrap.py`가 담당.
- **vendored 커밋:** `vendor/kis/VENDORING.md`에 원본 경로 + 복사 시점 submodule 커밋 해시 기록.
- **현재 미구현(TODO):** `main.py`의 `realized_pnl_today`가 `0` 고정 → **일일 손실 kill switch는 체결 기반 당일 실현손익 집계를 붙이기 전까지 발동하지 않음.** 나머지 가드레일(1회/비중/횟수 한도, 하드 손절, 멱등성, 장시간)은 동작.
