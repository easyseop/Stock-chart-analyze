# 구현지시서 — 수동 매수 포지션 귀속 정정 + 신규 게이트·섀도 적대 검토

> ## ⚠️ 2026-08-19 정정 — T2 이관 **중단**. H2·H3 모두 기각, H4 확정
>
> 체결 일시가 나왔다: **2026-08-18 22:30:18 KST, 지정가 65.03 × 74주.**
> 상태 브랜치 이력에서 그 시각 유효 피드(22:13:52 발행,
> `feed/signals.latest.json` @ `4248915…`)를 꺼내 대조한 결과:
>
> ```
> CVNA: group='now' · fresh=True · mode='pullback' · pb_price: 65.0332
> ```
>
> 발주가 65.03 = pb_price 65.0332의 KIS 소수 2자리 절사. **현 매수루프의
> pullback 지정가 주문이다.** 개장 직후 눌림에 체결됐다. 수동 매수도(H3),
> legacy 유실도(H2) 아니다 — **8/18 밤 봇이 샀고, 체결 확정 → 회계
> (kis_positions·costbook) 반영 체인이 유실됐다(H4).**
>
> 어제 "원장 grep 0건"은 오판이었다 — 검색 경로가 틀렸다. 실제 원장은
> `bot/order_ledger.jsonl`·`bot/kis_positions.jsonl`·`bot/costbook.jsonl`이다
> (검색은 `data/*.jsonl`을 봤다). 흔적 유무는 재확인 대상.
>
> **T2 재정의** (아래 원문 T2의 baseline 이관은 CVNA에 적용하지 않는다):
>
> - **T2a. 원인 확정(원장 실증 완료) — zero-fill 오판 종결 수정**
>
>   원장 추적 결과(2026-08-19), 재시작 경합이 아니라 **대사 판정 버그**다:
>
>   ```
>   22:30:37 submit  pullback limit 65.0332 ×74 (reservation 6,641,190원)
>   22:30:38 bind    ODNO=0000040445 · ack · filled=0
>   22:32:16 reconcile state=rejected · filled=0 · open=false
>            reason=broker-closed-zero-fill · source=ccnl
>            msg_cd=20310000 "모의투자 조회가 완료되었습니다"
>   (실제로는 체결됨 — 앱 체결내역 74/74, 잔고에 74주 실존)
>   ```
>
>   `bot/kis_reconcile.py:277` `elif not opened:` 분기가 **ack 98초 뒤**
>   ccnl 응답의 "filled=0·닫힘"을 그대로 믿고 zero-fill 종결했다. 모의
>   ccnl의 체결 반영 지연을 '0체결 확정'으로 오독한 것 — "조회 성공 +
>   행에 체결 없음"은 부재 증명이 아니라 **아직 안 올라온 것**일 수 있다.
>   종결이 reservation 6.64M을 해제해 그 돈으로 추가 매수가 이뤄졌고
>   (SEED 초과 투입), 포지션은 무보호로 남았다. 실계좌였다면 매수 대금이
>   증발한 것처럼 보이는 P0급 실회계다.
>
>   수정 요구:
>   1. **잔고 교차 검증** — BUY를 zero-fill 종결하기 전에 잔고에서 해당
>      심볼 보유 증가를 확인한다. 증가가 보이면 종결 금지, UNKNOWN 유지 +
>      P0 경보(수량 귀속이 모호하면 자동 귀속하지 말 것 — 기존 원칙 유지).
>   2. **유예 창** — ack 후 최소 N분(제안 10분) 또는 연속 2회 독립 확인
>      전에는 zero-fill 종결 금지. 98초 만의 확정이 이번 사고다.
>   3. 테스트: ①ccnl 지연 주입(체결됐는데 ccnl filled=0) → 종결 0·UNKNOWN
>      유지 ②잔고 증가 감지 → 경보 1회 ③진짜 zero-fill(잔고 불변·유예
>      경과) → 기존대로 종결 ④매도 방향 회귀(기존 R1~R5 무손상).
>   4. 뮤테이션 검증 전 커밋. 증거는 실패 테스트명 + 종료코드 원문.
> - **T2b. CVNA 원가 costbook 소급 기입** — 봇 돈으로 확정됐으므로 원래
>   §6-6이 되살아난다. 6,640,863원(74×$65.03×1380). 멱등키·이벤트 순서·
>   기존 대사와의 충돌을 검토해 안전한 기입 경로를 설계·구현하라.
> - **T2c. adopt CLI는 그대로 구현** — CVNA에 쓰지 않을 뿐, 미래의 진짜
>   수동 매수 대응 도구로 원문 R1·R3·테스트 요구 유지.
> - SEED A 37M은 **유지**(봇이 실제로 35.09M을 투입한 게 사실로 확정 —
>   초과 투입 자체가 H4 버그의 피해다). 원복 여부는 T2b 후 사용자 결정.


