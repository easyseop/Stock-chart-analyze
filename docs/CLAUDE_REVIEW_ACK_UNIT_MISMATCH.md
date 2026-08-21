# Claude 적대 검토 요청 — ACK 단위 불일치·동결 직접증거·거절 사유

작성: 2026-08-22 · 구현: Codex · 기준: `b7d9e3c5`
검토 브랜치: `codex/ack-unit-mismatch`

## 1. 판정 요청

`docs/CODEX_SPEC_ACK_UNIT_MISMATCH.md`의 C1~C3 구현을 적대 재검토해 주세요.
P0/P1이 하나라도 있으면 병합 차단입니다. 기본 브랜치 병합·Oracle 배포·kill/env
변경은 이 검토와 별개이며 사용자 승인 전 금지입니다.

## 2. C1 — 한 잔고 응답, 두 수량 단위

- `bot.kis.holding_quantities()`가 같은 완전 페이지 집합에서
  `total(hldg_qty/ovrs_cblc_qty)`과 `sellable(ord_psbl_qty)`을 독립 파싱합니다.
- 파수꾼 최초 SELL과 US chase는 이 접근자를 한 번만 호출합니다.
- 전송 상한은 기존 그대로 `min(요청수량, sellable)`입니다.
- 대사 기준 `hldg_before`만 total로 바꿨습니다. total만 불신이면 `None`으로
  기록하되 sellable이 유효하면 보호 SELL은 계속 냅니다. sellable 불신은 기존처럼
  주문 전 차단합니다.

INGR 실측 표 회귀:

| 상황 | total before | total now | 결과 |
|---|---:|---:|---|
| 미체결 | 11 | 11 | delta=0, 보류·동결 0 |
| 정상 5주 체결 | 11 | 6 | delta=5, filled+회계 |
| 매도가능 예약 5주 존재 | total=11, sellable=6 | - | 전송≤6, before=11 |

## 3. C2 — 동결은 추론만 막고 exact ODNO 직접 체결은 허용

- `_broker_inflight_counts`와 `_direct_evidence_allowed`를 공통화했습니다.
- `resolve_acks_from_rows`와 `reconcile_unknowns`는 다음을 모두 만족할 때만 동결과
  무관하게 직접 체결을 반영합니다.
  1. 이미 원장에 결속된 ODNO exact 단일 행
  2. 같은 심볼 broker non-terminal 주문 정확히 1건
  3. ownership baseline armed
  4. 사용자 기보유 baseline 아님(기존 3중 증명 legacy bot SELL 예외만 유지)
- 잔고 delta와 부재+잔고불변은 동결 시 계속 보류합니다.
- 자동 direct 확정은 `kis_accounting.sync_fill`까지 실행하지만 동결을 스스로 풀지
  않습니다.

신규 `scripts/kis_ack_resolve.py`:

```bash
python scripts/kis_ack_resolve.py --key '<ledger-key>' --plan
python scripts/kis_ack_resolve.py --key '<ledger-key>' --apply --ack '<operator reason>'
```

- plan/apply 모두 KIS 미체결·체결·총보유를 fresh 재조회합니다.
- 미국은 NASD/NYSE/AMEX 전부, 한국은 mock 강한 미체결 폴백까지 사용합니다.
- 한 조회라도 불신이면 원장·동결 파일 쓰기 0건입니다.
- apply는 빈 ack를 거부하고, 상태전이 전 intent와 완료 후 result를 append-only 감사
  이벤트로 남깁니다. terminal일 때만 동결을 원자 해제·재검증합니다.
- 자동 direct 처리 뒤 terminal+frozen인 주문도 fresh exact 체결을 다시 증명하면
  운영자 ack로 동결만 해제할 수 있습니다.
- 출력에는 ODNO·계좌·가격·수량 메타를 넣지 않습니다.

## 4. C3 — 응답/마지막 관측의 안전한 보존

- 주문·취소 HTTP 응답의 `msg_cd/msg1`을 원장 `reconcile_meta`에 보존합니다.
- exact ODNO nccs/ccnl 행의 마지막 status/code/message/source는 값이 바뀔 때만
  append합니다. 15초 반복행이 같으면 원장 증가 0건입니다.
