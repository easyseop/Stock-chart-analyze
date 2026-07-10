# KIS Stage 1.5 밤샘 작업 핸드오프 (2026-07-10 밤)

> 네가 자는 동안 진행한 것 / 아침에 네가 할 것 / 검토 포인트. 짧게.

## 1. 밤새 완료된 것 (전부 커밋·푸시·테스트 green)

| 모듈 | 내용 | 테스트 |
|---|---|---|
| `bot/ledger.py` 확장(O1) | ODNO 결속·합성 대사키·confidence(HIGH/LOW)·`can_submit`(동일종목 in-flight 1건·60초 간격) | test_ledger 11케이스 |
| `bot/kis_reconcile.py`(O3) | nccs+ccnl → UNKNOWN 후보 귀속 → HIGH 자동확정/LOW 잠금유지. 쌍둥이주문·완전체결·교차 오귀속·시간윈도우 방어 | test_kis_reconcile 5 |
| `bot/kis_ratelimit.py` | 초당 버킷(모의2/실전20)·order-plane 예약슬롯·61초류 대기 금지. `kis._get` 연결 | test_kis_ratelimit 4 |
| `bot/kis_orders.py`(O2) | 주문/취소 primitive — **live 하드블록**·`KIS_ORDERS_ENABLED=1` 게이트·원장 선기록→ODNO 결속·EGW00201 1회 백오프·타임아웃=UNKNOWN 잠금·마켓터블 지정가 헬퍼 | test_kis_orders 9 |
| `tests/test_kis_faults.py` | **UNKNOWN 전체 루프 통합**: 타임아웃→ccnl 대사(부분체결2/5)→HIGH 해제→잔여 3만 재주문→합산 무결 | 통합 1 |
| `scripts/kis_mock_roundtrip.py` | 아침에 네가 돌릴 모의 왕복 스크립트(아래) | — |

전체 스위트: **16개 테스트 모듈 all green.** 리뷰 R1(flock)·R2(plane 분리)·R3(confidence·동일종목)은 코드+테스트로 반영 완료.

## 2. 아침에 네가 할 것 (10분)

```bash
git pull origin claude/happy-gauss-cwoq21
export KIS_ENV=mock KIS_MOCK_APPKEY=... KIS_MOCK_APPSECRET=... KIS_MOCK_CANO=...
export KIS_ORDERS_ENABLED=1          # ← 주문 게이트(이게 있어야 전송)
python scripts/kis_mock_roundtrip.py # 기본: 체결 안 되는 왕복(포지션 0 유지)
```
- **하는 일**: 현재가 50% 지정가 매수 1주 접수 → nccs에서 확인 → 취소 → ccnl 확인. **모의 돈으로도 포지션을 안 만드는** 안전 흐름.
- **주의**: 미국 정규장 시간대(KST 밤 22:30~05:00)가 아니면 접수 거부가 정상 — 그 msg_cd도 수확 대상이니 출력 그대로 공유.
- 유량(2/s) 때문에 단계 사이 자동 대기 들어있음.

## 3. 이 왕복으로 실측 확정되는 것 ([대조필요] 해소 목록)
1. 모의 매수 TR `VTTT1002U` 실수용 + 주문응답 output 형태
2. **nccs에 내 주문 표시**(대사 채널 A 실증) + output 필드 실물
3. 취소 TR `VTTT1004U` + `ORGN_ODNO` 계약
4. 취소/미체결 건의 **ccnl 표기 형태**(대사 채널 B의 실제 커버리지)
5. 시세 TR `HHDFS00000300` 모의 지원 여부
6. `MGCO_APTM_ODNO` 에코 여부(응답에 실리면 대사 신뢰도 상승 — 리뷰 A5)

## 4. 그 다음 순서 (남은 Stage 1.5 → 2)
- 왕복 green이면: `--fill` 모드로 모의 체결 1건 관찰(체결 시뮬 품질) → 파수꾼에 `place_sell` 연결(X4, dry-run 병행) → 부팅 대사(O4) 루프.
- 병행 가능(네 작업): **KIS Developers에서 실전용 봇 계좌 계획** — 실전 계좌+전용 appkey(IS1-A 물리격리)는 Stage 2 직전에.

## 5. 검토 포인트 (아침에 코드 볼 때)
- `kis_orders.place_order`의 **live 하드블록**(orders_allowed)이 마음에 드는지 — 지금은 어떤 플래그로도 live 전송 불가. Stage 2 때 별도 게이트 설계로 푼다.
- 동일종목 최소간격 기본 60초 — Stage 1.5 검증 스크립트만 `min_interval_s=0` 사용. 실운영은 60초 유지.
- `marketable_limit_price` 기본 슬리피지 30bp — 손절 체감에 맞춰 조정 여지.
