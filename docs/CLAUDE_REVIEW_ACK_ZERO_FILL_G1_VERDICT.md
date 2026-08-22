# Claude 부분 재검토 판정 — F4 핫루프 분리 · SELL 전용 · 연속 갭 확인

검토일: 2026-08-23 · 대상: `codex/ack-zero-fill-stale-before` @ `543c3902`
(수정 `f1461e71` · 직전 검토 `7da43f90`)
선행 판정: `docs/CLAUDE_REVIEW_ACK_ZERO_FILL_VERDICT.md` (P1 1 · P3 2)

## 판정: **병합 가능 — P0 0 · P1 0 · P2 0 · P3 3**

**P1-1 해소**를 독립 프로브로 확인했다. 남은 P3 3건은 전부 **테스트 공백**이고
운영 코드는 정상이다. 다만 그중 하나는 Codex가 "이 mutation을 잡는다"고
명시한 테스트가 실제로는 못 잡는 건이라 별도로 적는다.

---

## P1-1 해소 — F4가 파수꾼에서 완전히 빠졌다

`check()`는 이제 원장만 읽는 F3만 호출한다(코드 검증 + 실측):

```
sentinel 배선(check) 1회
  KIS 호출                0회   []
  audit_sellable_gaps(F4) 0회
check() 본문의 audit 호출  → audit_blocked_protection 하나뿐
bot/sentinel.py diff       → 0
```

직전 검토에서 측정한 **6회 조회 · 18.0초(호출당 3초 가정) · 최악 450초**가
파수꾼 heartbeat 경로에서 사라졌다. 지시서 권장안 ①(핫루프 밖 이동)을
택했고, 데드라인 스레드를 쓰지 않아 daemon·유량 경합을 새로 만들지 않았다.
선택 근거도 타당하다.

### ops 주기 함수는 fail-closed다 (실측)

| 입력 | 결과 |
|---|---|
| 1초 간격 재호출 | 억제(간격 자체 적용) |
| `_gap_interval_s()` 경과 후 | 감사 수행 |
| 양 시장 닫힘 | 감사 0회 |
| 통화가 KRW/USD 아님 | 감사 0회(다른 시장으로 추측 안 함) |
| 수량 비숫자 | 감사 0회 |

실패에도 간격을 적용해 API 폭주를 막는 것도 코드에서 확인했다.

### 프로세스 사망 노출 경로

`telegram.service`의 `Restart=always`와 `health_beacon.sh`의 기본
`BEACON_UNITS`가 이중으로 드러낸다는 Codex 설명은 유닛 파일과 일치한다.
F4가 늦어지면 텔레그램 진단 응답만 늦고 손절 경로는 기다리지 않는다.

## G3 연속 확인 — 8종 전부 정확

| 검사 | 결과 |
|---|---|
| 1회차 침묵 → 2회차 경보 → 3회차 중복 0 | (0, 1, 1) |
| 서명 변경 시 카운터 리셋 | 변경 직후 침묵, 새 서명 2회차에 경보 |
| 갭 해소 | 회복 1회 · 카운터 삭제 확인 |
| 전송 실패 | 래치 안 잠금 · 매 감사 재시도 |
| 카운터 저장 실패 | 반환 False · 경보 0 · 파일 **바이트 동일** |
| 재시작(모듈 재로드) | 카운터 영속 → 2회차에 정상 경보 |
| 닫힌 시장 감사 | 다른 시장 카운터 보존(`{'INGR': {'count': 1}}`) |
| 경보 후 서명 변경 | 거짓 회복 없음 |

## 회귀·안전 계약

Python **74/74** · Node **19/19** · compileall · `node --check` ·
`git diff --check` 재현. `scripts/kis_ack_resolve.py`·`sentinel.py`·
`kill*.py`·`ownership.py`·`kis_orders.py`·`kis.py`·`ledger.py`·`notify.py`·
`kis_exits.py` **diff 0** — F1·F2·F3와 발주·baseline·kill·동결 경로 불변.