- 종결 사유 우선순위는 `명시 행 사유 → 마지막 관측 → 접수 응답 → 사유 미상`입니다.
- reconcile meta fold는 필드 merge라 접수 응답과 마지막 관측이 서로 지워지지
  않습니다.
- 제어문자·Bearer·credential key/value·실제 env secret·8자리 이상 식별자를
  저장/알림 전에 제거합니다. `msg_cd`만 숫자 코드 보존을 위해 별도 모드입니다.
- 공개 ntfy의 기존 category-only 계약은 변경하지 않았습니다.
- 사용자가 외부 payload 범위를 명시 승인해 `/진단`에는 열린 주문 중 최대 3건의
  `종목+BUY/SELL+정화된 브로커 상태/메시지`만 표시합니다. 계좌·ODNO·원장키·
  가격·수량은 출력하지 않고, 저장 시와 전송 직전에 이중 정화한 뒤 HTML escape합니다.
- 전용 테스트는 4번째 주문 미표시, 계좌/긴 식별자/토큰/내부키 비노출, HTML escape를
  함께 단언합니다.

## 5. 검증 증거

- 집중: `python -m tests.test_ack_unit_mismatch` — PASS
- 텔레그램: `python -m tests.test_kis_telegram` — PASS
- 관련: `test_kis`, `test_kis_orders`, `test_kis_reconcile`,
  `test_kis_ack_resolve`, `test_kis_boot`, `test_kis_accounting`,
  `test_kis_pending`, `test_sentinel`, `test_sentinel_chase`,
  `test_sell_reject_reconcile` — PASS
- 전체: bundled Python으로 `python -m tests.run_all` — **73/73 PASS**
- Node: `node --test tests/site_math.test.js` — **19/19 PASS**
- `compileall bot scanner scripts tests`, `node --check scanner/site_app/app.js`,
  `git diff --check` — PASS

독립 mutation(각 적용→테스트 exit 1 확인→원복):

| ID | 제거/오염한 방어 | 잡은 테스트 |
|---|---|---|
| M1 | hldg_before를 sellable로 회귀 | C1 before=11 assertion |
| M2 | 동결 exact direct 예외 제거 | C2 frozen direct assertion |
| M3 | 사용자 baseline direct 허용 | C2 baseline assertion |
| M4 | 동일심볼 open_count gate 제거 | C2 multi-order assertion |
| M5 | safe_qty min→max | C1 clamp assertion |
| M6 | 주문응답 원장 기록 제거 | `test_kis_orders` meta assertion |
| M7 | 긴 식별자 redaction 제거 | C3 secret assertion |
| M8 | operator ack 필수 제거 | CLI empty-ack assertion |

## 6. 집중 반증 질문

1. 한 페이지라도 불완전할 때 total/sellable 양쪽이 함께 불신되는가?
2. total만 손상된 보호 SELL이 hldg_before=None으로 나가고 추론 대사는 모두
   멈추는가? 이를 CLI가 근거 없이 우회할 수 없는가?
3. 동결 direct 확정이 원장만 닫고 회계를 빼먹는 크래시/회귀 경로가 있는가?
4. 동일 심볼 BUY+SELL 또는 두 SELL에서 exact ODNO 한 건이 자동확정되는가?
5. baseline 사용자 보유가 direct/CLI/terminal-unfreeze 어느 경로로도 풀리는가?
6. CLI plan과 apply 사이 상태가 바뀌거나 KIS 한 거래소만 실패하면 쓰기가 있는가?
7. order response/last observation에 token/account/order number가 섞이면 원장·알림으로
   노출되는가?
8. reconcile_meta의 merge 때문에 오래된 status가 최신 사유로 과장되는가?
9. auto direct 뒤 frozen을 코드가 스스로 해제하는가(하면 결함)?
10. KR 시장가 손절과 US chase 모두 total/sellable 단위 분리를 유지하는가?
