# AI Stock Assistant 3.0

국내·미국 주식시장을 분석하고, 정해진 위험관리 기준을 통과한 주문만 토스증권 Open API로 자동 실행하는 투자 보조 시스템입니다.

## 현재 운영 구조

```text
Render Web Dashboard
  ├─ 대시보드 화면
  ├─ 자동매매 시작/중지 버튼
  ├─ AI 행동패턴 드롭다운
  └─ 수익 그래프 표시

Render PostgreSQL
  ├─ 계좌/보유종목 최신 상태
  ├─ AI 판단 기록
  ├─ 매매 기록
  ├─ 수익률 스냅샷
  └─ 사용자 제어 상태

AWS 고정 IP 주문봇
  ├─ 토스증권 계좌/시세 조회
  ├─ OpenAI 투자 분석
  ├─ 위험관리 검증
  ├─ 실제 매수/매도 주문
  └─ Telegram 알림
```

Render는 웹과 DB만 담당하고, 토스증권 API 호출과 실제 주문은 AWS 고정 IP 서버에서 실행합니다. 따라서 집 PC를 24시간 켜두지 않아도 자동매매가 계속 동작합니다.

## 주요 기능

- 토스증권 Open API 기반 실계좌 조회와 주문
- Render 웹 대시보드와 PostgreSQL 분리 운영
- AWS 고정 IP 주문봇 24시간 실행
- 계좌·시세 5분마다 갱신
- 신규 종목 탐색과 AI 판단 30분마다 실행
- 국내·미국 주문 Telegram 알림
- 국내·미국 장 구분 감지
- 대시보드에서 프리·애프터마켓 거래 허용 토글
- 프리·애프터마켓에서는 시장가 금지, 현재가 기준 ±0.5% 지정가 자동 산정
- 대시보드에서 AI 행동패턴 변경
  - 홀드
  - 보수적
  - 기본형 · 보수적 수익추구
  - 공격적
  - 초공격형 · 최대수익 지향
- 일별/주별/월별 수익률 그래프
- 최근 투자 판단에서 현재가, 매수가, 평단가, 보유 수량, 평가금액 확인
- 판단 이유는 접어서 표시하고, 긴 URL은 제거하며, 링크는 `[DART 공시]`, `[삼성전자]` 같은 별도 버튼으로 분리
- `/decisions` 별도 화면에서 최근 7일 투자 판단 확인

## 행동패턴 기준

기본형은 원금 방어를 우선하되, 근거가 강한 수익 기회는 놓치지 않는 방향입니다.

`초공격형 · 최대수익 지향`은 위험 한도를 더 여는 방식으로 동작합니다. 단, 아래 제한은 어떤 성향에서도 우회하지 않습니다.

- 미수·신용 거래 금지
- 레버리지·인버스·ETN·파생상품 금지
- 투자경고·위험·과열 등 차단 대상 종목 금지
- 현금 매수 가능 금액 초과 금지
- 매도 가능 수량 초과 금지
- 기본값은 정규장 외 주문 금지
- 대시보드에서 `프리·애프터마켓 거래 허용`을 켠 경우에만 정규장 외 주문 허용
- 프리·애프터마켓 주문은 반드시 지정가만 허용
- 신뢰 가능한 서로 다른 출처 2개 미만이면 주문 금지

## 프리·애프터마켓 거래

대시보드의 `프리·애프터마켓 거래 허용` 토글을 켜면 국내 NXT 프리/애프터, 미국 프리/애프터 시간에도 주문 후보를 처리할 수 있습니다.

안전장치는 아래처럼 동작합니다.

- 토글 OFF: 정규장 주문만 허용
- 토글 ON: 프리·애프터 주문 허용
- 프리·애프터 주문 방식: 무조건 지정가
- 지정가 산정: 현재가 기준 기본 ±0.5%
  - 매수: 현재가보다 최대 0.5% 높은 가격
  - 매도: 현재가보다 최대 0.5% 낮은 가격
- 0.5%를 1.0%로 넓히려면 AWS `.env`에서 `EXTENDED_LIMIT_PRICE_BUFFER_PCT=0.01`로 변경 후 주문봇을 재시작합니다.

미국 데이마켓은 기본적으로 꺼져 있습니다. 꼭 필요할 때만 AWS `.env`에서 `US_DAY_MARKET_ENABLED=true`로 바꾸세요.

## 갱신 주기

```env
MARKET_POLL_INTERVAL_SECONDS=300
ANALYSIS_INTERVAL_SECONDS=1800
```

- 계좌·시세 갱신: 5분마다
- AI 탐색·판단: 30분마다
- 웹 화면 표시 새로고침: 60초마다

웹 화면이 60초마다 새로고침되더라도, 실제 토스 계좌 조회는 AWS 주문봇의 300초 주기를 따릅니다.

## Render 배포

Render에는 다음 2개만 있으면 됩니다.

```text
ai-stock-assistant-web
ai-stock-assistant-db
```

Render Worker는 사용하지 않습니다. 실제 주문봇은 AWS에서 실행합니다.

Render Web 환경변수 핵심값:

```env
BROKER_MODE=toss
BROKER_API_ENABLED=false
LIVE_TRADING_ENABLED=true
MARKET_POLL_INTERVAL_SECONDS=300
ANALYSIS_INTERVAL_SECONDS=1800
EXTENDED_HOURS_ENABLED_BY_DEFAULT=false
EXTENDED_LIMIT_PRICE_BUFFER_PCT=0.005
US_DAY_MARKET_ENABLED=false
```

Render Web에는 토스 Client Secret, OpenAI Key, Telegram Token을 넣지 않아도 됩니다. 민감한 키는 AWS 주문봇에만 넣는 것을 권장합니다.

## AWS 주문봇 실행

자세한 절차는 [AWS_고정IP_주문봇_가이드.md](./AWS_고정IP_주문봇_가이드.md)를 보세요.

핵심 순서:

1. AWS Lightsail Linux 인스턴스 생성
2. Static IP 생성 후 인스턴스에 연결
3. 토스증권 Open API 허용 IP에 AWS Static IP 등록
4. Render PostgreSQL External Database URL 복사
5. AWS 서버에 프로젝트 업로드
6. `.env.aws.example`을 `.env`로 복사하고 실제 값 입력
7. `setup_aws_worker.sh` 실행
8. `run_aws_worker.sh` 실행
9. 대시보드에서 `자동매매 시작` 클릭

## PC 주문봇

PC 주문봇은 임시/백업 방식입니다. AWS를 사용하지 못하는 경우에만 [PC_주문봇_실행_가이드.md](./PC_주문봇_실행_가이드.md)를 사용하세요.

## 주의

이 프로그램은 수익을 보장하지 않습니다. 자동매매는 손실을 낼 수 있으며, 특히 `초공격형 · 최대수익 지향`은 일반 사용을 권장하지 않습니다.
