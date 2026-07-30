# 수정주가 반영 대상 기업 리스트 (DART 기반)

2023-01-01 이후 KOSPI·코스닥 상장사 중 수정주가 조정 사유가 발생한 기업을
DART 공시에서 전수 추출하는 스크립트입니다.

## 수집 대상 공시 (= 수정주가 조정 사유)

| 공시 유형 | 조정 사유 |
|---|---|
| 무상증자결정 / 유무상증자결정 | 권리락 |
| 유상증자결정 | 권리락 (주주배정·일반공모만 해당, 3자배정 제외) |
| 주식분할결정 | 액면분할 기준가 변경 |
| 주식병합결정 | 액면병합 기준가 변경 |
| 감자결정 | 기준가 변경 |
| 주식배당결정 | 배당락(주식) |

현금배당은 KRX 수정주가 관행상 조정하지 않으므로 수집하지 않습니다.

## 실행

```bash
# OpenDART API 키 발급(무료): https://opendart.fss.or.kr
export DART_API_KEY=발급키
python scripts/dart_adjusted_price_events.py --since 20230101
# -> output/adjusted_price_events.csv 생성 (엑셀에서 바로 열림)
```

OpenDART가 차단된 사내망 등에서는 본사이트 검색 폴백을 사용합니다
(비공식 엔드포인트라 동작이 바뀔 수 있음):

```bash
python scripts/dart_adjusted_price_events.py --mode dartsite --since 20230101
```

## 이 클라우드 세션에서 직접 실행하려면

현재 Claude Code 원격 환경의 네트워크 정책이 GitHub 외 외부 접속을
차단하고 있어 DART 조회가 불가합니다. claude.ai/code 환경 설정에서
네트워크 허용 도메인에 `dart.fss.or.kr`, `opendart.fss.or.kr` 를 추가하면
세션 안에서 바로 실행·검증할 수 있습니다.

## 결과 열

공시일 · 시장 · 회사명 · 종목코드 · 이벤트 · 보고서명 · 정정여부 ·
접수번호 · DART 링크 · 비고
