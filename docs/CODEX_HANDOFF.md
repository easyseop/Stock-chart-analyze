# Codex 개발 인수인계

마지막 갱신: 2026-07-29
저장소: `easyseop/Stock-chart-analyze`

이 문서는 다른 노트북이나 새 Codex 작업에서 개발을 바로 이어가기 위한 현재 상태,
검증 결과, 미완료 작업과 운영 주의사항을 기록한다. API 키·계좌번호·토큰·SSH
개인키 등 비밀값은 이 문서와 Git에 절대 기록하지 않는다.

## 1. 현재 Git 상태

- 기본 브랜치: `claude/happy-gauss-cwoq21`
- 현재 개발 브랜치: `codex/l1-readiness-audit` (아직 미병합·미배포)
- 현재 개발 기준 커밋: `76aae46` (PR #96 병합 결과)
- 활성 로컬 복제본: `/Users/seop/Documents/매매봇/Stock-chart-analyze-deploy`
- 기존 `Stock-chart-analyze-site`는 iCloud가 일부 `.git/refs`를 dataless로 바꿔
  HEAD가 끊겼다. 작업 파일은 보존하고 기준 커밋+검토 diff를 새 복제본에 복원했다.
- 웹 통합 PR: [#77 KIS 준실시간 차트와 오늘 브리핑 추가](https://github.com/easyseop/Stock-chart-analyze/pull/77)
- 주문 안전성 PR: [#78 KIS 주문 안전성과 체결 회계 강화](https://github.com/easyseop/Stock-chart-analyze/pull/78)
- PR #78 병합 커밋: `3d2a2c5a`
- 회계 지연 알림 집계 PR: [#79 체결 회계 지연 알림을 1개로 집계](https://github.com/easyseop/Stock-chart-analyze/pull/79)
- PR #79 병합 커밋: `4c073eb2`
- 파수꾼 중복 시세 제거 PR: [#81 파수꾼 KIS 중복 시세 조회 제거](https://github.com/easyseop/Stock-chart-analyze/pull/81)
- PR #81 병합 커밋: `750a087e`
- 개인 웹 8888 포트 변경 PR: [#83 개인 웹 포트를 8888로 변경](https://github.com/easyseop/Stock-chart-analyze/pull/83)
- 포트폴리오 핵심 가격정보 PR: [#84 보유종목 매수가·현재가 핵심정보 강화](https://github.com/easyseop/Stock-chart-analyze/pull/84)
- PR #84 최초 구현 커밋: `548bdc5`
- PR #84 병합 커밋: `405aaf2`
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
- 서버는 코드 수준에서 `127.0.0.1:8888`에만 바인딩.
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

#### 포트폴리오 핵심 가격·매수 정보 강화

2026-07-25 PR #84에서 토스증권처럼 보유종목 판단에 먼저 필요한 가격·원가 정보를
카드와 상세 상단으로 끌어올렸다.

- 카드에 현재가, KIS 평균매수가, 1주당 손익, 총 투입금, 현재 평가금, 총 손익률·
  손익금, 보유수량을 동시에 표시한다.
- 상세에는 위 값을 6개 숫자로 분리하고 평균매수가를 강조한다. 손실 중이면 현재가
  기준 본전 회복에 필요한 상승률, 수익 중이면 평균매수가 대비 상승률을 계산한다.
- 봇의 확정 체결 원장에 `opened`가 있는 종목은 매수일과 진입일 포함 달력일 기준
  `보유 N일째`를 표시한다. 수동·기존 보유처럼 근거가 없거나 날짜가 잘못됐으면
  추정하지 않고 `매수일 미확인`으로 표시한다.
- 금액은 KIS 잔고의 `buy_amt`, `eval_amt`, `pl_amt`, `pl_rt`를 우선 사용하고,
  누락된 경우에만 수량×단가로 화면 표시값을 보완한다. 통화별 합계는 계속 분리한다.
- 파수꾼의 기존 공유 캐시 필드만 사용하며 새 KIS 호출·웹 API·주문 기능은 없다.
  공개 GitHub Pages에서는 기존처럼 계좌·보유·매수가 정보가 전혀 나오지 않는다.
- 상세창이 열린 동안 기존 5초 브라우저 갱신으로 현재가뿐 아니라 수량·평단·평가금·
  손익과 보호선까지 함께 새로 그린다.

추가 검증은 순수 계산 Node 테스트 `8/8`, 웹 안전 경계 `9/9`, 전체 Python 독립
테스트 `41/41`, Python compile, JavaScript 문법, `git diff --check`를 통과했다.
실제 브라우저 1280px·390px·320px에서 카드와 상세를 확인했고 문서 가로 넘침,
숫자 잘림, 브라우저 warning/error가 모두 0건이었다.

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
`codex/p0-order-protection`에서 수정했다. PR #78·#79·#81로 병합하고 Oracle
`750a087e`까지 단계배포했다.

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

- `tests/site_math.test.js`: 8/8 통과
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
- `/app/`은 200, POST는 405, 리스너는 `127.0.0.1:8888`뿐이었다.

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
- 배포 브랜치: `claude/happy-gauss-cwoq21` (현재 코드 `405aaf2`)
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
  재사용하지 않는다. Oracle 재배포 뒤 새 PID의 연속 세 사이클은 약 25~43초로
  측정돼 모두 P0 60초 경계 안에서 heartbeat를 갱신했다.
- 파수꾼과 개인 웹이 함께 읽는 캐시는
  `/home/ubuntu/Stock-chart-analyze/data_cache/kis_market_snapshot.json`이며
  권한 600이다. Oracle의 systemd `PrivateTmp` 때문에 `/tmp` 기본값이 프로세스마다
  갈라지는 문제를 이 영속 경로로 보정했다.
- 개인 API는 KIS mock 보유 17종목, `partial=false`, `read_only=true`,
  `source=sentinel_shared_cache`, 브라우저 갱신 5초를 반환했다. `/app/` GET은
  200, POST는 405이며 리스너는 계속 `127.0.0.1:8888`뿐이다.
- 사용자 요청으로 개인 웹 포트를 `8765`에서 `8888`로 변경했다. 저장소 서비스
  유닛과 Oracle 전용 systemd 드롭인, 헬스체크·SSH 터널 문서를 함께 맞췄으며 기존
  8765 리스너는 닫고 8888만 루프백으로 유지한다.
- PR #84는 `/home/ubuntu/stock-backups/pre-pr84-web-20260725.tgz`로 직전 웹 파일을
  백업한 뒤 배포했다. 병합 직후 자동배포 타이머가 먼저 새 커밋을 받아
  `sentinel`, `buyloop`, `telegram`, `portfolio-web`을 함께 재시작했고, 서버
  대상 테스트 뒤에는 `portfolio-web`만 다시 시작했다. 전체 서비스 5개 active,
  각 서비스 비정상 재시작 0회, 재시작 이후 오류 로그 0건, heartbeat 정상, L1,
  원장 정상을 확인했다. 공유 캐시 17종목의 숫자 필드와 `opened` 날짜 형식도 모두
  유효했다.
- 실제 Oracle 화면에서 보유 카드 17개, 현재가·평균매수가, 상세 6개 핵심 숫자,
  매수일/보유기간, 가격 분석, 기존 차트를 확인했다. 가로 넘침과 브라우저 오류는
  없었다. GET 200/POST 405, `read_only=true`, `partial=false`,
  `source=sentinel_shared_cache`, 8888 루프백 전용과 8765 폐쇄도 유지한다.
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
curl -fsS http://127.0.0.1:8888/api/portfolio.json \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["environment"], bool(d["positions"]), d["partial"], d["refresh_seconds"])'
```

사용자 기기에서 SSH 터널:

```bash
ssh -N -L 8888:127.0.0.1:8888 ubuntu@오라클주소
```

터널을 유지한 상태로 <http://127.0.0.1:8888/app/>에 접속한다. OCI 보안 목록이나
Ubuntu 방화벽에서 8888 포트를 공개하지 않는다.

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

> 이 절은 2026-07-25 당시의 역사 기록이다. legacy 이관과 커널 재부팅은 이후
> 완료됐으며, 현재 미완료 항목과 다음 순서는 문서 끝의 §18을 기준으로 한다.

P0/P1 수정 외부 승인, PR #78·#79 병합, Oracle 단계배포, KIS mock 실데이터,
전체 Python `41/41`, L1 신규매수 차단, 원장 건강성, 공유 캐시와 개인 웹 검증까지
완료했다. PR #84까지 Oracle 코드 `405aaf2`로 배포했으며 작업트리는 clean이고
보호매도는 유지된다.

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

## 10. 2026-07-25 차트 필터·B 관찰·거래이력

사용자가 차트의 여러 선을 직접 가려 비교하고, 전략 B의 진입 전 관찰군과 A/B
정의, 실제 매도 이력을 한 화면에서 확인할 수 있도록 개인·공개 웹을 확장했다.

- 성과·지수 비교 차트: `KIS 전체`, `전략 A`, `전략 B`, 각 시장지수를 이름
  버튼으로 개별 표시/숨김할 수 있다. 다른 선을 숨겨도 선 색은 바뀌지 않는다.
- 종목 가격 차트: 준실시간의 현재가·평균매수가·손절·목표와 일봉의
  MA20·MA60·MA120·평균매수가·손절·목표·현재가를 각각 켜고 끌 수 있다.
  여기서 `평균매수가`는 KIS 잔고가 반환한 해당 보유종목의 실제 평균매수가다.
- 전략 정의: A는 저점권의 하락→상승 전환을 확인한 전략, B는 장기
  POC/밸류영역 지지와 반등을 확인한 전략으로 화면에 명시했다.
- `B 관찰`: 매물대 위치·머리 위 물량·손절폭·손익비 같은 위험 조건은
  통과했지만 터치·회복·상단마감·거래량·신저가 방지 중 일부 확인이 덜 된 종목만
  표시한다. 그룹 이름은 `shelf_watch`이며 기존 매수루프가 받는
  `group == "shelf"`와 분리돼 자동매수되지 않는다. 후보의 손절·목표는
  `참고`로만 표시한다.
- 개인 `내 자산 > 거래이력`: 확정 매도 체결 시각, 종목, 전략, 손절/익절/기타
  사유, 매도 직전 평단가, 실제 매도가, 수량, 부분매도 잔량, 실현손익 원화,
  수익률과 종목 통화 기준 가격차를 표시한다. 전체/A/B와 손절/익절/수익/손실
  필터를 제공한다.
- 거래이력은 주문 원장·원가장부·보호 포지션 원장의 공통 `event_id`만 결합하는
  읽기 전용 API다. KIS 추가 호출과 주문 모듈 호출은 없으며 계좌번호·주문번호·
  내부 원장 키·파일경로를 응답하지 않는다. 주문 원장이 손상됐으면 이력을
  추정하지 않고 전부 숨기는 fail-closed 방식이다. 원장 도입 전 과거 체결은
  소급 추정하지 않는다.

검증:

- 전체 독립 Python 테스트 `42/42` 통과.
- 거래이력의 평단·매도가·부분매도·실현손익·수익률·민감정보 비노출과 원장 손상
  fail-closed 전용 테스트 통과.
- B 관찰의 자동매수 제외, 하드 위험조건 제외, 확정 B 회귀 테스트 통과.
- JavaScript 문법, `git diff --check`, 공개/개인 API 경계 검증 통과.
- 실제 브라우저에서 B 진입/관찰 탭과 상세 조건, 거래이력 A/B·손절/익절 필터,
  성과·지수 선과 종목 평균매수가 선의 껐다 켜기, 390px·320px 가로 넘침 없음,
  브라우저 오류 0건을 확인했다.
- Oracle 배포 전 상태는 서비스 5개 모두 active, kill-switch `L1`, 주문 원장
  `healthy=True`였다. L1은 배포 뒤에도 해제하지 않는다.
- PR #86 첫 단계배포에서 개인 웹의 systemd 읽기 전용 격리가 기존 reader의
  `O_RDWR|O_CREAT` 잠금 파일 열기까지 차단해 `/api/trades.json`이 503을 반환했다.
  원장이나 서비스 권한을 넓히지 않고, 거래이력 전용 reader가 writer와 동일한
  `.lock` 파일을 `O_RDONLY + LOCK_SH`로만 여는 방식으로 보정했다. 원장 본문과
  잠금 파일의 내용·mtime·권한을 바꾸지 않는 테스트를 추가했고 전체 `42/42`를
  다시 통과했다.
- PR #86의 첫 Pages 빌드는 실데이터 B 관찰 `checks`에 남은 `numpy.bool_`를
  표준 JSON 인코더가 거부해 `signals.json` 생성 단계에서 중단됐다. 분석 경계와
  JSON 발행 경계에서 검사값을 일반 `bool`로 이중 정규화하고, 실제 numpy 비교값을
  넣어 `_signals_json`까지 직렬화하는 회귀 테스트를 추가했다. 이 오류는 정적
  사이트 배포만 막았고 Oracle 매매 서비스나 주문 상태를 변경하지 않았다.

## 11. 2026-07-25 매수 거래이력·Actions 독립 차선 (미병합)

사용자 확인 결과 개인 웹 거래이력이 매도만 표시해 실제 매수 체결을 볼 수 없었다.
또한 예약 cron, GitHub freshness watchdog, Cloudflare Worker가 서로 다른 발사
주체처럼 보여도 모두 같은 GitHub Actions `daily.yml`을 실행하므로, Actions
대기열 지연을 실행기 차원에서 이중화하지 못한다.

로컬 브랜치 `codex/oracle-local-brain-trade-history`에서 다음을 구현했다.
아직 commit/push/merge/Oracle 배포하지 않았고 운영 L1도 그대로다.

- 거래이력 v2: 확정 `buy_fill`과 `sell_fill`을 함께 표시한다. ACK는 제외하고,
  부분매수 delta·실제 체결가·체결금액·체결 후 평단·체결 후 수량을 제공한다.
  공통 event_id 중복은 한 번만 표시하며 주문번호·원장키·pos_key·계좌정보는
  응답하지 않는다. 과거 체결 근거가 없는 주문은 현재 잔고로 소급 추정하지 않는다.
- 개인 웹: 매수/매도 필터, 실제 매수가, 체결 후 평단, 수량, 원화 환산금액을
  추가했다. 기존 전략·손절/익절·수익/손실 필터와 함께 쓸 수 있다.
- `oracle-brain`: KIS·주문·원장을 전혀 읽지 않는 소형 분석기다. 최근 후보 최대
  40개와 회당 4개 순환 종목만 분석해 `/var/lib/stock-oracle-brain/signals.json`
  에 원자 저장한다. 순환·고정 관찰군은 24시간 이내 유효한 후보 basis가 있을
  때만 대상을 보완하며 단독으로 basis를 만들 수 없다.
- `signal_feed`: GitHub가 20분 이내면 항상 GitHub, 지연됐을 때만 명시적으로
  활성화된 12분 이내 Oracle 결과를 선택한다. GitHub 45분 초과와 Oracle
  비활성/노후/손상이 겹치면 신호 0건으로 신규매수 fail-closed한다. 두 피드는
  합치지 않는다.
- 기본 `ORACLE_SIGNAL_FALLBACK_ENABLED=0`이라 병합 뒤에도 곧바로 주문 입력이
  바뀌지 않는다. 먼저 timer만 그림자 운전해야 한다.
- Oracle 실제 1GB VM에 맞춰 실행 완료 뒤 5분, 최대 40종목, `MemoryMax=420M`,
  낮은 CPU 우선순위, 중복실행 flock을 설정했다. KIS env는 서비스에 주입하지 않는다.
  캐시·상태 디렉터리는 systemd `CacheDirectory`·`StateDirectory`가 사전 생성하고,
  실제 `/home/ubuntu` 경로와 `.venv`는 표준 유닛이 아닌 Oracle drop-in으로 분리했다.
- 실제 Oracle에는 `watchdog.service`가 설치되지 않은 상태였는데 health beacon이
  선택 유닛으로 처리해 정상처럼 숨겼다. sentinel/buyloop/watchdog는
  `not_installed`도 down으로 센다. `BEACON_UNITS`를 부분집합으로 덮어도
  `BEACON_REQUIRED_UNITS`와 합집합을 만들어 필수유닛이 빠지지 않게 했고, 현재
  홈 디렉터리 경로용 watchdog drop-in을 준비했다.

검증:

- 전체 독립 Python 테스트 `44/44` 통과.
- ACK 제외, 부분매수 4+6, event_id 중복 제거, 평단·손익·민감정보 비노출,
  원장손상 fail-closed 통과.
- GitHub 우선, fallback=0, 양쪽 노후, 미래시각, 중복·계약 오류, 후보 24시간
  만료와 Oracle 주문 import 부재 통과.
- JavaScript 문법, shell 문법, `git diff --check` 통과.
- 첫 전체 회귀의 로그 선택필드 `KeyError` 1건을 수정한 뒤 `44/44` 전체를
  처음부터 다시 통과했다.

Claude 1차 적대 검토에서 P1 1건·P2 2건과 표시/구성 P3를 발견해 병합을 계속
차단했다. 수정 내용:

- P1: 후보 basis가 만료됐는데 discovery/configured-watch 대상이 있다는 이유로
  `basis=now`를 넣던 경로를 제거했다. 25시간 노후 basis와 두 보완 대상을 함께
  주입해 시세조회·출력 전에 `no-safe-candidate-basis`로 끝나는 회귀 테스트를
  추가했다.
- P2: 저장소의 `data_cache*` 사전존재를 요구하던 `ReadWritePaths`를 제거하고
  systemd 관리 State/Cache 디렉터리로 옮겼다. 깨끗한 호스트에서도 서비스가
  쓰기경로를 먼저 만든다.
- P2: health beacon 감시 대상을 `BEACON_UNITS ∪ BEACON_REQUIRED_UNITS`로 바꿨다.
  사용자가 watchdog를 목록에서 빼도 union 결과에 복원되는 실행 테스트를 추가했다.
- P3: 같은 pos_key의 legacy `open`과 확정 `buy_fill`을 이중계상하지 않게 했고,
  같은 종목 A/B lot이 둘 이상인데 key 없는 sell_fill은 임의 귀속하지 않고
  일부 이력을 숨기는 방향으로 실패한다.
- P3: Oracle 분석기의 실제 전이 import graph에도 KIS·원장·사이징·손절 모듈이
  없음을 깨끗한 하위 프로세스에서 검사한다. 선택 기능인 분석기 import가 기존
  매매 서비스의 autodeploy까지 롤백시키던 결합도 제거했다.

외부 검토 요청서는
`docs/CLAUDE_REVIEW_ORACLE_LOCAL_BRAIN_TRADE_HISTORY.md`, 상세 설계는
`docs/ORACLE_LOCAL_BRAIN_DESIGN.md`다. 다음 순서는 Claude P0/P1 검토 → 승인 시
커밋/PR → 누락 watchdog 복구 → fallback=0 그림자 배포 → 한/미 각 한 장 관찰 →
GitHub 60분 장애주입이다. L1 해제와 fallback=1은 각각 별도 승인 없이는 하지 않는다.

1차 검토 수정 뒤 Claude 재검토용 묶음은
`/Users/seop/Documents/매매봇/CLAUDE_REVIEW_SECTION11_V2.zip`이다. 전체 diff와
검토 요청서·상세 설계 3파일이 들어 있으며 압축 무결성·민감정보 패턴 검사를
통과했다. 기존 `CLAUDE_REVIEW_SECTION11.zip`은 1차 검토본이므로 재검토에는
사용하지 않는다.

Claude V2 재검토 최종 판정은 `P0/P1 없음`, 병합과
`ORACLE_SIGNAL_FALLBACK_ENABLED=0` 그림자 배포 승인이다. basis 만료,
State/CacheDirectory, 필수유닛 union과 systemd 격리 지시자를 직접 재검증했다.
보고서의 비차단 P3 목록에는 legacy 거래이력과 전이 import 검사가 남았다고
적혔지만, 전달된 V2 체크섬과 diff를 다시 대조한 결과 해당 두 건도 이미
`legacy-metadata`/복수 lot fail-closed 처리와 하위 프로세스 import graph
테스트로 포함돼 있다. 따라서 승인본보다 현재 작업본이 약화된 부분은 없다.

승인 뒤 다음 실행 순서는 명확히 분리한다.

1. 로컬 변경을 commit/push하고 기본 브랜치 대상 PR을 만든 뒤 CI 통과 후 병합.
2. Oracle에서 누락된 watchdog를 먼저 복구하고 heartbeat·L1을 확인.
3. oracle-brain timer와 Oracle 경로 drop-in만 설치해 fallback=0 그림자 운전.
4. 한/미 각 1세션의 RSS·실행시간·실패율·GitHub 대비 신호차를 관찰.
5. GitHub 60분 장애주입도 L1 상태에서 수행해 신규주문 0을 확인.
6. fallback=1, L1 해제, 실전 하드블록 해제는 각각 별도 승인 전까지 금지.

PR #89 생성 뒤 GitHub `Site UI CI`의 `compileall`이 리뷰용 V2 diff 끝부분에서
`tests/site_preview.py`가 `"low": roun`으로 잘린 문법 오류를 발견했다. 운영
모듈은 아니지만 병합 차단 결함으로 판정해, 기존 `_chart` 이후 미리보기 서버
본문을 그대로 복원하고 거래이력 v2 fixture만 유지했다. 복원 뒤 Python
`compileall`, 전체 Python 테스트 `44/44`, Node 테스트 `8/8`, 두 JavaScript
문법 검사, shell 문법 검사와 `git diff --check`를 다시 통과했다. 리뷰 결과만
믿고 우회 병합하지 않았으며 PR CI가 초기에 발견한 오류는 수정 커밋으로 남긴다.

병합 뒤 Oracle 배포 전 읽기 점검에서 서버는 새 HEAD `5371f4a`, clean, L1이며
sentinel·buyloop·telegram·portfolio-web가 active인 것을 확인했다. watchdog와
oracle-brain은 아직 미설치다. 서버 보안 env의 실제 경로는
`/home/ubuntu/kis.env`이고 `/etc/stock/kis.env`는 없으므로, watchdog Oracle
drop-in이 기본 `EnvironmentFile` 지시자를 비운 뒤 기존 서비스와 같은 방식으로
해당 env를 source하도록 보완했다. 값은 읽거나 저장하지 않았고 경로·파일 권한
600만 확인했다. 이 보완을 CI/후속 PR에 통과시킨 뒤 유닛을 설치한다.

### 12. 2026-07-25 섹션 11 병합·Oracle 그림자 배포 결과

- PR #89 `Add Oracle shadow brain and buy trade history`는 초기 Site UI CI가 잡은
  미리보기 파일 절단을 `bcd8c62`에서 복원한 뒤 CI 4건 전체 통과, merge commit
  `5371f4a`로 기본 브랜치에 병합했다.
- Oracle의 실제 env 경로를 반영한 후속 PR #90
  `Fix Oracle watchdog environment drop-in`도 CI 2건 전체 통과, merge commit
  `7a0ce19`로 병합했다.
- Oracle autodeploy가 `7a0ce19`를 반영했고 서버 저장소는 clean이다.
- `watchdog.service`와 Oracle drop-in, `oracle-brain.service`·timer와 drop-in을
  설치했다. buyloop에는 `/var/lib/stock-oracle-brain/signals.json`과
  `ORACLE_SIGNAL_FALLBACK_ENABLED=0`을 명시한 서버 drop-in을 설치했다.
- `systemd-analyze verify`는 성공했다. 출력된 유일한 경고는 이 작업과 무관한
  기존 snapd의 미지원 `RestartMode` 지시자였다.
- watchdog와 oracle-brain timer를 enable+start했고, buyloop를 한 번 재시작해
  drop-in을 적용했다. 실제 buyloop 프로세스 환경에서 fallback=0을 확인했다.
- 첫 oracle-brain oneshot은 장외라
  `{"ok": true, "status": "market-closed", "analyzed": 0}`으로 정상 종료했다.
  CPU 시간은 1.542초였고 oracle-brain 오류 로그는 0건이다. oneshot 서비스가
  실행 뒤 inactive인 것은 정상이고 timer는 active/enabled다.
- 배포 뒤 sentinel·watchdog·buyloop·telegram·portfolio-web는 active,
  실패 유닛 0, watchdog 재시작 0, heartbeat는 신선했다. buyloop 재시작 이후
  신규 매수 주문 접수 로그는 0건이다.
- kill-switch는 계속 L1이며 실행 확인 결과
  `buy_new=False`, `protect_sell=True`다. L1 하향, fallback=1, 실전 하드블록
  해제는 하지 않았다.

다음 작업은 코드 배포가 아니라 관찰 게이트다. 한/미 각 한 장에서 oracle-brain
실분석의 실행시간·RSS·오류·GitHub 대비 신호 차를 수집한 뒤, L1 상태에서만
GitHub 60분 장애주입을 수행해 신규주문 0을 확인하고 재검토한다. fallback=1은
그 재승인 뒤에만 검토한다. L1 해제 전에는 기존 BUY 16건 대사(BAM 잔고 0,
LW 보호수량 불일치 포함)와 총시드 초과 해소가 여전히 별도 선결조건이다.

### 13. 2026-07-28 legacy BUY 16건 이관·SELL ACK 수정 (검토 전)

로컬 브랜치 `codex/legacy-ledger-migration`을 최신 기본 브랜치 `9259bdb`에서
만들었다. Claude가 전체 diff를 검토할 수 있도록 이 브랜치에만 commit/push하며,
기본 브랜치 merge·Oracle 운영 배포·장부 apply는 하지 않았고 kill-switch L1도
그대로다.

확인한 실제 결함:

- 업그레이드 전 BUY 16건은 full-filled지만 `accounted=0`, `fx/pos_key`가 없어
  총시드 예약이 fail-closed로 계속 유지된다.
- CAG·KKR·LW 절반익절은 KIS 잔고가 각각 13·7·12주 감소했지만 ACK에 남았다.
  baseline 차단뿐 아니라 당시 `hldg_before`가 전체 보유가 아닌 주문수량으로
  기록된 것이 직접 원인이다.
- SELL 주문 한 건의 residual 0을 종목 전체청산으로 보고 `kis_positions.close`
  하던 코드가 있었다. 절반익절 ACK가 풀리면 남은 절반 보호기록을 삭제할 수 있어
  해당 강제 close를 제거했다.
- 잔고대사 SELL의 가격 fallback으로 KIS 보유 평단을 사용해 실현손익을 왜곡하던
  경로를 주문 제출가 fallback으로 분리했다.

구현:

- `bot/legacy_migration.py`: mock/L1/서비스 정지/5분 snapshot/plan SHA operator
  ack를 모두 요구하는 1회 이관 도구. plan은 읽기 전용이며 apply도 주문 API를
  호출하지 않는다. apply는 운영자 플래그뿐 아니라 sentinel/buyloop가 실제
  inactive이고 runtime mask 상태인지 확인하며, mutation 전 새 전용 디렉터리에
  주문·포지션·원가장부를 byte-for-byte 백업하고 SHA-256 manifest를 fsync한다.
- original BUY lot·보호 포지션을 event_id로 멱등 복원하고, 검증된 SELL을 먼저
  회계한 뒤 최종 costbook·position qty가 broker와 같을 때만 BUY accounted를
  남긴다. 중간 fault에서는 예약이 계속 유지된다.
- baseline 예외는 `legacy_migrated` 포지션·동일 pos_key·동일 original 수량의
  costbook lot이 모두 맞는 SELL에만 허용한다. 순수 사용자 보유에는 열리지 않는다.
- 이후 새 SELL은 주문수량이 아니라 주문 직전 전체 매도가능수량을
  `hldg_before`로 기록한다.
- 이관 ACK 대사는 CAG/KKR/LW의 exact 주문키만 허용한다. 후보에 없는 같은 시장
  ACK를 누락 잔고 0으로 오귀속할 수 있던 자체 적대검토 반례를 차단했다.
- 이관된 과거 BUY 가격과 잔고로 증명한 SELL 제출가는 거래이력에서 각각
  `장부 복원 가격`, `주문가 기준 매도가`로 표시해 실제 체결가로 과장하지 않는다.

실제 Oracle에는 변경본을 배포하지 않고 `/tmp/legacy-migration-review` overlay를
사용해 운영 장부와 KIS 모의잔고를 읽기만 했다. plan 16/16 생성에 성공했고 분류는
현재보유 12, ACK 잔고대사 3(CAG/KKR/LW), 완전청산 1(BAM)이다. original 합계
980주, broker current 합계 920주, 주문 전송·운영 JSONL 변경·서비스 재시작·L1
변경은 모두 0이다. 생성 plan은 5분 만료형이라 apply에 재사용하지 않는다.

검증:

- 집중 migration/ACK/boot/accounting 테스트 통과.
- BUY accounted 직전 fault injection 뒤 예약 유지와 동일 plan 멱등 복구 통과.
- 잘못된 ack, 서비스 미정지, snapshot 변경, broker qty 초과, pos_key 불일치
  fail-closed 통과.
- 실제 서비스 active/unmasked/상태조회 오류, 대상 밖 ACK, NaN/Infinity,
  ACK reconcile 직후 crash와 BUY accounted 직전 crash를 모두 차단·복구했다.
- apply 전 백업 권한·크기·SHA-256 manifest 및 거래이력 추정가 표기를 검증했다.
- Python compileall 및 독립 Python 테스트 모듈 `45/45` 통과.
- Node 계산 테스트 `8/8`, JavaScript·shell 문법, `git diff --check` 통과.

Claude 검토 요청서는
`docs/CLAUDE_REVIEW_LEGACY_MIGRATION_ACK_FIX.md`다. 다음 순서는 Claude P0/P1
적대 검토다. 사용자 지시로 검토용 브랜치 commit/push까지만 허용됐으며, 승인
전에는 기본 브랜치 merge·Oracle apply를 하지 않는다.
승인 뒤에도 L1 해제는 총시드·열린 주문·보호수량 재검증을 거치는 별도 게이트다.
실제 apply 런북에서는 sentinel/buyloop를 stop한 뒤 runtime mask하고, 검증 완료
후 반드시 `systemctl unmask --runtime`한 다음 재시작한다.

### 14. 2026-07-28 legacy 이관 Claude 1차 차단 수정 (재검토 대기)

Claude 1차 적대검토는 16개 반증질문 중 14개가 HOLDS였으나 P1 2건과 apply 전
P2 4건을 확인해 merge·Oracle apply·L1 해제를 차단했다. 이 브랜치에서 6건을
모두 수정했으며 Oracle 운영 코드·장부·서비스·L1에는 손대지 않았다.

P1 수정:

- SELL costbook close가 fsync된 뒤 `kis_positions.apply_sell_fill` 전에
  프로세스가 죽어도, 같은 `fill_event`가 costbook `event_results`에 있으면
  legacy lot를 다시 시딩하지 않는다. BAM 완전청산 장애주입에서 재실행 후에도
  `buy_cost`, `sell_proceeds`, 실현손익, 열린 수량이 각각 정확히 한 번만
  남는 것을 확인했다.
- 구버전 `balance-average`는 실제 SELL 체결가가 아니라 보유 평단 오염값으로
  취급한다. 이관 plan은 해당 값을 버리고 원 주문 제출가를
  `submitted-fallback` 추정값으로 사용하며, 거래이력도
  `price_estimated=true`, `verified=false`로 표시한다.
- 이관 실현손익은 apply 실행일이 아니라 원 SELL `submitted_at`의 KST 거래일에
  귀속해 현재 일일손실 서킷브레이커를 오염시키지 않는다.

P2 수정:

- `PLAN_VERSION=2`에 주문·포지션·원가장부 절대경로를 넣고 apply 시 현재 경로와
  완전일치하지 않으면 거부한다. `ORDER_LEDGER_PATH` 기본값도 cwd 상대가 아닌
  `ledger.py` 기준 절대경로로 바꿨다.
- apply 직전과 백업 직후 두 번 주문 원장·`kis_positions`·costbook 무손상,
  broker snapshot, 서비스 정지를 다시 확인한다. positions 손상은 백업·mutation
  전에 차단한다.
- systemd inactive+runtime mask뿐 아니라 `pgrep`으로 수동
  `bot.sentinel`/`bot.kis_buyloop` 프로세스가 0개인지 확인한다. heartbeat가
  120초 이내로 신선해도 거부한다. 따라서 실제 apply는 서비스 정지·mask 뒤
  heartbeat가 120초를 넘은 후 새 5분 plan을 생성해야 한다.
- 잔고 ACK 대사는 `complete_snapshot=True` 명시 또는 exact `only_keys`가
  없으면 0건을 반환한다. 부분 hmap으로 관계없는 SELL을 잔고 0으로 오인하는
  미래 호출자 반례를 계약 자체로 차단했다.

추가 방어로 비숫자 `legacy_hldg_before`는 해당 주문만 보류해 대사 배치 전체를
깨지 않게 했다. 집중 migration/ACK/accounting/boot/trade-history 테스트,
SELL close→position 중간 크래시, 원장 경로 불일치, positions 손상, 백업 후
수동 프로세스 재등장 장애주입을 통과했다. 전체 Python `45/45`, Node `8/8`,
`compileall`, JavaScript 문법과 `git diff --check`도 통과했다.

Claude 재검토서는
`docs/CLAUDE_REVIEW_LEGACY_MIGRATION_ACK_FIX_V2.md`다. 재승인 전에는 기본
브랜치 merge·Oracle 코드 배포·장부 apply·L1 해제를 모두 금지한다. 승인 뒤에도
Oracle apply와 L1 해제는 서로 다른 게이트다.

### 15. 2026-07-29 전략 A 정체청산·성과/지수 동시 리베이스 (재검토 전)

Claude가 legacy 이관 v2를 `P0/P1/P2 없음`으로 승인했고 PR #92가 merge commit
`ba30f9c7`로 기본 브랜치에 병합된 상태에서, 별도 깨끗한 worktree와
`codex/stall-exit-performance-rebase` 브랜치를 만들었다. 예전
`Stock-chart-analyze-site` worktree의 대량 미커밋 변경은 건드리거나 덮지 않았다.
이 섹션 작업은 아직 Oracle에 배포하지 않았고 장부 apply·L1 해제도 하지 않았다.

전략 A의 `+1R 절반익절` 체결 확정 뒤 잔량 관리에 정체 규칙을 추가했다.
유효 시세가 들어온 열린 시장의 고유 거래일만 세며, 15거래일 동안 직전 정체
기준보다 `+0.25R` 이상 높은 신고가가 없으면 추적폭을 1.5R에서 1.0R로 좁힌다.
30거래일이면 기존 KIS 매도·원장·대사 경로로 잔량을 정리한다. 의미 있는
신고가가 나오면 정체일을 0으로 되돌리고 1.5R 폭으로 복원하지만 이미 올라간
손절선은 절대 내리지 않는다. 전략 B에는 적용하지 않는다.

KIS와 autopaper가 `bot/stall_exit.py`의 같은 순수 상태 전이를 사용한다.
절반익절 ACK/부분체결, 같은 날짜 반복, 휴장·장마감, 시세 0/NaN은 정체일을
늘리지 않는다. 상태 손상은 즉시 매도하지 않고 기존 보호를 유지한 채 0일부터
다시 세며 같은 손상 바이트의 치명 경보는 한 번만 보낸다.
`STALL_EXIT_MODE=off|shadow|live`이고 기본값과 알 수 없는 값은 `off`다.
`shadow`에서는 15/30일 제안만 알리고 신규 래칫·매도 주문은 0건이다.

Git에 이미 공개된 15종목의 일봉과 현행 `now` 게이트로 공통 진입 22건을
워크포워드 비교했다. 개인 KIS 보유목록은 외부 시세 서비스로 보내지 않았다.

- 10/20: 총 `+1.082R`, 평균 14.27거래일, 최대 이론 시드점유 48.2%
- 15/30: 총 `+0.832R`, 평균 14.32거래일, 최대 이론 시드점유 48.2%
- 20/40: 총 `+0.582R`, 평균 14.32거래일, 최대 이론 시드점유 48.2%

표본이 작고 차이가 AAPL 2건에 집중돼 10/20으로 과최적화하지 않았다.
중간안 15/30을 구현하되 기본 `off`, 병합 후에도 1–2주 `shadow`와 재검토를
거친 뒤 별도 승인 때만 `live`로 바꾼다. 상세는
`docs/STALL_EXIT_BACKTEST_2026-07-29.md`다.

기존 지수 대비 약 `-17%`는 보유종목의 하루 폭락이 아니라 legacy BUY 회계
공백과 부분수집 비교가 섞여 만들어진 신뢰할 수 없는 누적 기준으로 판정했다.
legacy migration apply가 16건 모두를 검증·accounted 처리한 뒤에만
계좌 TWR·전략 A/B·지수의 성과 epoch를 같은 첫 표본 0%로 새로 시작한다.
과거 지수 가격 데이터 자체를 삭제하지 않지만, 오염된 계좌 구간과 공정하게
이어 붙일 수 없어 이전 성과 구간은 운영 차트에서 제외하고 apply 직전
`alpha_state.json` forensic 백업으로 보존한다. 같은 plan SHA 재실행은 새
성과를 다시 초기화하지 않는다.

화면은 1개월·3개월·전체를 새 epoch부터 일별 복리로 장기 누적한다.
장 시작 보유 동일가중 값은 전체 대상의 시세가 모두 모인 경우에만 표시하고
1/16 같은 부분수집은 `자료 부족 1/16`으로 숨긴다. `KIS 전체`라는 오해 소지가
있는 명칭도 `봇 운용자산 TWR`로 바꿨다.

검증은 전체 Python 테스트 모듈 `46/46`, Node 계산 `9/9`, Python compileall,
두 JavaScript 문법 검사와 `git diff --check`를 통과했다. 재검토서는
`docs/CLAUDE_REVIEW_STALL_EXIT_PERFORMANCE_REBASE.md`다. 구현 커밋
`7bee849a`를 원격 브랜치에 push했고 Draft PR #93을 기본 브랜치 대상으로
열었다. 구현·인수인계 HEAD `dc4fd908`에서 GitHub `CI`와 `Site UI CI`가 모두
성공했다. 다음 순서는 Claude P0/P1 적대 재검토다. 승인 전에는
병합·Oracle 배포·장부 apply·L1 해제·정체청산 live 전환을 모두 금지한다.

### 16. 2026-07-29 PR #93 Claude 1차 차단 수정 (V2 재검토 대기)

Claude 1차 적대검토는 정체청산/성과 리베이스 반례 20개 중 17개가 HOLDS였으나
`P0 1건`, `P1 2건`, `P2 3건`을 확인해 PR #93 병합을 차단했다. 모든 항목을
같은 깨끗한 `codex/stall-exit-performance-rebase` worktree에서 수정했으며,
예전 `Stock-chart-analyze-site` 미커밋 변경은 건드리지 않았다. Oracle 배포,
legacy apply, L1 해제, 정체청산 live 전환은 하지 않았다.

차단 결함 수정:

- 닫힌 시장 종목을 현재 `held`에서 찾을 수 없다는 이유로 정체 상태를 지우지
  않는다. 시장과 무관한 `kis_positions` 원장에 실제 포지션이 남아 있는 동안
  상태를 보존하고 원장 `close` 뒤에만 제거한다.
- autopaper의 stop0 없는 legacy half 포지션은 현재 래칫 stop으로 R을 역산하지
  않는다. 초기 stop이 증명되지 않으면 기존 3×ATR만 유지하며, 유효 R이 있어도
  15일 전에는 기존 3×ATR, 15일부터만 1.0R 폭을 적용한다.
- 리베이스 첫날의 `daily_indices`를 계좌와 같은 첫 표본 기준으로 바꿨다.
  이후 날짜만 전일종가를 쓰므로 첫날 오버나이트 갭이 장기 창에 영구 복리되지
  않는다.

P2와 복구성 보강:

- `kis_positions.jsonl`에 멱등 `half_done` 이벤트를 추가했다. 상태 JSON 손상 시
  닫힌 시장을 포함한 모든 원장 포지션을 먼저 격리하고, durable half 증명이
  없는 종목은 +1R 재매도와 21일 타임스탑을 모두 보류한다. sentinel 하드 손절은
  계속 동작한다.
- 모든 이관 회계와 BUY accounted가 끝난 뒤 alpha rebase에서 장애가 난 경우를
  위한 주문 없는 `recover-performance` 명령을 추가했다. 만료 plan 허용은 이
  경로에만 한정하며 별도 `RECOVER <sha>` ack, 동일 broker snapshot, 완료된
  3원장, 원본 4파일 backup manifest의 SHA/크기를 모두 확인한 뒤 epoch만
  멱등 전환한다.
- 1개월·3개월·전체의 장 시작 보유 동일가중은 선택 기간 모든 날짜가
  `covered == eligible > 0`일 때만 복리한다. 하루라도 부분수집이면 해당 비교를
  숨기고 `부분수집 N일 · 지수 비교 제외`로 표시한다.

비차단 항목도 함께 보강했다. 리베이스 직후 빈 epoch를 ntfy 캐시에 즉시 발행해
옛 `-17%` 잔존을 없앴고, off/shadow KIS 기본 보호폭을 환경값과 무관하게 1.5R로
고정했으며, 30일 청산 재시도 때 보호선 상향을 매도보다 먼저 기록한다.

날짜 의미는 기존 요청 사양인 고유 KST 날짜를 유지했다. 미국 한 세션이 KST
자정을 넘으므로 이름상의 30거래일보다 일찍 도달할 수 있다. 이를 조용히 다른
정의로 바꾸지 않고 shadow 1~2주에서 실제 세션 수를 함께 확인한다.
shadow→live 전환 전에는 누적 정체 상태를 종목별로 사람이 검토해 일괄 청산
가능성을 별도 승인해야 한다.

검증:

- 전체 Python 독립 테스트 모듈 `46/46`
- Node 계산 테스트 `10/10`
- Python compileall, 두 JavaScript 문법 검사, `git diff --check`
- 브라우저에서 오늘/전체 전환, 기간 부분수집 비교 차단, 비교선 토글 확인

V2 재검토 요청서는
`docs/CLAUDE_REVIEW_STALL_EXIT_PERFORMANCE_REBASE_V2.md`다. 다음 순서는 이
수정본을 PR #93에 push한 뒤 CI와 Claude 재검토를 받는 것이다. 승인 전에는
병합·Oracle 배포·legacy apply·L1 해제·live 전환을 계속 금지한다.

수정 구현 커밋 `d0f001d1`을 기존 Draft PR #93 브랜치에 push했고 GitHub
`CI` run #94와 `Site UI CI` run #50이 모두 성공했다. 클로드 전달용 전체
V2 묶음은
`/Users/seop/Documents/매매봇/CLAUDE_REVIEW_STALL_EXIT_PERFORMANCE_REBASE_PR93_V2_FINAL.zip`
이며 4개 구현 patch, V1/V2 검토서, 백테스트, 이 인수인계서를 포함한다.

### 17. 2026-07-29 Oracle legacy 16건 apply·PR #94·커널 재부팅 완료

Claude가 PR #93 V2를 `P0/P1/P2 없음`으로 승인한 뒤 정확한 승인 head
`cb7401cf`를 merge했고, 기본 브랜치 merge commit은 `a7831ba8`이다.
Oracle은 이 커밋을 반영한 상태에서 장외 유지보수를 시작했다. 시작 전 모의계좌,
kill-switch L1, `buy_new=False`, `protect_sell=True`, 원장 정상, 보유 16종목,
열린 주문 0을 확인했다.

실제 KIS 보유는 아래와 같았다. 사용자가 적은 `GPT 123`은 실제 종목코드
`GPK 123`으로 확인했다.

- ALK 8, AQN 129, BIPC 17, CAG 13, CHYM 94, GPK 123
- KKR 7, LW 13, MAIN 92, PUK 24, SNN 25, STE 3
- TAP 80, VRSK 3, WAL 13, WDAY 2

legacy 이관 대상은 ALK를 제외한 기존 BUY 16건이다. 이 중 15종목은 현재
보유이고 BAM 28주는 전량청산되어 broker current 0이다. 모든 이관 대상의
original/current/sold 불변식을 새 5분 plan으로 확인했다.

첫 apply는 주문을 한 건도 보내지 않고 fail-closed로 중단됐다. 과거 잘못된
`hldg_before` 때문에 AQN/CAG/GPK/LW/SNN/VRSK가 이미 close-only 동결돼
있었고, 이관이 durable pos_key·보호수량·원가 lot의 3중 증명을 만든 뒤에도
generic freeze 게이트가 먼저 실행되어 ACK 대사를 건너뛰는 운영 반례였다.
첫 apply 직전 원본 4원장은
`/home/ubuntu/legacy-backups/legacy-20260729T2334Z`에 보존됐고 manifest
SHA-256은
`a7367ff88db6770c26fbbfd6d4ef89877398ed9f88201a4741e638703c0c6488`이다.

일반 동결은 그대로 막고, durable 3중 증명을 통과한 legacy SELL에만 frozen
예외를 허용하도록 `bot/kis_reconcile.py`를 좁게 수정했다. 검증된 frozen
legacy ACK는 해소되고 같은 잔고 delta라도 증명 없는 일반 frozen SELL은
계속 보류되는 회귀 테스트를 추가했다. `tests.test_kis_ack_resolve`,
`tests.test_legacy_migration`, `git diff --check`가 통과했고 PR #94의 CI
2건도 모두 성공했다.

- 수정 커밋: `8c4ed9cf`
- PR: `#94 Fix verified frozen legacy ACK reconciliation`
- 기본 브랜치 merge commit: `1eb1a94a`

Oracle을 `1eb1a94a`로 fast-forward한 뒤 서버 자체 회귀 테스트를 다시
통과했다. 새 plan SHA
`dfa6c0e79046bc02dfa54e671eeb4e6aae3ff056159902ecf37906dd8fc719a2`로
멱등 apply를 재실행해 16/16 이관을 완료했다. `orders_sent=0`이며 두 번째
apply 직전 백업은
`/home/ubuntu/legacy-backups/legacy-20260729T2338Z`, manifest SHA-256은
`8ef125d9c003ec152cb91d41fbdc4a6dcc4c517e2c79e79b3f4886f8f70162d8`다.
성과 epoch도 같은 plan SHA로 리베이스됐다.

apply 직후 대조:

- 16개 plan entry 모두 broker current = `kis_positions` qty = costbook lot qty
- 모든 legacy BUY `accounted == original_qty`
- 10개 과거 SELL은 `filled`이고 `accounted == sold_qty`
- 주문 원장·costbook healthy, 열린 주문 0, `kis_positions` 16종목
- 거래이력 29행 생성, 성과/지수는 오염된 `-17%`를 잇지 않고 새 epoch에서
  일별 복리 누적 시작
- A 시드 30,000,000원 + B 시드 5,000,000원 = 총시드 35,000,000원
- 5% 완충 후 운영한도 33,250,000원, 현재 장부 운용원가 26,214,776.70원
- 웹 `/app/`과 `/api/portfolio.json` 200, 쓰기 요청 POST 405

서비스 unit이 `/etc/systemd/system`에 직접 배치돼 있어 runtime mask 링크가
우선순위상 가려지는 서버 특이점이 있었다. 원본 unit을 별도 사본과 SHA-256으로
보존하고 유지보수 동안만 `/dev/null` mask로 교체했다. apply 후 원본 해시 일치
확인, unit 복원, enable/start까지 완료했다.

정체청산은 `/etc/systemd/system/sentinel.service.d/stall-shadow.conf`로
`STALL_EXIT_MODE=shadow`만 켰다. 실제 프로세스 환경에서 mock,
`SENTINEL_LIVE=1`(모의 보호매도), fallback 0, L1을 확인했다. `shadow`는
15/30일 제안만 기록하며 정체청산 주문은 보내지 않는다. `live` 전환은 하지
않았다.

설치돼 있던 Oracle 커널 `6.8.0-1058-oracle` 적용을 위해 열린 주문 0과 L1을
재확인한 뒤 서버를 재부팅했다. 재부팅 후:

- 실행 커널 `6.8.0-1058-oracle`, `/var/run/reboot-required` 없음
- sentinel, buyloop, watchdog, portfolio-web, telegram,
  autodeploy.timer, oracle-brain.timer 모두 active/enabled
- 실패 unit 0, heartbeat 약 23초, 서비스 경고 0, 신규 주문/체결 로그 0
- 원장·costbook healthy, 열린 주문 0, 보유 원장 16종목
- L1 `buy_new=False`, `protect_sell=True`, shadow와 fallback 0 유지

과거 오대사로 생긴 AQN/CAG/GPK/LW/SNN/VRSK의 close-only freeze 표식은
보존했다. 이 표식은 보호매도를 막지 않지만 향후 해당 종목 재매수는 막는다.
운영 반례 해소만으로 안전장치를 조용히 내리지 않기 위해 자동 unfreeze하지
않았다. L1 하향과 이 6개 unfreeze는 별도 operator 승인 항목이다.

00:01 UTC 자동 후속에서 미국 정규장과 연장장 종료(ET 20:01), Oracle
`37d9d4a8`·clean·mock·L1, 5개 핵심 서비스 active/enabled, 실패 unit 0,
heartbeat 정상, 열린 주문 0을 다시 확인했다. 이관과 재부팅이 장 마감 뒤여서
9개 과거 절반익절 종목의 `half_done`·본전 래칫은 아직 첫 정규장 관리
사이클을 거치지 않았다. 원장상 목표/확정 체결은 각각
AQN 128/128, CAG 13/13, GPK 123/123, KKR 7/7, LW 12/12,
SNN 24/24, STE 3/3, VRSK 2/2, WDAY 2/2이고 잔여·pending은 모두 0이다.
따라서 다음 미국 정규장 첫 `kis_exits.manage()` 사이클 뒤 이 9개가 durable
`half_done=true`, 보호선 `stop >= entry`가 되는지 재확인한다. 그 전에는
L1을 낮추거나 수동으로 래칫 상태를 조작하지 않는다.

다음 순서는 mutation이 아니라 관찰이다.

1. 정체청산 shadow를 1–2주 관찰하며 미국 세션 수, 15/30일 후보와 제안값을
   종목별 검토한다. 누적 30일 종목을 확인하기 전 `live`로 바꾸지 않는다.
2. oracle-brain을 한/미 각 한 세션 관찰하고, L1 상태에서 GitHub 60분
   장애주입으로 fallback 주문 0을 확인한 뒤에만 fallback 1을 재검토한다.
3. L1 하향은 위 관찰과 frozen 6종목 처리 결정을 마친 뒤 별도 승인으로 한다.

### 18. 2026-07-29 L1 하향 준비상태 읽기 전용 점검기

최신 인수인계 기록을 다시 대조한 결과, legacy 16건 이관·총시드 한도·열린 주문
0·3원장 수량 일치·커널 재부팅은 완료됐다. 그러나 다음 항목은 PR #96 시점에도
완료 증거가 없다.

- 다음 미국 정규장 관리 사이클에서 과거 절반익절 9종목의 durable
  `half_done=true`, `stop >= entry` 확인
- 정체청산 `shadow` 최소 1주 관찰
- oracle-brain 한국·미국 각 1세션 관찰
- L1 상태의 GitHub 60분 장애주입과 신규주문 0 확인
- close-only 동결 6종목을 유지하거나 해제할지 별도 운영자 결정

이 조건을 말로만 확인하고 L1을 내리는 실수를 막기 위해
`codex/l1-readiness-audit` 브랜치에서 읽기 전용 점검기를 구현했다. 이 변경은
아직 기본 브랜치 병합·Oracle 배포 전이다.

- `bot/l1_readiness.py`: 런타임 상태와 관찰 증거를 fail-closed 게이트로 판정.
- `scripts/kis_l1_readiness.py`: 사람이 실행하는 GO/NO-GO CLI. 주문 모듈을
  불러오지 않고 kill-switch를 변경하지 않는다.
- `infra/server/l1-readiness-evidence.example.json`: 비밀값 없는 관찰 증거 형식.
- `tests/test_l1_readiness.py`: 증거 누락·노후, 열린 주문, 미회계 BUY, 총시드 초과,
  3원장 수량 불일치, heartbeat 노후, fallback 활성, shadow 부족, 고정된 9종목
  래칫 목록 축소, 6종목 동결 결정 누락을 각각 NO-GO로 검증한다.

CLI에서 `--broker`를 사용하면 KIS 잔고·미체결 조회 API만 호출해 브로커,
`kis_positions`, costbook 수량을 대조한다. 조회 실패나 응답 필드 결손도 열린
주문 0으로 추측하지 않고 NO-GO다. 모든 기술 게이트가 통과해도 결과는
`ready_for_operator_review`이며 L1 자동 하향은 없다. 실제 하향은 별도 사용자
승인과 운영자(operator) ack가 계속 필요하다.

로컬 검증은 격리된 `.venv`에서 전체 Python 테스트 모듈 `47/47`, L1 준비도
집중 테스트 `8/8`, Node 계산 테스트 `10/10`, Python compileall, 두 JavaScript
문법 검사와 `git diff --check`를 통과했다. 이 결과는 소스 회귀검사이며 Oracle의
미완료 운영 관찰을 완료한 것으로 대체하지 않는다.

구현 커밋 `f3fb5c6`을 원격 `codex/l1-readiness-audit` 브랜치에 push했고,
기본 브랜치 대상 Draft PR #97
`Add read-only L1 readiness audit`을 열었다. PR 병합·Oracle 배포·L1 하향은
아직 수행하지 않았다.

사용자 요청으로 7일 shadow·Oracle 관찰·동결 결정이 일반 mock 신규매수의 L0
조건과 과도하게 결합됐는지 다시 검토하는
`docs/CLAUDE_REVIEW_L1_RELEASE_OPTIONS.md`를 작성했다. 이 요청서는 기존 계획
유지, 기능별 조건 분리, 한 종목·한 주문 카나리, L1 예외 주문, L0+`ALLOW_BUY=0`
대안을 비교한다. 특히 현재 보호원장 16종목의 A/B 귀속, Stage 상한,
Stage 1.5의 risk cap 0.1%와 매수루프 기본 risk 1% 불일치, heartbeat 60/120초
경계를 반례로 확인하도록 요구한다. Claude 판정 전에는 L1과 운영 설정을
변경하지 않는다.

### 19. 2026-07-29 Claude 판정 반영 — 제한적 L0 scope

Claude는 §8 반례 13개를 mock으로 실행했고 KIS 호출·주문은 0건이었다고
보고했다. 최종 판정은 선결조건 충족 후 대안 B인 **제한적 L0 허용**이다.
이 판정은 위 17·18절의 “모든 관찰 완료 후 L0 검토” 결론 중 제한적 L0 부분을
대체한다. 관찰 항목 자체는 stall live·fallback 1·동결 해제의 게이트로 계속
유효하다.
7일 shadow, Oracle 한·미 세션, GitHub 60분 장애주입, 9종목 래칫, 동결 해제
결정은 일반 GitHub 신호의 mock 신규매수와 독립된 기능 조건이며, 기존
`l1_readiness.evaluate()`가 이를 하나의 `ready`로 묶은 것은 과결합이라고
판정했다. 전체 회신 요약은
`docs/CLAUDE_L1_RELEASE_OPTIONS_RESULT.md`에 보존했다.

PR #97에서 점검기에 두 승인 범위를 추가했다.

- `--scope strict`(기본): 기존 관찰 증거를 모두 차단 조건으로 유지한다.
- `--scope l0`: 독립 기능 관찰은 `INFO`로 표시하되, mock·L1 유지·원장·
  열린 주문·회계·예산·3원장 수량·heartbeat 60초·fallback 0을 계속 차단
  조건으로 둔다.
- `l0`에는 기존 게이트를 단순히 완화하지 않고 `STALL_EXIT_MODE=shadow`,
  동결 6종목의 실제 유지, `TRADE_STAGE=mirror`, 비어 있지 않은
  `ALLOWED_SYMBOLS`, `ALLOW_BUY=1`, `KIS_ORDERS_ENABLED=1`을 별도
  차단 게이트로 추가했다.
- JSON `context.position_counts_by_sleeve`에 보호원장의 A/B 보유 종목 수를
  출력해 Oracle에서 실제로 어느 슬리브가 열리는지 확인할 수 있게 했다.

이 변경도 읽기 전용이다. 아직 PR 병합, Oracle 배포, 환경변수 변경, L1 하향,
동결 해제, 주문 전송을 수행하지 않았다. 다음 단계는 PR #97 CI 확인 후 병합·
Oracle 배포를 별도로 승인받고, L1을 유지한 채 아래 명령의 JSON 증거를
확보하는 것이다.

```bash
python scripts/kis_l1_readiness.py --scope l0 --broker --json
```

`ready_for_operator_review=true`여도 자동 하향은 없다. 실제 L0 전환은
`docs/CLAUDE_L1_RELEASE_OPTIONS_RESULT.md`의 승인 문구와 Oracle 최신 결과를
사용자가 확인·승인한 뒤에만 진행한다.

로컬 검증은 L1 readiness 집중 테스트 `13/13`, 전체 Python 테스트 모듈
`47/47`, Node 계산 테스트 `10/10`, Python compileall, 두 JavaScript 문법
검사와 `git diff --check`를 통과했다. 이는 코드 검증이며 Oracle 운영 증거를
대체하지 않는다.

### 20. 2026-07-29 Oracle 제한적 L0 실행 인수인계

사용자가 “이 컴퓨터에서 가능한 작업은 모두 처리하고 원격 push하라”고 요청했다.
2026-08-05 대기는 제한적 mock L0의 조건에서 해제됐으며, 기다림 없이 진행할
수 있는 정확한 Oracle 절차를 `docs/ORACLE_LIMITED_L0_RUNBOOK.md`에 추가했다.

런북은 처음 보는 운영자도 다음 순서로 실행할 수 있게 구성했다.

1. PR #97 병합 여부와 Oracle clean fast-forward 배포 확인
2. mock·mirror·비어 있지 않은 allowlist·fallback 0·stall shadow·동결 유지
3. 실행 중 sentinel/buyloop 프로세스에서 비밀값을 제외한 안전 설정 확인
4. L1 상태의 `--scope l0 --broker --json`과 `blockers=[]` 확인
5. A/B 실제 개수와 allowlist를 사용자에게 제시하고 별도 승인
6. operator ack가 포함된 L1→L0 한 번 실행
7. 첫 주문 회계·보호선 확인과 이상 시 즉시 L1 rollback

런북 명령은 실제 Oracle 배치인 `/home/ubuntu/Stock-chart-analyze`,
`/home/ubuntu/kis.env`, 서비스 사용자 `ubuntu`, 저장소 `.venv`를 기준으로
작성했다. `/opt/stock`·`/etc/stock/kis.env`는 새 표준 설치 예시일 뿐 현재
Oracle에 사용하면 안 된다.

기존 `infra/server/README.md`의 점검 설명도 `l0`와 `strict`로 분리했다.
`l0`에서 7일 shadow·Oracle 세션·장애주입 등이 `INFO`라는 사실과, 이 조건이
stall live·fallback 1·동결 해제·실전 전환에는 계속 필요하다는 경계를 명시했다.

현재 이 컴퓨터에서 Oracle 접속은 불가능하다. 따라서 PR 병합, Oracle 배포,
환경변수 변경, 브로커 조회, L1 하향, 주문은 수행하지 않았다. 다음 컴퓨터는
PR #97과 위 런북만 확인하면 환경 작업을 이어갈 수 있다.

### 21. 2026-07-29 PR #97 병합·Oracle 배포와 L0 감사기 후속

사용자가 제한적 KIS mock L0 신규매수를 명시적으로 승인했다. 승인 범위는
`STALL_EXIT_MODE=shadow`, Oracle fallback 0, close-only 동결 6종목,
실전계좌 하드블록을 유지하는 제한적 L0다. 정확한 `ALLOWED_SYMBOLS`는 아직
사람이 정하지 않았으므로 L1 하향은 하지 않았다.

PR #97의 4개 CI가 통과했고 exact head `e213aff4`를 기본 브랜치에 병합했다.
merge commit은 `5321fc7f`다. Oracle도 clean fast-forward로 같은 commit에
배포했으며 sentinel/watchdog/buyloop/telegram/portfolio-web, 관련 timer,
heartbeat와 원장 무결성이 정상이다.

L1을 유지한 첫 읽기 전용 감사
`scripts/kis_l1_readiness.py --scope l0 --broker --json`은 두 사유로
NO-GO였다.

1. `ALLOWED_SYMBOLS`가 비어 있음. 이는 자동으로 종목을 고르지 않고 사용자
   선택을 기다리는 의도된 차단이다.
2. 미국주만 16종목 보유 중인데 KIS mock의 국내 미체결 API가 미지원 응답을
   반환해 `broker_open_orders=None`으로 판정됨. NASD/NYSE/AMEX 미체결은
   각각 정상 응답 0건이고 로컬 열린 주문도 0이었다.

두 번째 항목은 미국-only 제한적 L0를 실제 주문 위험과 무관한 미지원 API가
막는 운영 결함이다. `codex/l0-mock-open-orders-fix`에서 보유 포지션·로컬
열린 주문·allowlist로 관련 시장을 계산해 그 시장만 미체결 조회하도록 좁혔다.
미국-only이면 국내 API를 건너뛰고, KR 종목이 하나라도 관련되면 국내 조회
실패를 계속 fail-closed로 차단한다. 시장 범위를 증명할 수 없을 때는 양쪽을
모두 조회한다.

집중 준비도 테스트와 ACK/주문·회계 회귀는 통과했다. 로컬 전체 테스트에서
`pandas`/`numpy`가 필요한 8개 모듈만 작업공간 시스템 Python의 의존성
미설치로 실행되지 않았으며, 코드 실패로 판정하지 않는다. 원격 CI와 Oracle
`.venv`에서 전체 의존성 환경 재검증 후 병합·배포한다.

다음 순서:

1. 위 후속 패치 PR의 CI와 Oracle `.venv` 회귀를 통과시킨다.
2. L1에서 `--scope l0 --broker`를 다시 실행해 차단 사유가 allowlist 하나만
   남는지 확인한다.
3. 현재 신선한 후보와 동결·기보유 여부를 사용자에게 제시하고 정확한
   `ALLOWED_SYMBOLS`를 승인받는다. Codex가 임의로 종목을 선택하지 않는다.
4. 승인 목록만 `/home/ubuntu/kis.env`에 원자적으로 반영하고 서비스를 재시작한
   뒤 감사를 다시 통과시킨다.
5. 사용자 승인 문구를 operator ack로 남겨 L1→L0 한 번만 실행하고, 첫
   매수루프에서 allowlist·회계·보호선·수량을 검증한다. 이상 시 즉시 L1로
   되돌린다.

같은 후속 개발에서 성과/지수 비교가 legacy 이관 epoch 이후 공통 0% 기준의
일별 복리 누적으로 표시되고 예전 `-16%~-17%` 오염 구간이 라이브 응답에
재등장하지 않는지 검증한다. 또한 주문과 분리된 읽기전용 익절 사후추적을
추가한다. 확정된 수익 매도마다 1·3·5·10·20거래일의 종가·최고가를 추적해
평단 대비 추가 상승과 매도가 대비 놓친 상승을 함께 표시하고, 슬리브·청산
사유·부분/전량·당시 R값별 공통점을 표본 수와 함께 집계한다. 이 기능은 기존
시세 캐시/공개 일봉만 사용하고 KIS 호출·주문·kill-switch에 영향을 주지
않아야 한다.

후속 패치는 commit `25ee68ab`, PR #98로 원격에 올렸다. CI 2/2 성공 후 exact
head를 병합했고 기본 브랜치 merge commit은 `6ed8e78c`다. Oracle도 clean
fast-forward로 같은 commit에 배포했으며 서버 `.venv` 전체 Python 회귀
`47/47`이 통과했다.

배포 후 L1을 유지한 `--scope l0 --broker` 재감사에서는 국내 미지원 API의
거짓 차단이 사라졌다. 로컬·브로커 열린 주문 0, UNKNOWN·미회계 BUY 0,
운용원가 26,214,776.70원 ≤ 한도 33,250,000원, 3원장 수량 일치,
heartbeat 약 20초, mock·mirror·shadow·fallback 0·동결 6종목 유지가 모두
통과했다. A 12종목, B 4종목으로 두 슬리브 모두 현재 포지션 상한에 도달했다.
남은 blocking gate는 비어 있는 `ALLOWED_SYMBOLS` 하나뿐이다.

현재 실제 매수루프가 소비하는 신선한 후보는 A(now)
`259960, 192820, EQT, CEG, EXE`, B(shelf) `MARA, ALK, TBBK, CLBK`다.
국내 종목을 allowlist에 넣으면 KIS mock 국내 미체결 API 미지원 때문에 감사가
의도대로 fail-closed되므로 제한적 L0 첫 allowlist는 미국 종목만 사용해야 한다.
`ALK`는 이미 보유 중이다. 사용자에게 미국 미보유 후보 중 정확한 목록 승인을
받기 전에는 `/home/ubuntu/kis.env` 변경이나 L1→L0 하향을 하지 않는다. 현재
A/B 상한 때문에 L0를 내려도 즉시 신규주문은 없고, 향후 자연청산으로 해당
슬리브가 상한 아래가 될 때 승인 목록의 fresh 신호만 주문 가능해진다.

### 22. 2026-07-29 누적 지수 라이브 검증과 익절 사후추적 구현

Oracle 라이브 `/api/performance.json`을 확인했다. epoch는
`장부 이관 후 새 기준`, basis는 `account_and_indices_same_first_sample`,
시작시각은 `2026-07-28T23:38:33.132814+00:00`이다. 예전 `-16%~-17%`
구간은 응답에 없고, 현재 첫 미국 세션 표본은 계좌 `+0.0798%`, 나스닥
`-0.0817%`, S&P500 `+0.0151%`다. 새 epoch 이후 마감된 일별 행은 아직
0개이며 오늘 세션부터 계좌·A·B·지수를 같은 0% 기준의 일별 복리로 장기
누적한다. 첫날 계좌와 지수 모두 `first_sample` 기준이라 기존 오버나이트 갭
불일치도 재발하지 않았다.

사용자 요청으로 수익 매도 뒤 추가 상승을 추적하는 읽기전용 분석을
`codex/post-exit-analysis`에서 구현했다.

- 다음 거래소 세션부터 1·3·5·10·20거래일의 최고가·종가·최저가 추적
- 평단 대비 총 상승, 익절 뒤 평단 기준 추가 `%p`, 매도가 대비 놓친 상승 분리
- 미국 체결 KST→뉴욕 세션 날짜 환산, 익절 당일 고가 제외
- 전략·청산사유·부분/전량별 공통점. 확정 체결 완료표본 최소 3건만 결론 표시
- 구버전 주문가 추정 10건은 개별 참고만 하고 통계에서 제외
- 별도 systemd timer가 공개 일봉만 갱신하고 atomic JSON 발행
- 개인 웹 HTTP 경로는 발행 파일만 읽어 KIS·외부 네트워크 추가 호출 0
- worker에 `kis.env`·KIS·주문·kill import가 없고 Nice/CPU/Memory 제한 적용

설계는 `docs/POST_EXIT_ANALYSIS_DESIGN.md`, 적대적 검토 질문은
`docs/CLAUDE_REVIEW_POST_EXIT_ANALYSIS.md`에 기록했다. 집중 계산 테스트는
거래소 날짜, 세 분모, 미완료 기간, 추정/확정 격리, 손실·무효행 제외,
원자발행·손상 fail-closed, 주문 plane 분리를 검증한다.

브랜치 `codex/post-exit-analysis`에 구현 commit `9f0c83a2`와 Oracle
원장경로 보정 commit `ee361547`을 푸시했다. 처음 systemd 드롭인이 원장을
저장소 루트의 존재하지 않는 파일로 덮어써 0건을 표시할 결함을 Oracle
실원장 대입 검증에서 발견했다. 경로 재정의를 전부 제거하고 각 장부 모듈의
`bot/*.jsonl` 고정 기본경로를 사용하게 했으며, 회귀 테스트가 systemd의
원장경로 override 재도입을 차단한다.

Oracle의 운영 서비스와 분리한 `/tmp` worktree에서 전체 Python 회귀
`48/48`을 통과했다. 실제 운영 원장을 읽기 전용으로 대입한 결과 거래이력
29건(매수 18·매도 11), 수익 매도 10건(확정가 0·추정가 10)이었고, 격리된
임시 공개 일봉 캐시 백필은 10종목 모두 성공해 `tracked_exits=10`,
`failed_symbols=[]`를 확인했다. systemd 달력의 07:30·17:30 KST도 정상
파싱된다.

GitHub CLI 재인증 후 PR #99를 생성했고 CI와 Site UI CI 3/3 성공 뒤
exact head `0a4deab4`를 병합했다. 기본 브랜치 merge commit은
`ef017877`이다.

2026-07-29 23:22 KST(뉴욕 10:22)는 미국 정규장 중이었다. 수동 장중 배포는
실행 전 안전심사에서 차단됐지만, 기존 5분 autodeploy가 14:24 UTC에 PR #99를
감지해 Oracle을 `ef017877`로 fast-forward하고 sentinel/buyloop/telegram/
portfolio-web를 한 차례 재시작했다. 추가 장중 재시작을 막기 위해
`autodeploy.timer`만 정지했으며 현재 inactive다.

자동배포 직후 재감사 결과는 L1, KIS mock, 원장·브로커 열린 주문 0,
UNKNOWN 0, 미회계 BUY 0, 운용원가 26,214,776.7원 ≤ 33,250,000원,
브로커·보호원장·costbook 수량 일치, heartbeat 9.2초였다.
sentinel/watchdog/buyloop/telegram/portfolio-web는 모두 active이고
systemd 재시작 누적 오류는 0이다. 장중 자동배포로 인한 주문·원장 이상은
발견되지 않았다.

제한적 L0 재감사의 blocking gate는 두 가지다.

- `STALL_EXIT_MODE=off`: 정체청산은 먼저 `shadow`로 관찰해야 한다.
- `ALLOWED_SYMBOLS=[]`: 사용자가 정확한 미국 종목 목록을 승인해야 한다.

기존 동결 6종목은 그대로이며 fallback은 0이다. 정확한 allowlist 승인 전
L1을 해제하지 않는다.

다음 미국 정규·연장장 종료 뒤 할 일:

1. autodeploy timer를 잠시 멈추고 실행 중 oneshot이 없는지 확인
2. Oracle을 기본 브랜치 최신 exact merge로 clean fast-forward
3. `post-exit-refresh.service/.timer`와 Oracle drop-in 설치
4. timer 최초 실행으로 운영 캐시에 10종목 백필
5. portfolio-web만 재시작하고 `/api/post-exit.json` 200·POST 405,
   수익매도 10·tracked 10·failed 0·시크릿/주문키 0을 확인
6. autodeploy timer 복구, 매매 서비스 active·heartbeat fresh·L1 유지 확인

사후추적 결과를 자동 청산 규칙에 연결하지 않는다. 제한적 L0는 이 배포와
분리하며, 정확한 미국 allowlist 승인 전에는 해제하지 않는다.

기존 heartbeat automation `kis`는 2026-07-30 09:10 KST 한 번 실행되도록
갱신했다. 뉴욕 20:10 이후임을 재확인한 뒤 위 런북을 수행하며, 장중이거나
불일치가 있으면 아무것도 변경하지 않고 fail-closed 보고한다.

### 23. 2026-07-29 limited mock L0 정확 목록 승인

사용자가 limited mock L0의 정확한 미국 allowlist를 다음 6종목으로
명시 승인했다.

`EQT, CEG, EXE, MARA, TBBK, CLBK`

장후 heartbeat 런북에 이 목록을 반영했다. 먼저 `STALL_EXIT_MODE=shadow`와
위 `ALLOWED_SYMBOLS`만 `/home/ubuntu/kis.env`에 원자 적용하고,
`TRADE_STAGE=mirror`, `KIS_ENV=mock`, `ALLOW_BUY=1`,
`KIS_ORDERS_ENABLED=1`, fallback 0, 동결 6종목을 유지한다. 환경 로드 후
`scripts/kis_l1_readiness.py --scope l0 --broker --json`의
`blockers=[]`를 확인해야만 명시적 operator ack로 L1→L0를 하향한다.

현재 A 12·B 4로 두 슬리브가 모두 상한이므로 정상이라면 L0 직후 신규주문은
0건이다. 첫 buyloop 사이클에서 주문 0·heartbeat·보호매도·서비스 상태를
확인한다. blocker, 목록 불일치, 새 주문, 수량/원장 이상이 있으면 env 백업을
복원하고 즉시 L1을 유지 또는 복귀한다. 이 승인은 KIS mock limited L0만
대상이며 KIS live, fallback 1, stall live, 동결해제 승인이 아니다.

### 24. 2026-07-30 사후추적 운영 배포·limited mock L0 완료

09:10 KST(뉴욕 20:10 EDT)에 정규·연장장 종료를 확인했다. 변경 전 Oracle은
clean `ef017877`, L1, mock, 열린 주문 0, UNKNOWN·미회계 BUY 0,
원장·브로커 수량 일치, heartbeat 8.7초, 모든 핵심 서비스 active였다.

Oracle을 기본 브랜치로 fast-forward하고 사후추적 unit을 설치하는 첫 시도는
`python scripts/post_exit_refresh.py`가 저장소 루트를 import path로 잡지
못해 `ModuleNotFoundError: bot`으로 실패했다. 매매 서비스와 L1에는 영향이
없었고 timer를 즉시 disable/reset했다. `python -m scripts.post_exit_refresh`
로 수정하고 회귀 assertion을 추가한 commit `43e7c243`, PR #103을 CI 2/2
후 exact-head 병합했다. merge commit은 `7bb141db`다.

수정본을 Oracle에 배포한 뒤:

- `post-exit-refresh.service` Result=success, timer active
- `/api/post-exit.json` GET 200·POST 405·`read_only=true`
- 수익매도 10건·tracked 10·갱신종목 10·실패 0
- API에서 APPKEY/APPSECRET/CANO/ODNO/order_key/pos_key/kis.env 노출 0
- sentinel/watchdog/buyloop/telegram/portfolio-web 전부 active

`/home/ubuntu/kis.env.before-l0-20260730-0016`에 mode 0600 백업을 만들고,
본 파일도 0600을 유지하며 다음 두 값만 원자 적용했다.

- `STALL_EXIT_MODE=shadow`
- `ALLOWED_SYMBOLS=EQT,CEG,EXE,MARA,TBBK,CLBK`

TRADE_STAGE=mirror, KIS_ENV=mock, ALLOW_BUY=1, KIS_ORDERS_ENABLED=1,
fallback 0, 동결 6종목을 재검증했다. sentinel·buyloop 장외 재시작 후 L0
readiness는 `blockers=[]`였다. 사용자 승인 operator ack로 limited mock
L0를 하향했다.

L0 이후 00:17·00:18 UTC buyloop 두 사이클은 신호 0·신규주문 0이었다.
로컬·브로커 열린 주문 0, UNKNOWN 0, 미회계 BUY 0, 원장 healthy,
운용원가 26,214,776.7원 ≤ 33,250,000원, 수량 일치, heartbeat 12.8초다.
현재 kill-switch는 L0이며 stall은 shadow, allowlist 6종목만 신규매수
가능하다. A 12·B 4 상한 때문에 자리가 생기기 전에는 주문하지 않는다.

금지선은 그대로다: KIS live, fallback 1, stall live, 동결 6종목 해제는
수행하지 않았다.
