# 4.1.6 패치노트

## 대시보드

- 주문·보호 현황 열 너비와 행 정렬 개선
- 일반 주문과 OCO를 최신 변경 시각순으로 함께 표시
- 긴 주문번호 축약 및 클릭 복사 지원

## 종목명

- 토스 `/stocks`에서 확인한 종목명을 `stock_references` 테이블에 저장
- 계좌에서 사라진 종목도 판단·주문·매매·운영 기록에 저장된 이름 사용
- 토스에서 확인되지 않거나 거래 불가한 AI 발굴 후보는 자동 제외
- 이름을 확인할 수 없는 과거 기록은 코드 대신 `종목명 미확인`으로 명확히 표시

## AWS 구조 고정

- 설정 파일: `~/.config/ai-stock-assistant/worker.env`
- Python 가상환경: `~/.venvs/ai-stock-assistant`
- Git 저장소에는 소스코드만 유지
- systemd에서 Python 바이트코드 생성을 차단해 `__pycache__` 충돌 방지
- 안전 설치: `./harden_aws_worker.sh`
- 이후 업데이트: `./update_aws_worker.sh`
