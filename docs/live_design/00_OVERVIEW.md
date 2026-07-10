# 토스 실주문 자동매매 — 전체 설계 (00 개요 / 읽는 법)

> **⚠️ 읽는 법 (컨텍스트 손실 방지 — 리뷰어·독자 모두 지켜라)**
> 이 설계는 6개 파일로 나뉜다. **한 번에 다 읽지 말 것.**
> - 파일 순서: `00_OVERVIEW → 01_STAGE1_ACCOUNT_QUOTE → 02_ORDER_PRIMITIVES →
>   03_EXECUTION_PROTECTION → 04_INFRA_GATES → 05_TESTING_VERIFICATION →
>   06_REVIEW_V2_CHANGES → 07_ACCOUNT_ISOLATION_BUDGET`.
> - **⚠️ `06_REVIEW_V2_CHANGES.md`(Codex 리뷰)와 `07_ACCOUNT_ISOLATION_BUDGET.md`(계좌
>   격리·시드 봉투)가 확정 오버라이드다.** 충돌 시 06·07 우선. 07은 X1/X2/L4를 오버라이드.
>   특히 **라이브 사이징이 계좌 equity가 아니라 고정 SEED 기준**이어야 함(현행 버그).
> - **각 파일 안에서도 `##`(섹션=태스크) 하나씩 끊어 읽고, 그 태스크만 판정한 뒤
>   다음으로 넘어가라.** 태스크 하나가 What/Why/How/주의/테스트/의존으로 자기완결.
> - 리뷰는 `REVIEW_PROMPT.md`의 지시를 따른다(태스크별 채택/수정/기각 + 실패 시나리오).

---

## 1. 목표와 현재 위치

- **목표**: 차트 전략(52주 저점권 전환 초입 매수·ATR 손절·손익비 1:2·일봉 스윙)을
  **토스 실계좌로 자동 집행**. 시드 1억, 종목당 1/3 캡, 1% 리스크.
- **현재**: GitHub Actions 무료 배치 + CF Worker + 매도전담 파수꾼(dry-run)으로
  **가상 모의투자**. 토스는 **시세 읽기 전용(Stage 0)** 어댑터까지 구현. **실주문 0.**
- **이 설계가 다루는 것**: Stage 0 이후 → 계좌읽기(그림자) → 주문 primitive →
  실행/보호 → 실서버/게이트 → 테스트까지, **실주문에 도달하는 전 구간**.

## 2. 목표 아키텍처 (실주문 시)

```
[GitHub Actions · 무료]  풀스캔·렌더·Pages·신호 feed 생산.  ★ 주문 키 없음(금지)
        │  signals feed (state 브랜치)
        ▼
[상시 서버 · 고정 IP · $0 VM]        ← 주문 가능 토스 키는 여기에만
  루프A 시세→전략(1분): 토스 시세 + 신호 feed → 게이트/사이징 → 의도 산출
  루프B 주문 집행(이벤트): 진입/청산 → toss_orders + 원장 + 폴링/대사
  루프C 파수꾼(10~20초): 보유 손절 감시·매도(매수 경로 없음)
  공통: 단일 token_manager · 그룹별 rate limiter · preflight · 부팅 대사
        │  heartbeat
        ▼
[CF Worker]  GitHub 발사·검증 + 서버 하트비트 dead-man(B5)
```

**핵심 불변식** (설계 전체를 관통):
1. **주문 결과를 모르면(UNKNOWN) 절대 두 번 넣지 않는다** — 원장 잠금·대사·잔여만.
   ★ 단 **clientOrderId는 읽기 응답에 없다(write-only)** → 10분 내 POST 재시도가 유일한
   확실 복구, 그 밖은 heuristic 매칭, 모호하면 MANUAL_REVIEW_LOCK (06-A 참조).
2. **보호주문(손절)이 실패하면 신규 리스크를 늘리지 않는다** — 신규중지·파수꾼전환.
   강제청산은 기본값 아님. **단 무방비를 방치하지도 않는다** — SLA 프로토콜(06-E).
