# Claude 구현 보고 — KIS 스캐너 직접진입 전환 (autopaper 권한 제거)

- 상태: **적대검토 요청 — Codex/Claude 검토 통과 전 병합·Oracle 배포 금지**
- 구현지시서: 사용자 "Claude 구현지시서 — autopaper 미러 오해 정정 및 KIS
  스캐너 직접진입 전환" (2026-08-05)
- 판정 규칙: P0/P1이 하나라도 있거나 핵심 반례가 미검증이면 병합 차단

---

## 1. 기준 커밋 / 작업 브랜치 / 최종 커밋

| 항목 | 값 |
|---|---|
| 기준 커밋 | `f0a2eb0d0a4f9431942d234c77e42f8e2728669c` (Revise mirror parity V2 recommendation) |
| 작업 브랜치 | `claude/kis-direct-scanner-entry` |
| 코드 최종 커밋 | `d2b7fc4a` (Document KIS scanner-direct operation) |
| 커밋 구성 | `869de94b` Remove autopaper authority from KIS buyloop → `34a2eafd` Make limited L0 require direct-signal allowlist → `a591e4fa` Add adversarial direct-entry regression tests → `d2b7fc4a` Document KIS scanner-direct operation |

기준 커밋에는 고정 포지션 수 제한 제거가 포함되고, TWR 구현 커밋
`759b41ad`는 포함되지 않는다(지시서 §4.1 — 매수경로와 성과/TWR 분리).
이 검토 문서 자체는 코드 최종 커밋 위에 별도 커밋으로 올린다.

## 2. 변경 파일 목록과 파일별 이유

| 파일 | 이유 |
|---|---|
| `bot/kis_buyloop.py` | autopaper 런타임 의존 전부 제거(아래 §3). 모듈 문서를 "스캐너 신호 직접집행"으로 정정. NaN·inf·0 진입/손절가 input 게이트 추가(테스트가 발견한 실결함 수정). A/B 후보 선정·KIS 게이트·`execute_entry` 호출 인자·`order_meta`·슬리브 분리는 무변경. |
| `bot/l1_readiness.py` | snapshot `mirror_requires_autopaper` 필드·`mirror_parity_enforced` 게이트 제거. `limited_l0_fence`에 **비어 있지 않은 allowlist** 요구 추가(공백-only 목록은 evaluate에서 필터되어 빈 목록=차단). |
| `bot/rollout.py` | mirror 프로필 `allowlist_required: True` 복원. 주석을 "KIS 스캐너 직접진입 limited mock 프로필(key 이름 'mirror'는 legacy alias)"로 정정. `max_positions=None`·`max_new_per_day=10`·`risk_cap=0.01` 유지. |
| `tests/test_kis_buyloop.py` | paper 전용 helper/테스트 삭제, **네트워크 트랩 하네스**(urlopen 호출=즉시 실패) 전 테스트 적용, 직접진입 적대 테스트 6건 추가(§7). |
| `tests/test_l1_readiness.py` | parity fixture/기대 제거. allowlist 없음·`[]`·공백-only 각각 l0/strict 모두 blocker 검증. |
| `tests/test_kis_buy_gates.py` | mirror stage 계약 변경 반영: allowlist 없음→거부 단언 추가, 예산 테스트들은 명시적 `ALLOWED_SYMBOLS` 설정으로 전환. |
| `tests/test_sentinel.py` | ⑫ 추가 — 공개 paper feed 빈 목록/실패여도 로컬 `kis_positions` 손절선으로 보호 지속(§9.4 회귀 증명). |
| `infra/server/buyloop.service` | Description → `stock buy loop (execute fresh scanner signals on KIS with broker-truth gates)`. |
| `infra/server/README.md` | "autopaper 미러 매수" 서술을 "스캐너 신호 직접 집행"으로 정정, mirror 모드 섹션에 allowlist 필수 명시. |
| `docs/CODEX_HANDOFF.md` | §27 정정 항목 추가(오해 원인·처리). 과거 세션 기록(§20·§25 등)은 역사 기록이므로 재작성하지 않음. |

지시서 범위(§3.2 금지 목록) 밖 파일은 건드리지 않았다. `scanner/autopaper.py`,
`bot/sentinel.py`, `bot/kis_exits.py`, `bot/kis_buy.py`, `bot/settings.py`,
`bot/alpha.py`, site/TWR 파일 전부 무변경.

## 3. 삭제한 autopaper 런타임 의존 목록

`bot/kis_buyloop.py`에서 제거:

