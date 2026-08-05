# KIS 직접진입 V9 — 수동 종목 allowlist 제거·웹 전략 정합성 재검토 요청

## 1. 검토 대상과 사용자 확정 요구

- 브랜치: `codex/kis-direct-scanner-entry-v8`
- 직전 검토 기준: `d832701d`
- 이번 핵심: scanner-direct `mirror`에서 수동 종목 allowlist를 제거한다.
- 사용자 확정 계약: KIS는 autopaper 보유내역을 복제하지 않는다. 신선한 스캐너
  A/B 진입 신호를 KIS 현재가·잔고·예산·보호 게이트로 직접 집행한다.
- 이 변경은 병합·Oracle 배포·kill 하향을 포함하지 않는다.

## 2. 코드 변경

### 2.1 mirror allowlist 요구 제거

- `bot/rollout.py`
  - `mirror.allowlist_required=False`.
  - env/파일이 없거나 빈 목록이면 심볼 제한 없이 다음 안전 게이트로 진행한다.
  - 비어 있지 않은 목록을 명시한 경우에는 긴급 축소용 optional fence로만 동작한다.
  - Stage 1.5/2/2.5의 필수 allowlist와 빈 목록 fail-closed는 유지한다.
- `bot/l1_readiness.py`
  - scanner-direct L0 fence는 `allowed_symbols=[]`일 때만 통과한다. 서버에 과거
    6종목 env가 남아 있으면 제거 전까지 NO-GO다.
  - unrestricted 상태에서는 현재 보유시장과 무관하게 KR·US 양 시장 미체결을
    조회한다. 어느 한쪽 응답을 0건으로 증명하지 못하면 `broker_open_orders=None`
    으로 fail-closed한다.

### 2.2 V8 Claude P2 테스트 공백 보완

- `tests/test_kis_buy_gates.py`
  - 실행기 직접 호출에서 B `target<=actual_order_price` 방어를 RR 포섭과 분리해 검증.
  - B 손절폭 20%·RR 2.5 입력으로 최대 15% 손절폭 방어를 분리해 검증.

### 2.3 웹 전략 설명 정합성

- 기존 웹 기능은 유지된다: A/B 정의, A/B 관찰 분리, 평균매수가·현재가·손절·목표,
  거래이력, 익절 사후추적, KIS TWR와 장기 누적 지수 비교.
- `scanner/site_app/app.js`, `app.css`
  - KIS가 가상계좌 보유내역을 복제하지 않고 신선한 후보를 직접 재검증·집행한다는
    설명을 A/B 정의 위에 추가했다.
  - 공개 화면의 autopaper 성과는 `별도 가상 시뮬레이션`이고 KIS 주문과 무관함을
    명시했다.

## 3. 반드시 반증할 질문

1. `TRADE_STAGE=mirror`에서 `ALLOWED_SYMBOLS`가 삭제·빈 문자열·공백-only이고
   파일도 없을 때 임의의 유효 A/B 신호가 rollout의 종목목록 이유로 막히지 않는가?
2. 같은 조건에서 stale·관찰·전략계약 위반·세션 밖·동결·소유권·예산·원장·
   heartbeat 조건은 여전히 독립적으로 차단되는가?
3. Stage 1.5/2/2.5는 env 없음·빈 env·빈 파일에서 계속 전 종목 차단되는가?
4. mirror에 비어 있지 않은 optional list가 남으면 readiness가 L0를 차단해 운영자가
   낡은 6종목 설정을 지우도록 강제하는가?
5. unrestricted broker audit가 KR·US를 모두 조회하는가? 국내 mock 미체결 API가
   미지원/손상 응답이면 0으로 추정하지 않고 NO-GO인가?
6. 양 시장 응답이 모두 정상이고 열린 주문·UNKNOWN·미회계 BUY 0, 원장/KIS 수량
   일치일 때만 broker/readiness 주문 게이트가 통과하는가?
7. allowlist 제거가 하루 10건·risk 1%·종목당 1/3·A/B 슬리브·통합 운용한도·
   KIS 매수여력·mock hard-block·L1·정규장 게이트를 약화하지 않았는가?
8. 실행기 B target 관계와 15% 손절폭 이중방어 테스트가 해당 게이트를 각각 제거한
   mutation을 실제로 죽이는가?
9. 웹 공개 화면이 가상 성과를 KIS 성과로 오인시키거나 개인 계좌정보를 노출하지
   않는가? 신규 설명 문자열은 XSS/CSP/모바일 레이아웃을 해치지 않는가?
10. buyloop에 autopaper 런타임 의존이나 신규 외부 HTTP가 다시 생기지 않았는가?

## 4. 로컬 검증

- `python -m tests.run_all` → `ALL PASS: Python test modules 49`
- `python -m tests.test_kis_buy_gates` → 통과
- `python -m tests.test_l1_readiness` → 통과
- `python -m tests.test_kis_buyloop` → 통과, autopaper HTTP 0
- `python -m tests.test_site_app` → 통과
- `node --check scanner/site_app/app.js` → 통과
- `python -m compileall -q bot scanner tests` → 통과
- `git diff --check` → 통과

테스트는 주문 primitive를 spy/mock했으며 실제 KIS 주문 HTTP는 0건이다.

## 5. 운영 경계와 예상 차단

- Oracle의 과거 `ALLOWED_SYMBOLS=EQT,CEG,EXE,MARA,TBBK,CLBK`는 원자적 env 백업
  후 줄 자체를 삭제해야 한다.
- 삭제 후 readiness는 한국·미국 미체결을 모두 증명한다. KIS mock의 국내 미체결
  조회가 실제로 미지원이면 이번 변경은 이를 우회하지 않는다. 그 경우 L1 유지가
  정상 결과이며, 브로커-진실 대체 대사를 별도 설계·검증해야 한다.
- `KIS_ENV=mock`, fallback 0, 기존 동결, KIS live/stall live 금지는 유지한다.
- `STALL_EXIT_MODE=shadow`는 신규매수 알고리즘의 필수 근거가 아니라 독립 청산
  관찰 모드다. 현재 운영 경계를 바꾸지 않기 위해 그대로 둔다.

## 6. 판정 요청

P0~P3로 판정한다. P0/P1이 하나라도 있으면 병합 차단. 특히 “목록 제거 = 안전
게이트 제거”로 혼동하지 말고, 신호 신선도부터 KIS 주문 primitive까지의 전 경로와
양 시장 broker audit를 독립 재현해 달라.
