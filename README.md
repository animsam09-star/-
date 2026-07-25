# 상승 종목 원인·파급 분석 파이프라인

최근 주가가 강하거나 상승 전환한 KRX 종목을 스크리닝하고, 상승 **원인**을 뉴스·공시로 분류한 뒤,
원인이 **산업 공통 요인**일 때 동종업계·전방·후방 산업의 수혜 후보를 예측하는 자동화 파이프라인.

## 동작 흐름

```
1. 스크리닝 (src/screener.py)     pykrx로 전 종목 가격 수집 → 강세지속/상승전환 시그널 → 상위 30종목
2. 근거 수집 (src/collect.py)     네이버 뉴스 API + DART 공시 목록
3. 원인 분석 (src/analyze.py)     Claude API — 원인 분류(7유형) + 회사고유/산업공통 판정
                                  산업공통이면 valuechain/ 맵 참고해 파급 경로·수혜 후보 도출
                                  수혜 후보의 최근 수익률로 '미반영 체크' 후 종합 아이디어 생성
4. 리포트 (src/report.py)         reports/YYYYMMDD.html (GitHub Pages) + cases/ JSON 축적
5. 알림 (src/notify.py)           텔레그램 요약 발송 (+ Pages 링크)
```

핵심 설계: **회사 고유 이슈로 오른 종목은 파급 예측에 쓰지 않는다.** 산업 공통 요인만
밸류체인으로 확장하며, 이미 같이 오른 수혜 후보는 '기반영'으로 강등한다.

## 로컬 실행

```bash
pip install -r requirements.txt
python -m src.pipeline                  # 오늘 기준 전체 실행
python -m src.pipeline --skip-analyze   # 스크리닝+리포트만 (API 키 불필요)
python -m src.pipeline --date 20260724  # 특정일 기준
```

## 자동 실행 (GitHub Actions)

`.github/workflows/daily-screen.yml`이 **평일 17:10 KST**에 파이프라인을 실행하고,
결과를 커밋한 뒤 `reports/`를 GitHub Pages로 배포한다. Actions 탭에서 수동 실행(workflow_dispatch)도 가능.

### 필요한 설정

**1. Repository Secrets** (Settings → Secrets and variables → Actions):

| Secret | 필수 | 용도 |
|---|---|---|
| `KRX_ID` / `KRX_PW` | 권장 | KRX 정보데이터시스템([data.krx.co.kr](https://data.krx.co.kr)) 계정. 최근 KRX가 대량 조회에 로그인을 요구해, 미설정 시 가격 수집이 실패할 수 있음 |
| `ANTHROPIC_API_KEY` | 권장 | 원인 분석·파급 추론. 없으면 스크리닝만 수행 |
| `NAVER_CLIENT_ID` / `NAVER_CLIENT_SECRET` | 권장 | 뉴스 수집 ([developers.naver.com](https://developers.naver.com)에서 검색 API 앱 등록) |
| `DART_API_KEY` | 선택 | 공시 수집 ([opendart.fss.or.kr](https://opendart.fss.or.kr) 무료 발급) |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | 선택 | 텔레그램 알림 (@BotFather로 봇 생성 후 토큰, 봇과 대화 시작 후 chat_id 확인) |

**2. GitHub Pages 활성화**: Settings → Pages → Source를 **GitHub Actions**로 설정.

## 설정 조정

`config.yaml`에서 스크리닝 임계값(시총·거래대금 하한, 수익률·거래량 기준), 분석 대상 수,
모델 등을 조정한다.

## 밸류체인 맵 확장

`valuechain/*.yaml`이 파급 추론의 핵심 지식베이스다. 현재 건설·EPC, 전력기기, 조선, 반도체,
방산, 이차전지 6개 산업이 시드로 들어 있다. 산업 추가 시 같은 형식(peers / upstream /
downstream / notes)으로 파일을 추가하면 자동 반영된다. DART 사업보고서의 매출처·원재료
비중으로 notes를 보강할수록 수혜 후보의 정확도가 올라간다.

## 케이스 축적

매 실행마다 `cases/YYYYMMDD.json`에 후보·분석·아이디어가 쌓인다. 장기적으로
"수주 모멘텀형 상승은 N주 후 후방 기자재로 파급" 같은 시차 패턴 백테스트에 활용할 수 있다.

> 본 자료는 자동 생성된 참고 자료이며 투자 권유가 아닙니다.