작성: Claude, 2026-08-19 · 역할: **Codex 구현/검토 → Claude 적대 재검토 → 사용자 승인 후 병합·배포**
전제: 이 문서는 매도 보호(파수꾼)·kill 상향 규칙을 바꾸지 않는다.

---

## T1. 적대 검토 — 총위험 게이트·이력 게이트·섀도 실험 (우선)

대상: 커밋 `6708f7bc`(구현) + `b75bce3c`(설계서).
검토 자료: `docs/IMPL_DESIGN_RISKCAP_SHADOW.md` — §6에 공격 포인트 6개를
지정해 뒀다(§6-6은 아래 T2로 대체·재정의됨). 판정은 P0/P1/P2/P3로,
증거는 실패 테스트명 + 종료코드 원문. 뮤테이션 재검증 환영(커밋 먼저).

## T2. CVNA 귀속 정정 — "봇 포지션"이 아니라 "사용자 수동 매수"였다

### ⚠️ 전제 확인(사용자 숙제 — 이관 실행 전 필수)

근거가 아직 정황이다: 현 원장·거래이력 grep 0건은 **"현 원장 체계(8/4 legacy
이관 이후)에서 봇이 산 적 없다"**까지만 증명한다. legacy 시절(≈7월 중순~8/4)
봇 매수가 이관+페이지네이션 버그로 유실됐을 가능성(H2)이 남아 있다.
판별: **KIS 앱 기간별 체결내역에서 CVNA 매수 일시 확인.**
  · legacy 창(≈7/15~8/4) 안 + 장중 시각 → H2 재판정(이관 중단, costbook
    소급 기입 안으로 회귀 — Claude에 보고)
  · 그 외(다른 날짜·장외·주말) → H3 확정, 아래 그대로 진행.

### 사실관계 (2026-08-19 잠정 — 위 전제 확인 대기)

- CVNA 74주 @ $65.03은 **사용자 수동 매수로 추정**(H3 — 전제 확인 대기). 봇 주문
  아님 — 그래서 원장·costbook·거래이력 어디에도 없는 게 **정상**이었다.
- baseline(기보유 denylist)은 arming 시점 캡처라 **그 이후의 수동 매수**를
  모른다. 이 간극이 이번 사고의 진짜 뿌리다.
- 임시조치 이력: ① stop=60.48로 kis_positions에 **봇 포지션인 것처럼 복원**
  (무보호 해소 목적) ② SEED A 30M→37M 상향(불변식 해소). 둘 다 H3 확인
  전의 조치라 **귀속이 틀렸다** — 사용자 돈을 봇 장부에 편입한 상태다.

### 요구사항

R1. **CVNA를 baseline으로 이관**한다:
  - `ownership` baseline에 CVNA 추가(파일 스키마 유지·원자적 쓰기·0600).
    단발 스크립트가 아니라 **재사용 가능한 CLI**로:
    `python -m bot.kis_arm --adopt CVNA "사유"` 또는 동급. 수동 매수는
    또 생긴다 — 도구가 있어야 다음번엔 1분짜리 일이 된다.
  - kis_positions에서 CVNA를 close 이벤트로 제거(복원 open의 역연산).
    멱등: 두 번 실행해도 안전.
  - 동결(frozen) 해제 여부는 **건드리지 않는다**(별도 운영 판단).
R2. 이관 후 **SEED A를 30M으로 원복**해도 불변식이 성립함을 검증하는
  절차를 문서화한다(브로커-진실 held_cost가 baseline 제외로 계산되는지
  코드로 확인 — `_broker_state`/`aggregate`의 baseline 필터 경로).
R3. 재발 대응 계약: 브로커에 있고 원장·baseline 어디에도 없는 종목
  (= 고아)은 **자동 편입하지 않는다**. 분류(봇 포지션 복원 vs baseline
  이관)는 사람의 결정이다 — 감지·경보는 Claude의 포지션 대사 자동화가
  맡는다(별도 작업, 이 지시서 범위 밖). 이 지시서에서는 R1의 CLI가
  그 결정의 실행 도구가 되는 것까지만.
