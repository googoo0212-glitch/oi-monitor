# OI 실시간 모니터

- `index.html` — 실시간 가격(하이퍼리퀴드, 5초마다)·계정별 위험·펀딩 추이 페이지 (GitHub Pages)
- `scripts/record_funding.py` + `.github/workflows/funding.yml` — 30분마다 Variational 공개 API에서 금·은 펀딩을 받아 `data/`에 기록
- `data/positions.json` — 계정별 포지션·청산가·SL/TP. **증거금을 옮기거나 포지션을 바꾸면 이 파일만 수정** (GitHub에서 연필 아이콘 → 수정 → Commit)

⚠ 공개 저장소이면 이 페이지와 positions.json은 링크를 아는 누구나 볼 수 있어요. 지갑 주소는 넣지 않았어요.

## 텔레그램 알림
- `.github/workflows/alerts.yml` — 5분마다 현재가(하이퍼리퀴드)로 SL·TP·청산가까지 거리 확인 → 가까워지면 알림
- 알림 기준은 `data/alert_config.json` (금 1.5%/0.7%, 은 2.5%/1.2%)
- Settings → Secrets and variables → Actions 에 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` 등록 필요

## Entropy (DRAM/EWY 페어 OI)
- `entropy.html` — DRAM·EWY 가격/펀딩/Entropy 미결제, 7일 상관·베타·스프레드, 계정별 청산 여유·순노출·리밸런싱 제안 (10초마다)
  - 지갑 주소는 페이지의 **계정 주소 설정**에 넣으면 그 브라우저에만 저장돼요 (레포엔 안 올라감)
- `scripts/entropy_monitor.py` + `.github/workflows/entropy-alerts.yml` — 10분마다 텔레그램 알림
  - 포지션 사라짐/방향 뒤집힘(🚨), 포지션별 청산가 근접(격리), 마진 재분배 제안(수익 포지션 이익 → 손실 포지션 마진, 금액까지), 두 포지션 크기 불균형, 계정 합산 순노출, 매일 09시 요약
  - Entropy는 격리만 지원 → 교차 전용 항목(스프레드 여유·유지증거금 비율·계정 증거금 리밸런싱)은 교차일 때만 동작
- `.github/workflows/entropy-record.yml` — 30분마다 `data/entropy_history.json` 기록 (라벨·숫자만, 주소 없음)
- 설정: `data/entropy_config.json` (계정 라벨, 각 계정이 들고 있어야 할 방향, 알림 기준)
- **Secrets 추가 필요**: `ENTROPY_ACCOUNTS` = `{"A":"0x…","B":"0x…"}`, `ENTROPY_TELEGRAM_BOT_TOKEN` (Entropy 전용 봇, 없으면 기존 봇으로 보냄). 받는 채팅은 기존 `TELEGRAM_CHAT_ID`
- '받은 펀딩' 칸은 API의 cumFunding 부호를 뒤집어 표시 — 첫날 Entropy 화면 펀딩 내역과 부호 한 번 대조하기
