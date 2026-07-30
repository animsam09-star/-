#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DART에서 수정주가 조정 사유 발생 기업 리스트 추출.

2023-01-01 이후 유가증권(KOSPI)·코스닥 상장사의 수정주가 반영 대상
공시(무상증자·유상증자·주식분할·주식병합·감자·주식배당 결정)를 전수
조회해 CSV로 저장한다.

사용법:
    # 1) OpenDART API 사용 (권장, https://opendart.fss.or.kr 에서 무료 키 발급)
    export DART_API_KEY=발급받은키
    python scripts/dart_adjusted_price_events.py --since 20230101

    # 2) OpenDART가 차단된 환경: DART 본사이트 검색 폴백
    python scripts/dart_adjusted_price_events.py --mode dartsite --since 20230101

출력: output/adjusted_price_events.csv (utf-8-sig, 엑셀에서 바로 열림)

주의:
  - 유상증자는 주주배정·일반공모 방식만 권리락이 발생해 수정주가 조정
    대상이다. 제3자배정은 권리락이 없으므로 결과에서 '비고' 열을 보고
    걸러야 한다(공시 제목만으로는 방식 구분 불가).
  - 현금배당은 KRX 수정주가 관행상 조정하지 않으므로 수집하지 않는다.
"""

import argparse
import csv
import datetime as dt
import os
import re
import sys
import time
import urllib.parse
import urllib.request

OPENDART_LIST = "https://opendart.fss.or.kr/api/list.json"
DARTSITE_SEARCH = "https://dart.fss.or.kr/dsab007/detailSearch.ax"
VIEWER = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcpno}"

# 보고서명 키워드 -> 이벤트 유형. 순서대로 첫 매칭을 채택한다.
EVENT_PATTERNS = [
    ("유무상증자", "무상증자+유상증자"),
    ("무상증자결정", "무상증자"),
    ("유상증자결정", "유상증자"),
    ("주식분할결정", "액면분할"),
    ("주식병합결정", "액면병합"),
    ("감자결정", "감자"),
    ("주식배당결정", "주식배당"),
]
MARKET = {"Y": "유가증권", "K": "코스닥", "N": "코넥스", "E": "기타"}


def classify(report_nm):
    for kw, label in EVENT_PATTERNS:
        if kw in report_nm:
            return label
    return None


def http_get(url, params, timeout=30):
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def http_post(url, data, timeout=30):
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": "https://dart.fss.or.kr/dsab007/main.do",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def date_chunks(since, until, days=89):
    cur = since
    while cur <= until:
        end = min(cur + dt.timedelta(days=days), until)
        yield cur, end
        cur = end + dt.timedelta(days=1)


def fetch_opendart(api_key, since, until, markets):
    import json

    rows = []
    for beg, end in date_chunks(since, until):
        for corp_cls in markets:
            page = 1
            while True:
                params = {
                    "crtfc_key": api_key,
                    "bgn_de": beg.strftime("%Y%m%d"),
                    "end_de": end.strftime("%Y%m%d"),
                    "pblntf_ty": "B",  # 주요사항보고
                    "corp_cls": corp_cls,
                    "page_no": page,
                    "page_count": 100,
                }
                data = json.loads(http_get(OPENDART_LIST, params))
                status = data.get("status")
                if status == "013":  # 조회 결과 없음
                    break
                if status != "000":
                    raise RuntimeError(f"OpenDART 오류 {status}: {data.get('message')}")
                for it in data.get("list", []):
                    label = classify(it.get("report_nm", ""))
                    if not label:
                        continue
                    rows.append({
                        "공시일": it["rcept_dt"],
                        "시장": MARKET.get(it.get("corp_cls", ""), it.get("corp_cls", "")),
                        "회사명": it["corp_name"],
                        "종목코드": it.get("stock_code", ""),
                        "이벤트": label,
                        "보고서명": it["report_nm"],
                        "정정여부": "정정" if "정정" in it["report_nm"] else "",
                        "접수번호": it["rcept_no"],
                        "링크": VIEWER.format(rcpno=it["rcept_no"]),
                        "비고": "유상증자는 3자배정이면 조정 불요" if label == "유상증자" else "",
                    })
                if page >= int(data.get("total_page", 1)):
                    break
                page += 1
                time.sleep(0.2)
    return rows


def fetch_dartsite(since, until, markets):
    """dart.fss.or.kr 본사이트 상세검색(dsab007) 폴백.

    비공식 엔드포인트라 파라미터가 바뀔 수 있다. 실패 시 응답 일부를
    출력하므로 브라우저 개발자도구의 실제 요청과 대조해 수정한다.
    """
    rows = []
    kw_list = [kw for kw, _ in EVENT_PATTERNS if kw != "유무상증자"]
    row_re = re.compile(
        r"openReportViewer\('(?P<rcpno>\d{14})'.*?>(?P<title>[^<]+)</a>",
        re.S,
    )
    for beg, end in date_chunks(since, until):
        for kw in kw_list:
            page = 1
            while True:
                data = {
                    "currentPage": page,
                    "maxResults": 100,
                    "startDate": beg.strftime("%Y%m%d"),
                    "endDate": end.strftime("%Y%m%d"),
                    "reportName": kw,
                    "reportNamePopYn": "N",
                    "corporationType": "all",
                    "finalReport": "recent",
                }
                try:
                    html = http_post(DARTSITE_SEARCH, data)
                except Exception as e:  # noqa: BLE001
                    print(f"[경고] dartsite 요청 실패({kw} {beg}~{end}): {e}", file=sys.stderr)
                    break
                found = row_re.findall(html)
                if not found:
                    break
                for rcpno, title in found:
                    title = title.strip()
                    label = classify(title) or classify(kw) or kw
                    rows.append({
                        "공시일": rcpno[:8],
                        "시장": "",
                        "회사명": "",  # HTML 구조상 회사명은 후처리 필요
                        "종목코드": "",
                        "이벤트": label,
                        "보고서명": title,
                        "정정여부": "정정" if "정정" in title else "",
                        "접수번호": rcpno,
                        "링크": VIEWER.format(rcpno=rcpno),
                        "비고": "dartsite 폴백: 회사명은 링크에서 확인",
                    })
                if len(found) < 100:
                    break
                page += 1
                time.sleep(0.3)
    return rows


def main():
    ap = argparse.ArgumentParser(description="DART 수정주가 조정 사유 공시 추출")
    ap.add_argument("--since", default="20230101", help="시작일 YYYYMMDD (기본 20230101)")
    ap.add_argument("--until", default=dt.date.today().strftime("%Y%m%d"), help="종료일 YYYYMMDD")
    ap.add_argument("--mode", choices=["opendart", "dartsite"], default="opendart")
    ap.add_argument("--api-key", default=os.environ.get("DART_API_KEY", ""))
    ap.add_argument("--markets", default="Y,K", help="시장구분 corp_cls (Y=유가,K=코스닥,N=코넥스)")
    ap.add_argument("--out", default="output/adjusted_price_events.csv")
    args = ap.parse_args()

    since = dt.datetime.strptime(args.since, "%Y%m%d").date()
    until = dt.datetime.strptime(args.until, "%Y%m%d").date()
    markets = [m.strip().upper() for m in args.markets.split(",") if m.strip()]

    if args.mode == "opendart":
        if not args.api_key:
            sys.exit("DART_API_KEY 환경변수 또는 --api-key가 필요합니다. "
                     "(무료 발급: https://opendart.fss.or.kr) "
                     "OpenDART가 차단된 환경이면 --mode dartsite 사용.")
        rows = fetch_opendart(args.api_key, since, until, markets)
    else:
        rows = fetch_dartsite(since, until, markets)

    # 접수번호 기준 중복 제거 후 공시일 순 정렬
    rows = list({r["접수번호"]: r for r in rows}.values())
    rows.sort(key=lambda r: (r["공시일"], r["회사명"]))

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    fields = ["공시일", "시장", "회사명", "종목코드", "이벤트", "보고서명",
              "정정여부", "접수번호", "링크", "비고"]
    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    by_event = {}
    for r in rows:
        by_event[r["이벤트"]] = by_event.get(r["이벤트"], 0) + 1
    print(f"총 {len(rows)}건 -> {args.out}")
    for k, v in sorted(by_event.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}건")


if __name__ == "__main__":
    main()
