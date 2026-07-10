# 파일 04 — 인프라 · 진입 게이트 · 안전장치

> 읽는 법: `##` 하나씩. 이 파일은 "주문을 만드는 것"이 아니라 **"주문을 안전하게
> 둘러싸는 것"**들이다. 상당수는 Stage 2 Go의 필수조건.

---

## I1 — 진입 게이트: 종목 상태 · 유의사항 · 세션 · 기업행위

- **무엇**: 신규 진입 직전 `StockInfo.status`·`warnings`·`market-calendar`로 걸러내는 게이트.
- **왜**: 상장폐지·정리매매·VI·거래정지·휴장·조기폐장 종목에 진입하면 물리거나 주문거부.
  기업행위(액면분할·티커변경)로 심볼·수량이 어긋나면 오주문.
- **어떻게**:
  - 진입 조건에 **`status==ACTIVE ∧ warnings 없음`** 추가(LIQUIDATION_TRADING·OVERHEATED·
    INVESTMENT_WARNING/RISK·VI·STOCK_WARRANTS 있으면 진입 금지, 보유면 경보).
  - **세션**: `market-calendar/US`로 **regularMarket에서만 신규 진입**(pre/day/after 금지).
    휴장·조기폐장 정확 반영(현 하드코딩 `market_open` 대체).
  - **기업행위**: 심볼 매핑 불일치·수량 불일치 감지 시 **신규 주문 금지 + 수동 리뷰**(자동
    처리 안 함 — 감지·정지가 원칙).
- **주의**: warnings/status는 STOCK 버킷 — 후보 종목만 조회(캐시). 이 게이트는 진입만
  막고 **보유 청산은 막지 않는다**(손절은 항상 가능해야).
- **테스트**: DELISTED·경고종목·비정규세션 각각 진입 차단, 정상은 통과.
- **의존/Stage**: L1, `stocks`·`warnings`·`market-calendar` / Stage 2.

## I2 — 상시 서버 ($0 VM) + 하트비트 dead-man (B5)

- **무엇**: 파수꾼·루프A/B를 **고정 IP 상시 호스트**에서 auto-restart로 운용 + 그 생존을
  CF 워커가 외부에서 감시.
- **왜**: 파수꾼이 실제로 "상시"가 되려면 배치(GitHub)가 아닌 상시 프로세스가 필요.
  그 프로세스가 죽으면 손절이 멈추므로, GitHub 밖(CF)에서 하트비트 dead-man 감시.
- **어떻게**: Oracle Always-Free/라즈베리파이 등 무료·자가 호스트에 systemd(또는
  docker) 유닛으로 `python -m bot.sentinel`(+ 루프). 원장·상태를 **디스크에 durable 저장 +
  주기 백업**(state 브랜치 유사). 파수꾼이 매 폴링 `feed/sentinel_heartbeat.json` 갱신 →
  CF 워커가 나이 초과 시 **P0(ntfy+텔레그램) "SENTINEL DOWN — 손절 무방비"**.
- **주의**: "서버 없이"의 의미 = 유료 서버 안 사도 됨(무료 티어/자가 HW). 고정 IP가
  IP allowlist(VA)와 연결. 재시작 시 반드시 O4 부팅 대사 후 매매.
- **테스트**: 프로세스 kill→재시작→부팅대사, 하트비트 정지→CF P0.
- **의존/Stage**: O4, CF 워커(기존), sentinel / Stage 2.

## I3 — 단일 token_manager + 그룹별 rate limiter + 시계

- **무엇**: 여러 루프가 **하나의 토큰**을 공유하고, 그룹별(ORDER/ORDER_INFO/ASSET/
  MARKET_DATA/...) **중앙 rate limiter**를 통과하게. 서버 시계 동기화.
- **왜**: 토스는 client당 토큰 1개(재발급 시 이전 무효화) → 루프별 발급은 **토큰 폭풍**.
  그룹은 독립 버킷이나 수치 미확정 → 버스트 방지 리미터 필요. quote_ts 나이 판정은
  서버 시계가 정확해야.
- **어떻게**: 프로세스 단일 `TokenManager`(refresh lock — 한 주체만 갱신, 나머지는
  `get_token()`). 기존 `bot/toss.py` 모듈캐시를 이 매니저로 승격(단일 프로세스 서버라
  자연스러움). 그룹별 `RateLimiter`(토큰버킷) — 모든 호출이 통과, 429 시 `Retry-After`
  준수. NTP/chrony, `now()`는 UTC monotonic, 오차>2초면 주문 금지.