검토자 독립 뮤테이션 8종: **5 KILLED · 3 SURVIVED**(전부 아래 P3).

---

## P3-1 — G2 가드 테스트가 실제로는 가드하지 못한다

`side == "SELL"` 제거 뮤턴트가 **또 SURVIVED**했다. Codex 요청서 7절 6번은
"신규 G2 테스트에서 KILLED되는지"를 물었는데, 실측 결과 **그 테스트는 뮤턴트
상태에서도 통과한다**(`rc=0`).

### 원인 — 픽스처가 다른 게이트에 걸려 먼저 멈춘다

`tests/test_ack_unit_mismatch.py:_row()`가 `"side": "SELL"`을 하드코딩한다.
주문은 BUY인데 행은 SELL이라 `collect_plan`의 `exact` 필터에서 제외된다:

```python
exact = [row for row in rows if ... and (not row.get("side") or not side
                                          or row.get("side") == side)]
```

검토자 실측:

```
                 현행 코드            뮤턴트 적용
행 side=SELL  →  hold  (exact=0)      hold  (exact=0)   ← 테스트가 쓰는 픽스처
행 side=BUY   →  hold  (exact=1)      operator-zero-fill ★
```

테스트가 단언하는 `hold`는 **SELL 게이트가 아니라 ODNO/side 불일치**에서
나온다. 목적한 분기를 아예 밟지 않는 공허한 단언이다.

운영 코드는 정상이다(행 side=BUY에서도 현행은 `hold`). **테스트만** 못 잡는다.

**최소 수정**: `_row()`에 `side` 파라미터를 주고 G2 테스트에서 `side="BUY"`로
넘긴다. 그러면 `exact=1`이 되어 실제로 SELL 게이트를 밟는다.

이 항목은 직전 두 라운드에 이어 **세 번째 재발**이고, 이번에는 "고쳤다"는
주장과 함께 왔다는 점만 다르다.

## P3-2 — 서명 변경 시 카운터 리셋이 미테스트

`count = old + 1 if signature == old else 1`에서 조건을 없앤 뮤턴트가 SURVIVED.
실측하면 **서명이 매번 바뀌는 갭도 2회째에 경보**한다 — G3가 막으려던
"1회성 일시 예약"이 그대로 통과한다. 현행 코드는 정상(검토자 G3-b 통과).

## P3-3 — 닫힌 시장 가드가 미테스트

`if not scope_markets: return False`를 `= {"US"}`로 바꾼 뮤턴트가 SURVIVED.
실측하면 **양 시장 닫힘에도 KIS 6회 조회를 강행**한다. 현행 코드는 정상.

---

## 부분 재검토 질문 7개 판정

1 ✅(KIS 0회·F4 0회 실측, sentinel diff 0) · 2 ✅(간격·닫힌시장·통화 불신
전부 fail-closed) · 3 ✅(`Restart=always` + beacon 기본 유닛 확인) ·
4 ✅(F3 매 사이클·원장 외 블로킹 I/O 0) · 5 ✅(재시작·전송실패·저장실패·닫힌
시장 8종 정확) · 6 ⚠️ **P3-1 — KILLED되지 않는다** · 7 ✅(관련 9파일 diff 0).

---

## 병합 조건

**병합 가능.** P3 3건은 전부 테스트 공백이라 차단 사유가 아니다.

다만 P3-1은 함께 고치기를 권한다. 픽스처 한 줄이고, 무엇보다 *가드가 있다고
믿는 상태*가 가드가 없는 상태보다 나쁘다. 나머지 둘(P3-2·P3-3)도 각각
테스트 1건이면 닫힌다.

## 병합 후 절차 (그대로 유효)

갇힌 INGR SELL은 배포만으로 풀리지 않는다. 새 CLI로
`--key 'xe:INGR:half:2026-08-11#2' --plan`을 먼저 돌려 `operator-zero-fill`이
뜨는지 확인한 뒤, 판단이 서면 `--apply --ack "<사유>"`. 운영 apply는 사용자
승인 사항이라 이번 검토에서도 실행하지 않았다.