3. **브로커가 진실** — 보유·주문가능수량·주문상태는 매번 브로커 재조회로 대사.
4. **깃에는 주문 키를 두지 않는다** — 집행은 고정 IP 상시 서버에서만.
5. **추정으로 매매 경로를 만들지 않는다** — 단, **live 주문 호출 경로만** V2 후. O1·VH는
   V2 전 선행(06-C 순서).

## 3. Stage 게이팅 (각 단계 통과해야 다음)

| Stage | 범위 | 주문 | 리스크 | 이 설계의 태스크 |
|---|---|---|---|---|
| 0 | 시세 읽기 | 가상 | 0 | (완료) |
| 1 | +계좌 조회·대사 | 가상 | 0 | 파일 01 (L1~L6) |
| 1.5 | +주문 primitive·상태전이 실측 | 최소단위 수동 1건 | 극소 | 파일 02 (O1~O4) |
| 2 | +자동 실주문 | 0.1%·1종목·하루1건 | 소액 | 파일 03 (X1~X4,P1~P2) |
| 3+ | 제한→정상 | 0.25→1% | 단계적 | 파일 04·05 게이트·테스트 통과 후 |

## 4. 전체 태스크 인덱스 (27개 + Codex 리뷰 반영 → 06 참조)

**파일 01 — Stage 1 (계좌읽기·시세정확도, 지금 안전하게 가능)**
- L1 계좌 헤더 GET plumbing · L2 accounts/accountSeq 결정 · L3 holdings/buying-power/
  sellable 읽기 · L4 브로커↔내부 대사(외부 포지션 감지) · L5 토스 실시간가 매매 주입(0.5) ·
  L6 파수꾼 하드닝(orderbook·sellable 단계화·quote-age)

**파일 02 — Stage 1.5 (주문 primitive, 실측 게이트)**
- O1 원장 확장(clientOrderId·body_hash·10상태) · O2 toss_orders(생성/조회/취소) ·
  O3 체결 폴링 루프 · O4 부팅/크래시 대사 · **CO1 조건주문 primitive(신설, 06-B)**

**파일 03 — Stage 2 (실행·보호)**
- X1 라이브 매수 실행기 · X2 라이브 사이징(실 buying-power·환율·체결가) · X3 환전 처리 ·
  X4 파수꾼 실매도 · P1 조건주문(OCO/STOP·등록SLA·만료갱신·고아취소·부분체결) ·
  P2 보호주문 커버리지 감시

**파일 04 — 인프라·게이트·안전**
- I1 진입 게이트(status/warnings·세션·기업행위) · I2 상시 서버 + dead-man ·
  I3 token_manager + rate limiter + NTP · I4 환경분리 플래그 + 깃 주문키 제거 ·
  I5 장전 preflight · I6 kill-switch 레벨2~4 · I7 단계적 롤아웃 가드(코드 강제)

**파일 05 — 테스트·검증**
- VH 장애주입 하네스 · VR 실측 런북(V1~V3) · VA 계좌 설정 확인

## 5. 이미 구현된 골격 (⚠️ 재작성 금지, 확장만)

| 있는 것 | 파일 | 남은 일 |
|---|---|---|
| 시세 어댑터·토큰·쿨다운 | `bot/toss.py` | 계좌·주문 확장(별 파일) |
| `classify_error` | `bot/toss.py` | 주문 호출에 **연결**(O2) |
| `client_order_id` | `bot/toss.py` | 원장에 **저장·사용**(O1) |
| 원장(5상태·잠금·대사·잔여) | `bot/ledger.py` | 10상태·clientOrderId·body_hash **확장**(O1) |
| 파수꾼 dry-run·멱등키·원장연동 | `bot/sentinel.py` | 실 place_sell/order_status **채우기**(X4) |
| kill-switch L1 | `scanner/autopaper.py` | L2~4 **추가**(I6) |
| ntfy P0 이중화 | `bot/notify.py` | (완료) |

→ Codex 리뷰 시 "새로 만들라"가 아니라 "이 골격을 어떻게 확장/연결하냐"로 볼 것.