- **주의**: dev/stage/live가 같은 앱키 공유 금지(토큰 무효화 충돌). 리미터 수치는 V3 실측 후 조정.
- **테스트**: 동시 refresh 1회만, 그룹 리미터 초과 시 지연, 시계 오차→주문차단.
- **의존/Stage**: 기존 `toss._token` / Stage 2(서버).

## I4 — 환경 분리 플래그 + 깃 주문키 제거

- **무엇**: 실행 환경·권한을 플래그로 명시하고, **주문 가능 키를 GitHub에서 제거**.
- **왜**: 실수로 Stage 0/1 프로세스가 주문하거나, 유출된 깃 시크릿으로 주문나는 것 차단.
- **어떻게**: `TOSS_ENV=read|paper|live` · `LIVE_TRADING_ENABLED` · `ALLOW_BUY` ·
  `ALLOW_SELL` · `LIVE_ACCOUNT_SEQ` · `MAX_LIVE_RISK_PCT`. 주문 함수는 이 플래그 없으면
  거부. **주문 가능 토스 키는 상시 서버에만** — GitHub Actions 시크릿엔 (스코프 가능하면)
  읽기전용 키만, 불가하면 시세도 서버로 옮기고 깃에서 토스 키 제거([VA] 확인).
- **주의**: 현재 Stage 0은 읽기전용이라 깃에 키 무방 — **Stage 2 전환 시점에 반드시 분리**.
  플래그는 fail-safe(미설정=주문 불가)여야.
- **테스트**: 플래그 조합별 주문 허용/거부, 키 없는 환경에서 주문 시도→거부.
- **의존/Stage**: O2 / Stage 2 직전.

## I5 — 장전 preflight 캐너리

- **무엇**: 매 장 시작 N분 전, 주문 없이 **인증·권한·계좌·시세 경로가 살아있는지** 점검.
- **왜**: "토큰이 주문에서만 401"·"IP allowlist 밖"·"계좌 권한 없음"을 **장중이 아니라
  장전에** 잡는다(auth 실패=P0, warning 아님). 5일 연속 green이 Stage 2 게이트.
- **어떻게**: `bot/preflight.py` — token 발급/갱신, `accounts`·`holdings`·`sellable`
  샘플 조회, (실주문 스테이지면) 주문가능정보 조회. 하나라도 실패=**P0**. green 스트릭 기록.
- **주의**: 실주문은 하지 않음(읽기 프로브만). IP allowlist 있으면 여기서 403이 조기 경보.
- **테스트**: 각 프로브 실패→P0, 5일 스트릭 카운트.
- **의존/Stage**: L2·L3, `notify` / Stage 1.5→2.

## I6 — kill-switch 레벨 2~4

- **무엇**: 현재 레벨1(`KILL_NEW_ENTRIES`=신규진입 중지)에 이어 **단계적 정지**.
- **왜**: 사고 규모별 대응. 신규만 멈출 상황 ↔ 전 주문 멈출 상황 ↔ 손절만 남길 상황이 다르다.
- **어떻게**: `KILL_LEVEL` = 0(정상)/1(신규진입 중지)/2(전 신규주문 중지=진입+지정가)/
  3(파수꾼 손절만 유지, 그 외 전부 중지)/4(전면 중지→수동 청산). 각 레벨을 루프A/B/C가
  읽어 해당 동작만 수행. state 브랜치 파일 or env로 토글(코드 수정 없이). 레벨 상승 시 P0.
- **주의**: 레벨 3에서도 **손절(파수꾼)은 살아야** — 정지가 보호를 죽이면 안 됨. 레벨 4는
  수동 개입 전제.
- **테스트**: 각 레벨에서 허용/차단 동작(특히 L3에서 손절만 통과).
- **의존/Stage**: 기존 kill-switch L1, X1·X4 / Stage 2.

## I7 — 단계적 롤아웃 가드 (코드로 강제)

- **무엇**: Stage 2 첫 주 제한(1종목·whole-share·0.1%·하루1건·한시장)을 **문서가 아니라
  코드 가드**로 강제.
- **왜**: "첫 주는 조심하자"를 사람 의지에 맡기면 실수. 코드가 물리적으로 막아야.
- **어떻게**: `STAGE`/`MAX_LIVE_RISK_PCT`/`MAX_CONCURRENT_POS`/`WHOLE_SHARE_ONLY`/
  `DAILY_NEW_MAX`/`ALLOWED_MARKETS` 설정을 루프A/사이징/주문이 강제. Stage 상승은 명시적
  설정 변경 + 통과기준 확인.
- **주의**: 소수점 금지(whole-share), confirmHighValueOrder=false와 결합. 상한 초과 주문은
  전송 전 차단 + 로그.
- **테스트**: 각 상한 초과 시도→차단, Stage 승급 시 상한 완화.
- **의존/Stage**: X1·X2 / Stage 2.