- `json`, `urllib.request`, cache-buster용 `time` import (HTTP 전용)
- `_MIRROR_REQUIRES_AUTOPAPER`, `PAPER_FEED_MAX_AGE_MIN`, `_MIRROR_FEED_ALERT_AFTER`
- `_mirror_feed_fail_streak`, `_mirror_feed_alerted`, `_note_mirror_feed()`, `_notify_safe()`
- `_parse_paper_feed()`, `_mirror_window()`, `autopaper_entries()`
- `run_once()`의 `mirrored` 조회와 A 후보 교집합, `gate="mirror"` 결과 전부

`bot/l1_readiness.py`에서 제거:

- snapshot `mirror_requires_autopaper` 필드, `mirror_parity_enforced` 게이트,
  "autopaper가 좁혀주므로 allowlist 없음도 안전" 설명

buyloop는 이제 **외부 HTTP를 한 건도 호출하지 않는다** (전 테스트에서
`urllib.request.urlopen` 트랩으로 보증).

## 4. 그대로 보존한 KIS 안전 게이트 목록

buyloop 1차 게이트: 세션(시장별 정규장) · 어닝 D-3 · 잔고 조회 실패=전면 skip ·
KR+미 3거래소 병합 보유 중복매수 금지 · 당일 매도 재진입 쿨다운 · 시세
실패/0/음수 skip · entry tolerance · 전술(full/half/pullback) 구조 · 손절폭
검증 · **신규: NaN·inf·0 input 게이트**.

`kis_buy.execute_entry` 게이트 체인(무변경): `KIS_ENV=mock` 하드블록 →
kill-switch → 부팅 대사/boot → 파수꾼 SLA/heartbeat → rollout(정규장·정수주·
allowlist·하루 10건·risk 1%) → ownership(baseline denylist) → 원장(UNKNOWN
잠금·동일종목 in-flight·60s 간격·선기록 멱등성) → 사이징(슬리브 예산·A+B 통합
운용한도·KIS 매수여력 클램프·종목당 1/3·수량 0 차단) → 전송.

pending/부분체결 예약금(`_broker_state` reservations), 한 사이클 내 주문원가
누적, `ALLOW_BUY`·`KIS_ORDERS_ENABLED` 봉인, 일일손실 게이트 전부 무변경.
고정 포지션 수 상한 제거(`max_positions=None`)도 유지.

## 5. 신호→가격관찰→주문 실제 호출 흐름

```text
signal_feed.select()                      # 신선도·기여자·fallback 판정(무변경)
  → kis_buyloop.run_once(signals)
      A = _now_signals(signals)           # group=now · fresh=True · 유효 entry/stop
      B = _shelf_cands(signals)           # group=shelf · 별도 슬리브 예산
      [1차] 세션 → 어닝 D-3 → (후보 있을 때만) _broker_state()
            잔고 실패 → 전 후보 skip(fail-closed)
      후보별: 보유중복 → 당일매도 쿨다운 → KIS 현재가 조회
              → input(NaN·inf·0·역전 stop) → tolerance → 전술 구조
  → kis_buy.execute_entry(...)            # §4 게이트 체인 전부
  → kis_orders.place_buy                  # mock 전용(live 하드블록)
  → 체결 확정은 kis_boot._resolve_acks(잔고대사) → kis_accounting.apply_buy_fill
    → kis_positions 기록 → sentinel/kis_exits가 로컬 stop으로 보호
```

autopaper는 이 흐름 어디에도 없다. 비교/시각화 소비자(advisor 중복알림 억제,
sentinel의 참고 손절선, 공개 대시보드)는 주문 권한 밖이며 무변경(§13 후속).

## 6. 적대 반례 20개 판정