R4. §6-6(costbook 소급 기입)은 **철회**한다 — H3이므로 봇 회계에 CVNA
  원가를 넣는 것 자체가 오귀속이다. 대신 R1 이관 시 costbook에 아무
  흔적도 남지 않음을 테스트로 증명하라(매도 시 현금 부풀림 경로 소멸).

### 테스트 (최소)

1. adopt CLI: baseline에 추가 + kis_positions close + 멱등(2회 실행 동일).
2. adopt 후 buy_denied("CVNA") = True(기보유 denylist).
3. adopt 후 파수꾼 held 집계에서 CVNA 제외(감시·매도 안 함).
4. adopt 후 costbook·open_risk에 CVNA 흔적 0.
5. baseline 파일 손상·쓰기 실패 → 기존 fail-closed 유지(전 종목 매수 거부).
6. 기존 스위트 무손상(test_ownership_baseline·test_kis_buyloop·
   test_risk_budget·test_sentinel).

### 주의 (적대 검토 포인트)

- baseline은 "매수 전면 거부"의 근거 파일이다 — 쓰기 실패가 조용히 지나가면
  전 종목 거부(과차단) 또는 denylist 누락(과허용) 둘 다 가능. 원자적 쓰기 +
  실패 시 명시적 에러.
- adopt가 kis_positions close를 먼저 하고 baseline 추가가 실패하면 CVNA가
  다시 무보호 고아가 된다 — **순서: baseline 추가 성공 확인 → close**.
- 뮤테이션 검증 전 커밋. 증거는 실패 테스트명 + 종료코드 원문.

## 완료 기준

T1 판정문 + T2 구현·테스트·뮤테이션 증거 → Claude 적대 재검토(P0/P1=0) →
사용자 승인 → 병합. 배포 후: 사용자가 adopt 실행 → SEED 원복 → 붙박이
확인(`python3 scripts/kis_orphan_audit.py` = 이상 없음).

---

## ⚠️ 2026-08-20 재정의 — T2b apply 전 파수꾼이 절반 익절 (plan 거부 실측)

00:5x KST plan 실행이 `보호 포지션 수량 불일치`로 **정상 거부**됐다(fail-closed
작동). 원인: apply 이전인 8/20 00:13 KST 파수꾼이 A 전략대로 CVNA를 절반
익절했다. 원장 실측:

```
8/19 00:27  open 74주 @65.03 · stop 60.48          (수동 복원)
8/20 00:13  sell_fill 37주 @69.51
            event_id fill:xe:CVNA:half:#1:SELL:37 · pos_key legacy:A:CVNA:?
8/20 00:14  half_done · raise stop→65.03(본전 래칫)
현재: KIS 잔고 37 = kis_positions 37 · stop 65.03(하방 0)
```

### T2b 재정의 — 복구 대상이 매수 1건에서 매수+매도 2건으로

1. **BUY 74 @ $65.03 = 6,640,863원** 원가 기입(기존 그대로), 그리고
2. **SELL 37 @ $69.51의 회계 실태 조사·정정** — costbook에 매수 lot이 없는
   상태에서 이 매도가 어떻게 처리됐는지 원장·costbook에서 확인하라.
   (a) proceeds가 lot 없이 계상됐다면 → 현금 부풀림이 실존, BUY 기입과 함께
   SELL을 그 lot에 귀속시키는 순서 보장 필요.
   (b) lot 부재로 fail-closed됐다면 → BUY 기입 후 SELL 재귀속 경로 필요.
3. 복구 후 기대 상태: lot 74 매수 → 37 매도 귀속 → **잔여 37주 ·
   원가 3,320,432원**(37×65.03×1380) · 실현손익 +228,749원(37×4.48×1380).
4. plan 검증은 "현재 잔여 수량"과 "복구 시나리오의 최종 수량"을 구분할 것 —
   이번 거부가 보여줬듯 살아있는 포지션은 apply 사이에도 움직인다.
   시나리오(매수→부분매도)를 통째로 검증·기입하는 형태를 권한다.
5. 안전 계약(주문 0·plan/apply·SHA·서비스 정지·백업·멱등)은 기존 그대로.
6. 서두를 필요 없음: 잔여 37주는 본전 래칫이라 하방 0. 성과 미확정이
   길어지는 것뿐이다. 정확성이 속도보다 우선.

---

## ⚠️ 2026-08-20 02:0x 운영 결함 — quiesce 게이트가 실서버에서 도달 불가 (P1)

v2 apply 실행 시도에서 발견. plan 3회 거부는 전부 정상 방어였으나(ODNO 미국
거래일 20260819로 해결·SHA 만료), 마지막 거부는 **도구 결함**이다:

