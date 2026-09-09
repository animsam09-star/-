# 건설사 데이터센터 수주 검토 (DART 기준, 2017Q1~2022Q4)

- `docs/datacenter_orders_review_17Q1_22Q4.md` — 검토 결과 보고서(결론, 후보 목록, 근거 URL, 용어 설명, 자가검토)
- `data/user_list_datacenter_orders_17Q1_22Q4.tsv` — 검토 대상으로 제공된 수주 목록(86행)
- `tools/dart_dc_census.py` — DART 정기보고서 수주 표 전수조사 스크립트 (DART 접속 가능한 환경에서 실행)
- `tools/dart_lib.py` — DART 문서 수집·표 파싱 라이브러리

실행:
```bash
python3 tools/dart_dc_census.py --out out/
```