| # | 반례 | 판정 | 근거(코드 위치 · 테스트) |
|---|---|---|---|
| 1 | autopaper 사이트·state feed 48시간 사망 | **HOLDS** | buyloop에 HTTP 없음(§3, grep `urllib`=0) · `test_a_fresh_signal_executes_without_autopaper`, `test_no_autopaper_network_call_in_buyloop` |
| 2 | autopaper에 AAPL 20일 보유 | **HOLDS** | `autopaper_entries` 저장소 전체 0건(§8) — 읽는 코드 자체가 없음 · 전 buyloop 테스트의 urlopen 트랩 |
| 3 | autopaper가 오늘 TSLA 매수, scanner 신호는 만료 | **HOLDS** | `signal_feed.select`가 만료 문서 전체 거부 → run_once에 후보 미전달 · `test_both_stale_or_invalid_is_fail_closed`, `test_buyloop_consumes_selector_result_without_direct_network_logic` |
| 4 | 현재가 tolerance 밖 | **HOLDS** | tolerance 게이트 유지 · `test_price_deviation_skips`, `test_a_outside_tolerance_then_inside_executes_once`(1사이클 skip) |
| 5 | 다음 사이클 진입 시 정확히 1회 주문 | **HOLDS** | 같은 테스트가 2사이클째 submit 1회 단언 · 원장 동일종목 in-flight 잠금(`test_inflight_buy_counts_toward_position_accounting`) |
| 6 | 전날 pending 체결 중 새 후보 → 총 운용한도 | **HOLDS** | `_broker_state` reservations가 in-flight/planned BUY 잔여 전부 합산(`bot/kis_buyloop.py:67`) · `test_broker_truth_open_cost_gate`, `test_combined_a_b_total_gate` |
| 7 | 같은 종목 전날 pending + 오늘 신호 | **HOLDS** | 브로커 보유 중복 금지 + 원장 동일종목 in-flight 잠금 · `test_already_held_skips`, `test_inflight_buy_counts_toward_position_accounting`, `test_x1_gate_chain_then_sent` |
| 8 | 부분체결 잔량 예약 소멸 | **HOLDS** | 잔여수량 예약 유지 · `test_partial_and_multiple_same_symbol_reservations_are_summed` |
| 9 | allowlist 빈 목록 → readiness·rollout 모두 차단 | **HOLDS** | `rollout.check_new_entry`(al=None→거부, 빈 set→전 종목 목록 밖) + `l1_readiness` limited_l0_fence(빈/공백/필드없음=blocker) · `test_mirror_stage`, `test_l0_fence_requires_declared_order_configuration`, `test_allowlist_file_fallback_and_env_precedence` |
| 10 | `MIRROR_REQUIRES_AUTOPAPER=0/1` 잔존 env | **HOLDS** | 읽는 코드 저장소 전체 0건(§8) — 어느 값이든 no-op |
| 11 | A 12·B 4 보유에도 금액 여유 시 검토 지속 | **HOLDS** | mirror `max_positions=None` · `test_a_has_no_fixed_position_count_cap`(14보유), `test_b_sleeve_has_no_fixed_position_count_gate` |
| 12 | 보유 1개여도 잔여예산 < 1주 → 주문 0 | **HOLDS** | 사이징 수량 0 차단(무변경) · `test_half_never_promotes_zero_sizing`, `test_missing_broker_budget_snapshot_blocks_send` |
| 13 | KIS 잔고 실패 때 신호만 믿는 경로 | **HOLDS** | `_broker_state()=None` → 전 후보 "잔고 조회실패 skip" · `test_holdings_unknown_skips` |
| 14 | 신호 재발행이 오래된 basis 세탁 | **HOLDS** | `basis_generated_at` 만료·미래·tz-없음 거부(무변경) · `test_both_stale_or_invalid_is_fail_closed`, `test_bad_time_duplicate_or_wrong_contract_is_rejected` |
| 15 | B shelf가 autopaper 없이 A 예산 잠식 없이 동작 | **HOLDS** | `_shelf_cands` 유지 + 슬리브별 `seed_krw` 분리 · `test_no_autopaper_network_call_in_buyloop`(B 경로 포함), `test_combined_a_b_total_gate`, `test_b_sleeve_survives_balance_before_position_reconcile` |
| 16 | ACK를 체결로 오인 | **HOLDS** | 포지션 기록은 `kis_accounting.apply_buy_fill`(`bot/kis_accounting.py:208`) — 잔고대사 확정 후에만 · `test_half_ack_is_not_fill`, sentinel ⑩(ACK 대사 중 원장 임시 손절선) |
| 17 | 매수 후 로컬 stop 기록·sentinel이 autopaper 없이 사용 | **HOLDS** | `apply_buy_fill`→`kis_positions` 기록, sentinel은 max(feed, 로컬 stop) 선택(`bot/sentinel.py:559`) · **신규 sentinel ⑫**: feed 빈 목록에서 로컬 stop으로 하드 손절 실행 |
| 18 | TWR/성과 API/지수 그래프 파일 무접촉 | **HOLDS** | §2 변경 파일 목록에 `bot/alpha.py`·site 파일 없음(diff --stat 전체 첨부 가능) · 기준 커밋이 TWR 커밋 이전 |
| 19 | 테스트가 실제 KIS HTTP 주문 0건 | **HOLDS** | 주문 전송은 전부 `mock.patch`(kis_orders/urlopen 트랩), `KIS_ENV=mock` 하드블록 병행 · 전 테스트 오프라인 실행 |
| 20 | `mirror` env 이름이 paper 미러로 오해 유발 | **HOLDS** | `rollout.py` 프로필 주석·`infra/server/README.md` 섹션·`CODEX_HANDOFF §27` 모두 "legacy alias, scanner-direct 의미" 명시 · rename은 §13 후속 cleanup |