```
why: 주문 서비스 미정지: sentinel.service=enabled (systemctl mask --runtime 필요)
실측: sudo systemctl mask --runtime sentinel buyloop  → 조용히 성공
      systemctl is-enabled sentinel buyloop           → enabled / enabled
```

원인: 이 서버의 유닛은 `/etc/systemd/system/*.service` **실파일**이다(README
설치 절차). `mask --runtime`은 `/run/systemd/system/`에 심볼릭을 만들지만
systemd 우선순위가 `/etc > /run`이라 **마스크가 그림자에 가려 무효**가 된다.
`_services_quiesced()`가 요구하는 `masked|masked-runtime`은 이 배치에서
`systemctl mask --runtime`으로 도달할 수 없다. 테스트는 systemctl을 모킹해
이 배치 특성을 보지 못했다.

### 수정 요구

1. 정지 검증을 실배치에서 달성 가능한 계약으로: 예 —
   `is-active=inactive` + (`masked*` **또는** `disabled`+`watchdog inactive`
   증명) 또는 `/run` 마스크의 실효성을 systemd 우선순위까지 확인하는 검사.
   어느 쪽이든 "정지됐고, 다른 무엇도 되살릴 수 없다"는 원래 의도를 지킬 것
   (watchdog이 sentinel을 재기동하는 유일한 주체임을 활용해도 좋다).
2. 테스트에 "유닛이 /etc 실파일 + /run 마스크 무효" 배치 재현 1건.
3. 문서 런북의 mask 명령을 수정된 계약에 맞게 갱신.

재검토는 Claude. apply 재시도는 수정 배포 후 장외에.


### → 2026-08-20 해결(Claude 구현, Codex 역방향 검토 요청)

사용자 지시로 Claude가 직접 수정했다. 정지 증명을 유닛별 두 갈래로:
① 기존 mask 증명(masked/masked-runtime — 가능한 배치에선 그대로) **또는**
② 부활 주체 전원 정지 증명 — `GUARDIAN_UNITS`(watchdog·autodeploy.service·
autodeploy.timer)가 전부 inactive(activating/reloading도 가동으로 취급,
조회 실패는 fail-closed). is-active=inactive·pgrep·heartbeat 요구는 불변.

이로써 기존 런북의 "stop watchdog autodeploy.timer telegram sentinel buyloop"
만으로 quiesce가 성립한다(mask 불필요 — 하면 여전히 인정됨).

검증: 신규 계약 테스트 8케이스 · 뮤테이션 4/4 KILLED(두갈래 제거·activating
정지 취급·조회실패 fail-open·autodeploy 감시 제거) · 회귀 5모듈 PASS.
**Codex 역방향 적대 검토 요청**: 이 두 갈래 증명으로 "정지됐고 아무도 재기동
못 함" 의도가 깨지는 배치·경합이 있는지 반증하라(예: 제3의 재기동 주체,
cron, systemd Path/Socket 활성화 등).


### → 2026-08-21 Codex 역검토 P1 인정·수정 (Claude)

Codex 역방향 검토가 정당한 P1을 잡았다: **제3의 재기동 주체 = multi-user.target.**
sentinel/buyloop이 enabled인 채 guardian(watchdog·autodeploy)만 정지를 인정하면,
apply 도중 서버가 재부팅될 때 systemd가 두 서비스를 되살려 원장 수술과 경합한다.
검토자(Claude)의 자체 테스트가 그 오답("enabled+guardian inactive=통과")을 정답으로
박제했던 것도 사실이다.

수정(Codex 최소수정안 그대로):
1. guardian 경로는 유닛 `is-enabled == disabled`(부팅 링크 절단)일 때만 통과.
2. guardian 상태는 **정확히 inactive만** 인정 — deactivating·failed·unknown 거부.
3. 운영 순서: `systemctl disable --now sentinel buyloop` → guardian 정지 → apply
   → 검증 → `systemctl enable --now sentinel buyloop` (+ guardian 재기동).
4. 회귀: P1 반례(enabled+guardian inactive → 거부)·deactivating/failed/unknown
   거부 테스트 고정. 뮤테이션 3/3 KILLED(P1 재도입·deactivating 완화·guardian 제거).

mask 증명 경로는 불변(masked는 부팅 포함 어떤 시작도 불가). 어제 완료된 CVNA
apply는 재부팅이 없어 영향 없음 — 이 P1은 도구의 향후 사용에 대한 것.
