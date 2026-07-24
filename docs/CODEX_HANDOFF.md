# Codex 개발 인수인계

마지막 갱신: 2026-07-25
저장소: `easyseop/Stock-chart-analyze`

이 문서는 다른 노트북이나 새 Codex 작업에서 개발을 바로 이어가기 위한 현재 상태,
검증 결과, 미완료 작업과 운영 주의사항을 기록한다. API 키·계좌번호·토큰·SSH
개인키 등 비밀값은 이 문서와 Git에 절대 기록하지 않는다.

## 1. 현재 Git 상태

- 기본 브랜치: `claude/happy-gauss-cwoq21`
- 현재 개발 브랜치: `codex/p0-order-protection` (배포 결과 문서화용)
- Oracle 배포 코드: `4c073eb2` (PR #79 병합 결과)
- 활성 로컬 복제본: `/Users/seop/Documents/매매봇/Stock-chart-analyze-deploy`
- 기존 `Stock-chart-analyze-site`는 iCloud가 일부 `.git/refs`를 dataless로 바꿔
  HEAD가 끊겼다. 작업 파일은 보존하고 기준 커밋+검토 diff를 새 복제본에 복원했다.
- 웹 통합 PR: [#77 KIS 준실시간 차트와 오늘 브리핑 추가](https://github.com/easyseop/Stock-chart-analyze/pull/77)
- 주문 안전성 PR: [#78 KIS 주문 안전성과 체결 회계 강화](https://github.com/easyseop/Stock-chart-analyze/pull/78)
- PR #78 병합 커밋: `3d2a2c5a`
- 회계 지연 알림 집계 PR: [#79 체결 회계 지연 알림을 1개로 집계](https://github.com/easyseop/Stock-chart-analyze/pull/79)
- PR #79 병합 커밋: `4c073eb2`
- 병합 PR: [#75 Oracle KIS 검증과 알림 안정성 보강](https://github.com/easyseop/Stock-chart-analyze/pull/75)
- PR #75 병합 커밋: `468ad0cf`
- 리뷰 수정 커밋: `7ebe0d97`, 테스트 격리·인수인계 커밋: `79a67c51`
- 통합 PR: [#72 오라클 KIS 대시보드와 실매매 안정성 보강](https://github.com/easyseop/Stock-chart-analyze/pull/72)
- PR #72 병합 커밋: `b88eee1`
- 중복 Draft PR #70: 통합 확인 후 닫음
- 배포-매매 분리 PR: [#73 코드 push 배포와 모의매매 분리](https://github.com/easyseop/Stock-chart-analyze/pull/73)
- PR #73 병합 커밋: `bba4341`
- 통합된 주요 커밋:
  - `be0dd7a` — Oracle 보유자산 서비스와 조용한 알림 정책
  - `f2dc1ed` — 주문·체결·대사·일일손실 안전장치 통합
  - `bfc8997` — 장중 폴링 주기와 지표 합의 게이트 보강
  - `fff297d` — 코드 push 배포를 읽기 전용으로 분리
  - `a8d3ac5` — Oracle 배포를 SSH 정보가 있는 컴퓨터로 인수인계

기본 브랜치에는 과거 `codex/trading-safety-large-5`의 안전성 개선 커밋과
Oracle 자산 대시보드, 코드 push 무거래 보정이 PR #72와 #73을 통해 통합돼 있다.

## 2. 완료된 개발

### 공개 웹앱

- PR #71 병합 및 GitHub Pages 배포 완료.
- 공개 주소: <https://easyseop.github.io/Stock-chart-analyze/app/>
- 기존 `/api/signals.json`, `/api/paper_auto.json`, `/api/track.json` 계약은 유지.
- 공개 사이트에는 KIS 계좌·보유종목·키·토큰을 발행하지 않음.
- 검색, KR/US 필터, 정렬, 다크 모드, 모바일 내비게이션, 로딩/빈 상태/오류/신선도
  경고 구현.

### Oracle KIS 보유자산 화면

- `bot/portfolio_web.py`: 주문 모듈을 불러오지 않는 GET/HEAD 전용 서버.
- `infra/server/portfolio-web.service`: Oracle Ubuntu용 systemd 서비스.
- 서버는 코드 수준에서 `127.0.0.1:8765`에만 바인딩.
- 기존 KIS 환경 파일을 이용해 KIS `mock` 또는 `live` 환경의 국내·미국
  보유종목을 조회.
- 표시 필드: 종목, 보유수량, 평단, KIS 잔고 기준 현재가, 평가금액, 손익, 손익률.
- 기본 60초 갱신. 같은 주기 안의 요청은 캐시하며, 별도 프로세스인 파수꾼·매수루프와
  KIS 호출 경합을 줄인다.
- 계좌번호, App Key/Secret, 토큰은 JSON과 로그에 포함하지 않음.

### KIS 준실시간 차트·전략 A/B·지수 비교

2026-07-24 후속 개발에서 기존 읽기 전용 KIS 화면을 확장했다.

- 파수꾼이 원래 20초마다 조회하던 보유종목 가격과 잔고 응답을 권한 600의 Oracle
  로컬 캐시에 기록한다. 웹은 이 캐시를 5초마다 읽으므로 KIS API 호출을 추가하지
  않고 보유종목 가격·평가손익을 보통 20초 안팎으로 갱신한다.
- 캐시가 없거나 장중 90초 이상 낡았을 때만 기존 60초 직접조회 폴백을 사용한다.
  주문·손절 경로가 우선이며 캐시 쓰기 실패는 파수꾼을 중단시키지 않는다.
- 보유종목 상세에는 준실시간 가격선과 일봉 캔들, 거래량, 이동평균선 20/60/120,
  평균매수가·손절가·목표가 기준선을 제공한다.
- 전략 A와 전략 B를 신호·보유종목·성과 화면에서 명시적으로 분리했다.
- 개인 KIS 화면에 계좌 전체·전략 A·전략 B 수익률과 미국 Nasdaq/S&P 500,
  한국 KOSPI/KOSDAQ 비교 차트를 추가했다.
- 성과 이력은 새 배포 시점부터 Oracle 로컬 상태에 누적된다. 새로 추가된 전략별
  과거값과 두 번째 지수의 배포 이전 값은 소급 생성하지 않는다.
- 공개 GitHub Pages에는 계좌·보유종목·금액을 발행하지 않으며, 개인 성과 API도
  백분율 시계열만 반환한다.

#### 웹 UX 개선 — 외부 검토 승인 및 보완 완료

아래 변경은 `codex/kis-realtime-charts-benchmarks`에서 구현했다. 2026-07-24 외부
검토에서 P0·P1 없음, 공개/비공개 경계·읽기 전용·통화 분리·XSS·CSP·KIS 호출
무증가가 확인됐다.

- 첫 화면에 `오늘 브리핑`을 추가해 보호선·목표선 3% 이내 보유종목을 우선 표시.
- 보호선 정보가 없는 수동·미연결 보유종목과 90초 이상 낡은 시세를 별도 경고.
- 전략 A/B의 보유 수, 미국·한국 장중 성과, 시장별 보유 수익률을 나란히 비교.
- 통화가 다른 자산을 억지로 합산하지 않고 미국·한국별 최대 종목 집중도를 표시.
- KIS 계좌가 미국/한국의 두 지수보다 앞서는지 한 문장으로 요약.
- 종목 카드에 손절선부터 목표선까지 현재 위치와 남은 거리를 표시.
- 종목 상세에 현재가 기준 남은 손익비를 표시.
- 성과 화면에 A/B 우세, 두 지수 대비 판정, 선택 기간 최대낙폭을 추가.
- 모든 값은 기존 공개 JSON과 Oracle 읽기 전용 응답에서 화면이 계산하며 KIS 호출,
  주문 기능, API 응답 필드는 추가하지 않음.

외부 검토의 P2·P3도 병합 전에 보완했다.

- 계산 로직을 `portfolio_math.js`로 분리하고 Node 입력→출력 단위테스트를 추가했다.
  손절/목표 경계와 부호, 0/빈 계획값, 최대낙폭 빈·1개·알려진 시계열, A/B 통화 분리,
  시장별 집중도를 실제 숫자로 검증한다.
- 최대낙폭은 세션 시작 0% 기준임을 명시하고 유효 표본 2개부터 표시한다.
- 지수가 하나만 있을 때 “두 지수”라고 표시하지 않게 수정했다.
- 오늘 브리핑에서 연 종목 상세도 5초 갱신에 맞춰 현재가·손익·차트를 새로 읽는다.
- “지금 확인할 것”에서는 손절 위험을 목표 도달보다 먼저 정렬한다.
- 320~360px 하단 6개 메뉴가 잘리지 않도록 폭·말줄임·간격을 보강했다.

이 웹 변경은 매매 주문 코드를 추가하거나 변경하지 않는다. PR #77 병합 뒤 Oracle
운영 서버에도 반영했으며, 개인 화면은 파수꾼 공유 캐시를 이용해 KIS 호출을 늘리지
않고 서비스 중이다.

### 매매 운영 안정성

- 매수할 종목이 있어도 주문가능 현금이 부족하면 매수하지 않는 fail-closed 처리.
- 일일 실현손실 한도 도달 시 신규매수 영속 차단.
- 주문 접수와 체결을 분리하고, ACK/부분체결/UNKNOWN을 잔고·체결내역으로 대사.
- 미확정 주문이 있으면 중복 주문 차단, 확인된 잔여 수량만 재주문.
- 반반/눌림 지정가 주문의 대기·취소·만료 수명주기 구현.
- 손절 주문 chase, 취소 확인, 가격 하한, 초과매도 방지 구현.
- 체결 확인 후에만 원가장부와 보호 포지션 생성.
- 매수 신호 확인 60초, 파수꾼 시세 확인 20초, 보유자산 화면 60초.
- 각 주기는 환경변수로 조정 가능하며 너무 짧거나 긴 값은 코드에서 제한.

#### 2026-07-25 주문 보호·총시드·성과 전량 수정 — 외부 최종 승인

외부 안전성 보고서 `검토보고서_주문경합_총시드_손절.md`의 P0 4건, P1 12건,
P2 1건, P3 1건을 기준 커밋 `106065d2`에 대조했고 모두 로컬
`codex/p0-order-protection`에서 수정했다. PR #78·#79로 병합하고 Oracle
`4c073eb2`까지 단계배포했다.

핵심 완료 내용:

- BUY 잔량 취소 **확정** 뒤 최신 KIS 잔고와 주문 직전 매도가능수량만으로 손절한다.
  ACK 0주는 절반익절이나 본전 래칫으로 확정하지 않으며, 부분체결 잔여만 재시도한다.
- 잔고 선반영에는 BUY 원장의 유일한 stop으로 임시 보호한다. 후보 없음/충돌,
  잔고 실패, 주문번호 불명은 추측 대신 동결·경보·주문 차단이다.
- A/B 대사 전 귀속, 동종목 복수 예약, pending B를 하나의 브로커+원장 스냅샷으로
  계산한다. 총시드 3,500만원에서 기본 5% 완충을 먼저 제외하고 A:B=30:5 비율로
  2,850만원/475만원을 배분한다.
- sizing 0의 1주 승격을 제거하고, 첫 매수도 파수꾼 heartbeat를 요구한다. 최종 BUY
  호출에 브로커 A/B 원가가 없으면 원자 총시드 게이트를 우회하지 못하고 차단한다.
- 모든 KIS HTTP 재시도는 새 슬롯을 쓰며 sentinel/buyloop/웹의 유량을 flock 공용
  파일에서 합산한다. 웹은 공유 캐시만 읽고 KIS REST를 직접 호출하지 않는다.
- `SENTINEL_LIVE`를 실제 KIS SELL/chase 전송 게이트로 만들었다. dry-run 판단은
  영속 완료로 쓰지 않아 나중 live 보호를 막지 않는다.
- 주문 원장은 fsync+디렉터리 fsync, 프로세스 flock, 손상 전면 fail-closed,
  stale submitted→UNKNOWN, 원자 검사+기록, 중복 취소 키 차단을 갖는다.
- 계좌 성과는 매수·매도 현금흐름을 제거한 시장×A/B TWR로 바꿨다. 종목 선택 품질은
  장 시작 보유종목의 전일종가 대비 동일가중 평균으로 분리하고 장중 수동 신규매수도
  제외한다. 지수도 전일종가 기준이며 첫 배포일만 첫 표본 기준이라고 표시한다.

장애 주입 10개와 추가 경계검사를 자동화했고 Python 독립 테스트 모듈 `41/41`,
사이트 계산 Node 테스트 `5/5`, compileall, JavaScript 문법, `git diff --check`가
통과했다. 1280px·390px·320px 실물 화면에서 자산, 종목별 준실시간 차트, 지수 비교,
하단 6메뉴를 확인했고 가로 넘침과 브라우저 warning/error는 없었다.

최종 외부 재검토 요청서는 `docs/ORDER_SAFETY_FINAL_REVIEW.md`다. 기존
`docs/P0_ORDER_PROTECTION_REVIEW.md`는 첫 P0 묶음의 역사 기록이다.

직전 재검토에서 기존 18건 중 16건은 `HOLDS`였으나, 취소 확정실패 뒤 고정 취소키가
재시도를 막는 P0 E와 BUY `filled`→costbook 전환창의 총시드 과소계상 P1 Q1이
발견됐다. 두 건과 함께 비차단 2건도 로컬에서 추가 수정했다.

- 취소는 `:protect-cxl#N`/`:cxl#N` 고유 시도키를 사용한다. 앞 시도가 확정
  `rejected`일 때만 재시도하고, `submitted/unknown/filled`이면 새 HTTP를 막는다.
  취소 성공 직후 크래시는 다음 사이클이 원주문 `cancel_pending` 상태만 복구한다.
- terminal BUY도 `accounted < filled`인 동안 예약을 유지한다. 최종 submit flock
  안에서 durable costbook을 재조회해 오래된 브로커 원가와 max로 합친다.
- costbook·KIS 보호 포지션은 flock+O_APPEND+파일/디렉터리 fsync를 사용한다.
  체결 `event_id`로 `accounted` 직전 크래시 재시도도 lot·보호수량 중복이 없다.
  costbook 손상은 신규매수 fail-closed다.
- 미체결 B 계획의 기존 A 보유 재태깅을 막고, 장 시작 동일가중은
  `opened < session_day`가 증명되는 추적 포지션만 포함한다.

추가 fault-injection을 포함해 Python `41/41`, Node 계산 `5/5`, compileall,
JavaScript 문법, `git diff --check`가 다시 통과했다. 2차 최종 재검토 요청서는
`docs/ORDER_SAFETY_REREVIEW_2.md`다.

외부 최종 판정은 **승인**이다. P0 E와 P1 Q1의 적대적 반례, 잠금 교착, 기존 주문
불변식 회귀가 모두 닫혔고 병합·Oracle 단계배포 가능 판정을 받았다. 남은 비차단
P2는 브로커가 실체결가를 오래 제공하지 않을 때 미회계 예약이 계속 남는 가용성
문제다. 안전 방향은 초과지출이 아니라 신규매수 차단이므로 다음 감시를 추가했다.

- buyloop가 `filled > accounted` BUY를 매 사이클 확인한다.
- 기본 3회 연속 지속 시 운영자에게 치명 알림을 한 번 보내고 회계 완료 시 상태를
  정리한다.
- 여러 종목이 함께 임계값에 도달해도 사이클당 요약 알림 1개만 보낸다.
- `KIS_ACCOUNTING_ALERT_CYCLES`는 2~60 범위이며 기본값은 3이다.
- 감시 파일은 `bot/kis_accounting_watch.json`(0600, Git 제외)이다.
- 원장 flock은 비재귀다. 잠금 보유 경로는 `_unlocked` 변형만 호출하며 잠금 계층은
  `ledger > {costbook, kis_positions}` 단방향, 네트워크 호출 중 파일 락 보유 금지다.

새 복제본에서 의존성을 다시 설치한 뒤 전체 Python 독립 테스트 `41/41`이 통과했다.
기존 복제본의 가상환경은 iCloud dataless 때문에 pandas 본체가 비어 있었고, 이는
새 독립 가상환경 재생성으로 해소했다.

Oracle에서도 전체 Python 독립 테스트 `41/41`과 회계 지연 집계 테스트를 통과했다.
배포 첫 buyloop 사이클에서 신규 후보 FRPT·TM·SBSW가 모두 L1로 차단됐고, 과거
미회계 예약 16건은 텔레그램 16개가 아니라 요약 경보 1개로 처리됐다. 이 경보는
신규 주문이나 체결 알림이 아니라 운영자 확인용이며 주문 상태를 변경하지 않는다.

### 지표와 전략 연결

- 점수 모듈 8개: 추세/다중 TF, 상대강도, 52주 신고가, 시장방향, 거래량,
  지지저항, RSI, 추세선.
- ADX는 시장 국면과 국면별 가중치에 사용.
- ATR은 손절선·위험금액·수량 계산에 사용.
- 매물대/POC/VAH/VAL은 전략 B 진입·목표·손절에 사용.
- 8개 방향성 점수 중 6개 이상이 동시에 약세이면 `now` 신규 진입 거부.
- 검증 전 패턴 품질 지표는 기록·분석용으로 유지해 과최적화를 방지.

### 알림

- `NOTIFY_MODE=trade_only` 지원.
- 실제 매매, 사용자 요청 조회, 치명 안전 경보만 전송.
- 매수 제안, 성과 리포트, 일상 운영 성공 알림은 억제.
- 치명 경보는 어떤 알림 모드에서도 유지하며 ntfy 이중화 가능.

## 3. 검증 결과

로컬에서 아래 검증이 모두 통과했다.

```bash
python -m tests.run_all
python -m compileall -q bot scanner tests
/Users/seop/.nvm/versions/node/v24.15.0/bin/node --check scanner/site_app/app.js
/Users/seop/.nvm/versions/node/v24.15.0/bin/node --check scanner/site_app/portfolio_math.js
/Users/seop/.nvm/versions/node/v24.15.0/bin/node --test tests/site_math.test.js
git diff --check
```

검증 범위에는 매수 현금, 일일손실, 중복주문, 부분체결, UNKNOWN 대사, 국내/미국
주문 라우팅, 손절 chase, 지표 매핑, 알림 필터, 공개/개인 웹 안전 경계가 포함된다.

후속 KIS 차트 개발에서도 전체 `tests/test_*.py`를 다시 실행했다. 공유 캐시 권한과
장중 만료, 파수꾼 잔고 응답 재사용, 직접 KIS 호출 수 불변, 준실시간 가격 이력,
일봉 OHLCV/이동평균, 전략 A/B, 4개 지수, 백분율 전용 성과 응답을 추가로 검증했다.
사이트 JavaScript 문법 검사와 로컬 HTTP 스모크 테스트에서도 페이지·가격·성과
엔드포인트가 모두 200을 반환했다.

웹 UX 외부 검토 후에는 아래 계산 회귀검사를 추가로 통과했다.

- `tests/site_math.test.js`: 5/5 통과
- `tests/test_site_app.py`: 9/9 통과
- `portfolio_math.js`, `app.js` 문법 검사 통과
- `git diff --check` 통과

시스템 기본 Node는 과거 Homebrew ICU에 묶인 오래된 실행 파일이라 시작되지 않았다.
검증에는 이 Mac에 정상 설치된 Node 24를 사용했다. 코드 결함이나 CI 문제는 아니며
GitHub Actions도 자체 Node 실행 환경을 사용한다.

### 2026-07-24 리뷰 3건 및 Oracle 실측

클로드 리뷰의 실제 결함 3건을 PR #75에서 보정했다.

- 일일손실 래치 최초 전이에 critical `trade` 알림 1회 발송. 알림 실패와 무관하게
  래치를 먼저 저장하며 같은 날 재호출에는 다시 보내지 않는다.
- 보유자산 화면 기본 폴링을 15초에서 60초로 늘리고 5~300초 클램프를 테스트한다.
  프로세스 간 공유 리미터는 새로 만들지 않았으며 주문 예약 슬롯도 변경하지 않았다.
- 매수·매도 체결확정 알림에 `category="trade"`를 명시해 메시지 문구 추론 의존을
  제거했다.

로컬에서는 전체 `tests/test_*.py`, compileall, 사이트 JavaScript 문법 검사,
`git diff --check`가 통과했다. Oracle에서는 관련 단위검사와 실제 KIS `mock`
보유자산 조회를 검증했다.

- 반환된 보유행이 비어 있지 않았고 모든 행의 수량·평단·현재가·평가금액·손익·
  손익률이 유한한 숫자였다.
- `partial=false`, `read_only=true`, `refresh_seconds=60`.
- 60초 안의 두 요청은 같은 `generated_at`을 반환하고, 60초가 지난 요청은 새
  `generated_at`을 반환했다.
- `/app/`은 200, POST는 405, 리스너는 `127.0.0.1:8765`뿐이었다.

Oracle에서 첫 `test_kis_boot` 실행 때 모의 AAPL/NASD/NVDA 알림 3건이 실제 Telegram
자격증명 폴백을 읽어 발송됐다. 테스트의 KIS 조회·주문은 모두 모킹돼 실제 주문·체결은
없었다. 재발 방지로 `tests/__init__.py`가 외부 알림 자격증명을 비우고
`NOTIFY_ENV_FALLBACK=0`을 설정하며, `bot.notify`도 이 명시적 폴백 차단값을 지원한다.

## 4. CI 테스트 격리 보정

PR #72에서 발견된 자동매매 회귀 테스트의 운영 상태 격리 누락을 보정했다.

GitHub Actions에서는 `GITHUB_ACTIONS=true`이므로 테스트가 실제 `state` 브랜치의
`autopaper.snapshot.json`을 복구해 테스트용 빈 계좌를 오염시킬 수 있었다.
`fastsafe`, `killswitch`, `phase0`, `pos_cap`, `trail` 테스트의 `_fresh()`
초기화에서 운영 스냅샷 복구를 끈다. CI 분산 매매 락도 검증 대상이 아닌
`phase0`, `pos_cap`, `trail`에서는 명시적으로 `off`로 고정한다.

```python
def _fresh(tmp: str) -> None:
    ...
    ap._state_branch_snapshot = lambda: None
    ap._trading_lock_status = lambda run_id: "off"
```

이 수정은 테스트에서만 운영 스냅샷 복구를 끄며 실제 자동매매 복구 로직은 변경하지
않는다. 로컬 검증도 `GITHUB_ACTIONS=true` 조건으로 실행해 CI 환경을 재현한다.

## 5. 코드 push 배포와 매매 분리

기본 브랜치에 PR을 병합하면 `daily-scan`의 push 실행이 사이트를 재배포한다. 이전에는
`MODE=none`이어도 스크리너 생성 과정에서 모의계좌를 갱신하고 텔레그램 시크릿을
전달해 코드 배포만으로 모의 체결 알림이 발생할 수 있었다.

후속 보정에서는 push 실행에 `AUTOPAPER_READ_ONLY=1`을 적용한다.

- 모의계좌 신규진입·청산·대기주문을 실행하지 않음
- 매매 분산 락과 `state` 브랜치 백업에 접근하지 않음
- 텔레그램·ntfy 자격증명을 스크리너에 전달하지 않음
- 공개 화면을 위한 읽기 전용 `paper_auto.json` 스냅샷만 생성
- 정기·수동 데이터/매매 실행과 빌드·배포 실패 경보는 기존대로 유지

PR #73은 전체 CI 통과 후 병합됐다. 병합으로 시작된
[daily-scan 실행 #30074425686](https://github.com/easyseop/Stock-chart-analyze/actions/runs/30074425686)은
빌드, Pages 배포, GitHub 호스팅 스모크 테스트까지 모두 성공했다. 원격 로그에서도
아래 문구를 확인했다.

```text
[autopaper] 코드 배포 전용 — 매매·상태 저장·알림 생략(표시 스냅샷만 생성)
```

이 실행에서는 `state` 브랜치 계좌 백업 단계도 건너뛰었다.

## 6. 다른 노트북에서 이어가기

GitHub CLI 인증 후 아래 순서로 시작한다.

```bash
gh auth status
git clone https://github.com/easyseop/Stock-chart-analyze.git
cd Stock-chart-analyze
git fetch --all --prune
git switch claude/happy-gauss-cwoq21
git pull --ff-only origin claude/happy-gauss-cwoq21
python3 -m pip install -r requirements.txt
```

작업 시작 전:

```bash
git status --short --branch
gh pr list
gh run list --limit 10
```

다른 노트북의 Codex에 전달할 문장:

> `docs/CODEX_HANDOFF.md`를 먼저 읽고 현재 운영 상태를 확인해줘. PR #75 병합과
> Oracle 기본 브랜치 복귀는 완료됐다. 새 변경은 별도 `codex/` 브랜치에
> 커밋·푸시하고 이 인수인계서도 갱신해줘.

## 7. Oracle 배포 — 완료

SSH 주소·개인키·KIS 인증값은 계속 Git 밖에만 둔다. 2026-07-24 실제 서버 구성이
초기 문서의 `/opt/stock`·`bot` 사용자 예시와 달라 기존 운영 배치를 보존해 적용했다.

- 저장소: `/home/ubuntu/Stock-chart-analyze`
- KIS 환경 파일: `/home/ubuntu/kis.env`(권한 600, 값은 기록하지 않음)
- 서비스 사용자: `ubuntu`
- Python 의존성: 저장소의 `.venv`; 서버에 `python3.10-venv` 설치
- 배포 브랜치: `claude/happy-gauss-cwoq21` (`4c073eb2`)
- `portfolio-web.service`: enabled/active
- 기존 `sentinel`, `buyloop`, `telegram`: 수정 코드 적용 후 재시작, 모두 active
- `autodeploy.timer`: active. 재시작 대상에 `portfolio-web`까지 포함

2026-07-25 PR #78·#79 단계배포 결과:

- kill-switch L1: `buy_new=False`, `protect_sell=True`
- KIS 환경은 `mock`이며 `SENTINEL_LIVE=1`로 모의계좌의 보호 SELL만 실제 전송
  가능하게 했다. 이는 KIS 실전계좌 전환이 아니다. 신규 BUY는 L1이 계속 차단한다.
- `buyloop.service`는 시스템 Python 대신 저장소 `.venv`를 사용한다. 배포 뒤 첫
  사이클에서 pandas 성과 추적 오류가 사라졌고 서비스 재시작 횟수는 0이었다.
- 최초 실측에서 파수꾼이 잔고에 포함된 현재가를 다시 종목별 KIS API로 조회해 한
  사이클이 약 100초가 되고 heartbeat가 60초 P0 경계를 넘었다. 같은 사이클의 KIS
  잔고 현재가를 손절 판단과 웹 캐시에 재사용하도록 보정했다. 잔고 조회가 실패하거나
  현재가가 없을 때만 기존 종목별 현재가 조회로 폴백하며, 직전 사이클 가격은
  재사용하지 않는다.
- 파수꾼과 개인 웹이 함께 읽는 캐시는
  `/home/ubuntu/Stock-chart-analyze/data_cache/kis_market_snapshot.json`이며
  권한 600이다. Oracle의 systemd `PrivateTmp` 때문에 `/tmp` 기본값이 프로세스마다
  갈라지는 문제를 이 영속 경로로 보정했다.
- 개인 API는 KIS mock 보유 17종목, `partial=false`, `read_only=true`,
  `source=sentinel_shared_cache`, 브라우저 갱신 5초를 반환했다. `/app/` GET은
  200, POST는 405이며 리스너는 계속 `127.0.0.1:8765`뿐이다.
- 배포 전 환경 파일과 주문 상태는 각각
  `/home/ubuntu/kis.env.pre-pr78`,
  `/home/ubuntu/stock-backups/pre-pr78-20260725.tgz`로 권한 600 백업했다.
- KIS mock 미국 보유 17종목, 매입금액 합계 `$25,133.71`, 평가금액 `$25,161.12`.
  적용 환율 1,380원 기준 매입원가는 약 3,468만원이다. 명목 총시드 3,500만원 이하는
  맞지만 새 정책의 5% 완충 후 운영한도 3,325만원은 약 143만원 초과하므로 신규매수를
  계속 막고 자연 청산으로 한도 아래가 될 때까지 기다린다.
- 업그레이드 전 체결된 BUY 16건은 새 `accounted` 표식이 없어 예약으로 남아 있다.
  이 중 브로커 잔고가 0인 BAM, 보호수량 25와 브로커수량 13이 다른 LW도 있어 자동
  일괄이관하면 중복 장부나 잘못된 보호수량을 만들 수 있다. 감시 경보는 발송됐지만
  예약은 안전하게 유지했다. 주문별 대사·마이그레이션 전에는 L1을 해제하지 않는다.

저장소의 일반 배포 유닛은 `/opt/stock` 표준 구성을 계속 유지한다. 실제 서버에는
`/etc/systemd/system/portfolio-web.service.d/oracle-ubuntu.conf` 드롭인으로 사용자,
경로, `.venv`, 기존 `kis.env` source 방식을 보정했다. 서비스는 외부 포트를 열지 않고
루프백에만 바인딩한다.

서버 자체의 민감정보 없는 검증:

```bash
curl -fsS http://127.0.0.1:8765/api/portfolio.json \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["environment"], bool(d["positions"]), d["partial"], d["refresh_seconds"])'
```

사용자 기기에서 SSH 터널:

```bash
ssh -N -L 8765:127.0.0.1:8765 ubuntu@오라클주소
```

터널을 유지한 상태로 <http://127.0.0.1:8765/app/>에 접속한다. OCI 보안 목록이나
Ubuntu 방화벽에서 8765 포트를 공개하지 않는다.

## 8. 공개 사이트 접속 참고

GitHub API 기준 Pages는 public이고 최신 배포와 GitHub 호스팅 스모크 테스트가
성공했다. 배포 artifact에도 다음 파일이 포함된 것을 별도로 확인했다.

- `app/index.html`
- `app/app.js`
- `app/app.css`
- `api/paper_auto.json`
- `api/signals.json`
- `api/track.json`

다만 2026-07-24 현재 작업 중인 Mac 네트워크에서는
`easyseop.github.io:443` 연결이 IPv4/IPv6 모두 타임아웃됐다. 사이트 파일
문제라기보다 해당 네트워크의 GitHub Pages 접근 문제로 확인됐다. 다른
네트워크/모바일 핫스팟으로 확인하거나, 개인 KIS 화면은 Oracle SSH 터널 경로를
사용한다.

## 9. 다음 작업 순서

P0/P1 수정 외부 승인, PR #78·#79 병합, Oracle 단계배포, KIS mock 실데이터,
전체 Python `41/41`, L1 신규매수 차단, 원장 건강성, 공유 캐시와 개인 웹 검증까지
완료했다. 서버 작업트리는 `4c073eb2`에서 clean이고 보호매도는 유지된다.

1. 업그레이드 전 BUY 16건을 주문별로 브로커 체결·보호 포지션과 대사한다. BAM과
   LW 불일치를 먼저 확인하고, 검증된 주문만 durable `accounted`로 이관한다.
2. 총시드가 5% 완충 후 운영한도 아래인지 다시 계산하고, 모의계좌 10세션 이상
   주문·부분체결·취소·손절·경보 무결성을 관찰한다.
3. 1~2가 끝난 뒤에만 사용자 승인과 operator ack를 받아 L1 해제를 별도 수행한다.
   현재 배포 완료가 신규매수 재개 승인을 뜻하지 않는다.
4. KIS 실전계좌 전환과 live 하드블록 해제는 별도 단계다. 장애 주입 10개와 모의
   관찰이 모두 유지되는지 재검증한 뒤 명시적 사용자 승인 없이는 진행하지 않는다.
5. Ubuntu에는 `6.8.0-1058-oracle`이 설치됐지만 현재 `6.8.0-1049-oracle`로
   실행 중이라 재부팅 필요 표시가 남아 있다. 매매 시간 밖에서 재부팅하고
   `sentinel`, `buyloop`, `telegram`, `portfolio-web`, `autodeploy.timer`를
   다시 확인한다.