미검증 항목 없음. BROKEN 항목 없음.

## 7. 테스트 명령·결과

```bash
for f in tests/test_*.py; do python -m "tests.$(basename $f .py)"; done
# → 49개 모듈 전부 통과 (PASS=49 FAIL=0)
python -m compileall -q bot scanner tests scripts   # 통과
git diff --check                                    # 통과
git diff f0a2eb0d..HEAD | grep -iE "appkey|appsecret|token|password"  # 0건
```

신규/변경 집중 테스트:

- `test_kis_buyloop`: `test_a_fresh_signal_executes_without_autopaper`,
  `test_no_autopaper_network_call_in_buyloop`(A·B 모두),
  `test_a_outside_tolerance_then_inside_executes_once`,
  `test_malformed_signal_is_fail_closed`,
  `test_no_order_on_nan_zero_negative_or_inverted_stop`,
  `test_a_has_no_fixed_position_count_cap`
- `test_l1_readiness`: allowlist 없음·`[]`·공백-only → l0/strict blocker
- `test_kis_buy_gates`: `test_mirror_stage` allowlist 필수 계약
- `test_sentinel` ⑫: paper feed 빈 목록 → 로컬 stop 보호

**테스트가 발견한 실결함**: NaN 손절가는 `stop <= 0`·`stop >= px` 부등식이
모두 False가 되어 기존 게이트를 전부 통과했다(`nan<=0 == False`).
`math.isfinite` input 게이트로 수정했고 `test_no_order_on_nan_zero_negative_or_inverted_stop`이 회귀 방지한다.

## 8. 정적 grep 결과 (지시서 §9.5)

```text
git grep -n "MIRROR_REQUIRES_AUTOPAPER"        → 코드 0건(과거 리뷰 문서·§27 인수인계 언급만)
git grep -n "autopaper_entries"                → 0건
git grep -n "mirror_parity_enforced"           → 0건
git grep -n "PAPER_SOURCES" -- bot             → bot/settings.py:20(정의), bot/advisor.py:74(비주문 소비자)만
git grep -n "TRADE_STAGE=mirror"               → 문서·infra README(legacy alias 명시)만 — 코드 판정은 rollout._PROFILES 키
grep -n "urllib|urlopen" bot/kis_buyloop.py    → 0건
```

`PAPER_SOURCES` 잔존은 advisor(공개 알림 중복 억제)뿐 — 지시서 §5.4 허용 범위.
`kis_buyloop`·`kis_buy`·`l1_readiness`·`rollout`의 주문권한 판정에는 없다.

## 9. 미해결 P0/P1/P2/P3

- P0: 없음
- P1: 없음
- P2: 없음
- P3 (전부 §13 후속 cleanup PR — 이번 PR 범위 밖, 주문 재개 선결조건 아님):
  1. `reason="미러진입"` 기본 표시명 — 원장 키/멱등성과 무관함을 grep으로
     확인했으나 rename은 표시 이력 연속성 때문에 별도 PR로.
  2. `TRADE_STAGE=mirror` → `scanner_direct` rename(+ 1릴리스 alias).
  3. advisor·sentinel의 비주문 paper feed 소비(참고 손절선·중복알림 억제) 대체 여부.
  4. `bot/STRATEGY.md` 등 "autopaper와 동일" 서술 정리.

## 10. 병합 승인/차단 판정

**구현자 자체판정: 승인 가능(P0/P1 0건, 반례 20/20 HOLDS).**
단, 사용자 규칙에 따라 **Codex/Claude 적대검토 통과 + 사용자 승인 전에는
병합하지 않는다.** 이 브랜치는 검토 전까지 `claude/happy-gauss-cwoq21`
(autodeploy 브랜치)에 병합 금지.

## 11. Oracle 배포 가능 여부와 별도 운영 승인 항목

**지금은 배포 불가** — 검토 통과 전. 검토 통과 후에도 지시서 §12 절차를
장외에만 수행한다(자동배포 timer 정지 → L1 유지 확인 → exact commit
fast-forward → readiness → **별도 사용자 승인**으로만 limited L0).

별도 운영 승인이 필요한 항목(코드와 무관):

1. `ALLOWED_SYMBOLS` 재설정 — 현재 allowlist 파일은 삭제 상태(전 종목 거부).
   승인 목록을 사용자가 정해야 신규매수가 가능해진다.
2. kill-switch L1 → L0 하향(operator ack) — 현재 L1 유지 중.
3. `MIRROR_REQUIRES_AUTOPAPER` env 잔존 시 제거(남아도 no-op이나 정리 권장).

금지 유지: KIS live 전환 · fallback=1 · stall live · 동결 해제 · allowlist
확대 · 하루 한도 상향.
