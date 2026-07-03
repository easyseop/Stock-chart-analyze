# 봇 — 알림(현재 사용 중) + 실행 스켈레톤(추후)

**두뇌/손 분리**: GitHub Actions 스캐너(15분)가 `/api/signals.json`(두뇌)을 발행한다.
지금은 이 시그널로 **텔레그램 매수/매도 제안 알림만** 보낸다(주문 없음). 나중에
증권사 API를 붙이면 같은 판단 로직으로 실제 주문까지 낼 수 있는 구조.

## 1. advisor.py — 매수/매도 제안 알림 (지금 쓰는 것)

GitHub Actions `advisor.yml`이 장중 30분마다 자동 실행 — **로컬에 아무것도 안 켜둬도 됨**.

- **매수 제안**: signals.json '지금 진입' 그룹에 새로 뜬 종목마다 1회 알림
  (진입가·손절·목표·저점권%·참고수량)
- **매도 제안**: `holdings.json`(직접 기록한 보유 종목)의 현재가가 손절/목표에 닿으면 알림

### 설정 (최초 1회, 5분)
1. 텔레그램에서 **@BotFather**에게 `/newbot` → 봇 토큰 받기
2. 그 봇과 대화 시작 후, **@userinfobot**에게 아무 메시지나 보내 내 chat_id 확인
   (또는 만든 봇에 메시지 보낸 뒤 `https://api.telegram.org/bot<TOKEN>/getUpdates`에서 확인)
3. GitHub 저장소 → Settings → Secrets and variables → Actions → New repository secret
   - `TELEGRAM_BOT_TOKEN` = 1번 토큰
   - `TELEGRAM_CHAT_ID` = 2번 chat_id
4. 끝. 다음 장중 실행부터 알림이 옴. Actions 탭에서 `buy-sell-advisor` 수동 실행(workflow_dispatch)으로 바로 테스트 가능.

### 보유 종목 등록 — holdings.json
직접 사서 갖고 있는 종목을 매도 알림 대상으로 넣으려면 저장소 루트의
`holdings.json`을 편집·커밋(형식은 `holdings.example.json` 참고):
```json
[{"code":"005930","name":"삼성전자","ccy":"KRW","qty":10,"avg":71000,
  "stop":65000,"target":85000}]
```
손절·목표는 상세 페이지의 '매매 전략' 카드 값을 그대로 넣으면 됨. 팔았거나
알림 그만 받고 싶으면 해당 항목을 지우고 커밋.

```
python -m bot.advisor --once --dry-run     # 로컬 테스트(텔레그램 전송 없이 콘솔 출력)
```

## 2. trader.py — 실행 스켈레톤(자동매매, 추후용)

**두뇌/손 분리**: GitHub Actions 스캐너(15분)가 `/api/signals.json`(두뇌)을 발행하고,
이 봇(손)이 그걸 폴링해 가드를 통과한 시그널만 매매한다. 기본은 **페이퍼 모드**(실주문 없음).
지금은 로컬에서 직접 돌려야(--loop) 하는 실행 엔진 — advisor.yml처럼 Actions 자동화는
아직 없음(실주문 전이라 신중하게 로컬에서 먼저 검증하는 용도).

```
python -m bot.trader --once      # 1회: 시그널 확인 → 매매 판단 → 종료
python -m bot.trader --loop      # 5분 간격 반복 (장중 켜두기)
python -m bot.trader --status    # 보유/손익 현황
```

## 안전 가드 (settings.py)
- **가격 괴리**: 현재가가 진입가 ±1.5% 이내일 때만 매수 (15분 신호 지연 보호)
- **확정봉 모드**: 전 거래일 시그널에도 있던 종목만 (미확정 일봉의 가짜 돌파 방지)
- **한도**: 동시 보유 5종목, 일일 실현손실 −2% 도달 시 신규 중지
- **수량**: 1회 리스크 = 계좌의 1% (손절폭 기준 자동 계산)

## 토스 API 연동 (다음 단계)
1. 토스증권 개발자 포털에서 앱 키 발급
2. `bot/broker.py`의 `TossBroker` 구현: 인증 → 실시간 시세(quote) → 주문(buy/sell)
3. `settings.py`에서 `BROKER = "toss"` 전환 — **반드시 페이퍼로 충분히 검증 후**

상태 파일(state.json, seen.json)은 봇 로컬 저장이며 git에 올라가지 않는다.
