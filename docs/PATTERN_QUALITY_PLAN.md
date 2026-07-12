# 패턴 품질(Pattern Quality) — GPT 제안 반영 결정 + 진행 상태

> 2026-07-12. 사용자 업로드 차트 치트시트(패턴·캔들·이평·지지저항) → GPT 제안서
> (`CHART_PATTERN_FEATURE_PROPOSAL.md`) → 본 결정. **원칙: 패턴 '이름' 분류 금지,
> 가격 구조를 스칼라로 수치화 → 기록 → 분위수 검증 → 증명된 것만 정렬/티어 승격.**
> 매매 행동(점수·verdict·게이트·타점·손절)은 검증 전까지 **절대 불변**.

## 결정 요약 (채택/보류/기각)

| 제안 | 결정 | 근거 |
|---|---|---|
| 3.1 `clear_space_R` | ✅ **채택(Phase 0 구현됨)** | 최고 아이디어 — 손절만 R로 재고 상방 저항은 R로 안 재던 갭. 기존 levels.strong·box·POC 재사용 |
| 3.2 `ATR14/ATR60` 수축 | ✅ 채택(구현됨) | "3봉 안착=시간조건"의 베이스 품질 보완 |
| 3.3 `close_location` | ✅ 채택(구현됨) | 거래량 1.5배 조건의 품질 보완(윗꼬리 돌파 구분) |
| 3.4 `wick_score` | 🟡 기록만(구현됨) | 점수화는 3.3과 공선성 확인 후 |
| 3.8 `extension_ATR` | 🟡 기록만(구현됨) | RSI<70 대체는 A/B 검증 후(§6.3) |
| 3.5 쌍바닥 / 3.6 reclaim / 3.7 retest | ⏸ **보류** | 자유 파라미터 과다(과최적화 위험 최대). 단순 스칼라가 증명된 뒤 재검토 |
| 3.9 하락패턴→트레일 강화 | ⏸ 보류(백테스트 A/B로만) | 기존 청산 A/B/C 하네스에 변형 추가로 검증 먼저 |
| §7 "넣지 말 것" 전부 | ✅ 동의 | 이름 하드룰·OR 입구·지표 무더기·저점권 완화 금지 |

**프레이밍 교정**: 제안서의 "수평 매물대를 안 본다"는 부분 부정확 — `support_resistance()`
가 이미 박스·POC·라운드넘버를 봄. clear_space_R은 **기존 수평 레벨을 R 단위로 연결**하는
작업(신규 검출이 아님).

## 구현 상태 (Phase 0~1 완료)

- ✅ `scanner/pattern_quality.py` — 5지표 계산(+quality 0~3 합산). 순수 계산·기록 전용.
- ✅ `scanner/analyze.py` — 결과에 `pattern` 첨부. **테스트로 불변 증명**: pattern을
  최악값으로 바꿔도 norm/verdict/entry/stop 동일(`test_analyze_wiring_record_only`).
- ✅ `scanner/card.py` — `패턴품질: 📐 품질 n/3 · 저항까지 x.xR · 수축 · 종가위치 (기록용)`.
- ✅ `scanner/backtest.py` — `Signal`에 cs_r/atr_ct/close_loc/ext_atr/**MFE/MAE** 추가,
  `cli_pattern_quantiles`(분위수별 기대R·승률·+1R/+2R·MFE/MAE, §6.2 방법론 그대로).

## 다음 관문 (Phase 2 승격 조건)

```bash
# 캐시 있는 곳(GitHub Actions 배치 후 로컬/러너)에서:
python -c "from scanner.backtest import cli_pattern_quantiles; cli_pattern_quantiles()" -- --sample 80
```
- **승격 규칙**: Q1→Q4 기대R·+2R 도달률이 **단조 개선**(그리고 표본외에서도 유지)인
  지표만 now 후보 **정렬 키**에 추가(진입 여부는 계속 불변).
- 그 다음(Phase 3): stage③ + 품질 낮음 → risk 0.5× 티어(코드 반영은 별도 결정).
- 하드 게이트(Phase 4)는 반복 증거 없인 금지(제안서 §2 그대로).

## 관련 파일
`scanner/pattern_quality.py` · `tests/test_pattern_quality.py` ·
`scanner/backtest.py`(cli_pattern_quantiles) · 제안 원문: 사용자 업로드
`CHART_PATTERN_FEATURE_PROPOSAL.md`(2026-07-12).
