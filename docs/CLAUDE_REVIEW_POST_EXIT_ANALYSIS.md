# Claude 검토 요청 — 익절 사후추적

## 검토 대상

- `bot/post_exit.py`
- `scripts/post_exit_refresh.py`
- `bot/portfolio_web.py`
- `scanner/site_app/app.js`
- `scanner/site_app/app.css`
- `infra/server/post-exit-refresh.*`
- `tests/test_post_exit.py`
- `tests/test_site_app.py`
- `docs/POST_EXIT_ANALYSIS_DESIGN.md`

## 반드시 반증할 질문

1. 미국 체결의 KST 날짜를 뉴욕 세션 날짜로 바꾸지 않아 첫 사후 거래일을
   건너뛰는 경로가 남아 있는가?
2. 매도 당일 일봉 고가를 `익절 뒤 추가 상승`으로 잘못 세는가?
3. `평단 대비 총 상승`, `평단 기준 추가 %p`, `매도가 대비 놓친 상승`의
   분모가 서로 뒤섞였는가?
4. 1·3·5·10·20거래일 중 미완료 기간이 완료 통계에 들어가는가?
5. `submitted-fallback`, `legacy-ledger-price`, `balance-average` 등 추정
   매도가가 확정 표본이나 공통점 통계로 승격되는가?
6. 같은 체결이나 같은 종목 복수 익절이 중복·덮어쓰기로 잘못 집계되는가?
7. 손실매도·무효 평단·무효시각·손상 거래원장이 수익 매도로 섞이는가?
8. 익절 세션 뒤 종목 재상장·티커변경·일봉 결손에서 숫자를 추측하는가?
9. API/HTML에 주문키, ODNO, pos_key, 원장 경로, 계좌·인증정보가 노출되는가?
10. 종목명·사유가 DOM XSS로 들어가는 경로가 있는가?
11. HTTP 새로고침이 FinanceDataReader나 KIS를 호출해 트래픽·렉을 늘리는가?
12. refresh worker가 KIS/order/kill import 또는 `kis.env`를 받아 매매 경계에
    닿는가?
13. worker OOM·timeout·네트워크 hang이 sentinel/buyloop를 굶기거나 죽일 수
    있는가?
14. 발행 중 크래시·디스크 손상·부분 JSON에서 웹이 이전값을 최신값으로
    과장하거나 주문에 영향을 주는가?
15. 공통점 표본 1~2건을 결론처럼 표시하거나 verified와 estimated를 섞는가?
16. 모바일 320~430px에서 3단 가격 흐름·4개 지표·필터가 가로로 깨지는가?

## 승인 기준

- P0/P1 없음
- 추정가와 확정가가 통계까지 완전히 격리
- HTTP 경로 KIS/외부 네트워크 0건
- worker가 주문 plane과 import·환경·자원 면에서 분리
- 계산 테스트와 공개/비공개/XSS 경계 테스트 통과

P0/P1이 있으면 병합·Oracle timer 설치를 차단한다. P2는 데이터 품질 또는
운영 가용성에 영향을 주면 병합 전 수정을 권고한다.
