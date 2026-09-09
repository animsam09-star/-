#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DART 정기보고서 수주현황 데이터센터 전수조사 스크립트
=====================================================

목적
----
지정한 건설사들의 DART 정기보고서(분기·반기·사업보고서, 정정본 포함)를
2017년 1분기부터 2022년 4분기까지 모두 내려받아, '수주' 관련 표(수주상황·
건설계약 수주현황 등)의 모든 행을 읽고 데이터센터 관련 키워드가 들어간 행을
CSV로 뽑아낸다. 사람이 목록을 눈으로 훑는 대신 기계가 전부 훑게 하려는 것이다.

주의
----
* 이 스크립트는 dart.fss.or.kr 에 직접 접속한다. 접속이 차단된 환경(예: 회사
  프록시, 제한된 클라우드 샌드박스)에서는 동작하지 않는다.
* 작성 시점의 실행 환경에서는 dart.fss.or.kr 접속이 네트워크 정책으로 차단되어
  실제 DART 응답으로 검증하지 못했다. 파싱 함수는 동일 저장소의 dart_lib.py
  (기존에 DART 문서 파싱에 사용된 라이브러리)를 그대로 쓴다.
* 키워드 필터는 일부러 넓게 잡았다(예: 'IT센터', '전산센터', 'DC'). 결과 CSV의
  match_level 열이 'strong'이면 데이터센터일 가능성이 높고, 'weak'이면 사람이
  확인해야 한다.

사용법
------
    python3 tools/dart_dc_census.py --out out/ [--y0 2017 --y1 2023] [--company 현대건설]

결과
----
    out/reports.csv   : 회사별·기간별로 사용한 보고서 목록(rcpNo, DART URL)
    out/hits.csv      : 키워드가 걸린 표 행 전부
    out/missing.csv   : 보고서를 찾지 못했거나 수주 표를 찾지 못한 기간

의존성: 표준 라이브러리만 사용 (urllib, re, csv, html).
"""
import argparse
import csv
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dart_lib as D  # noqa: E402

# ---------------------------------------------------------------------------
# 1. 조사 대상 회사
#    DART 검색창에 넣는 회사명은 '현재 이름'이어야 옛 보고서까지 나온다
#    (같은 법인이면 이름이 바뀌어도 한 회사로 검색된다).
#    법인이 다른 전신(예: 대림산업 → DL이앤씨는 인적분할이라 법인이 다름)은
#    따로 검색해야 하므로 여러 이름을 넣는다.
# ---------------------------------------------------------------------------
COMPANIES = {
    '삼성물산':   ['삼성물산'],
    '현대건설':   ['현대건설'],
    'GS건설':     ['GS건설'],
    # 대림산업(000210)은 2021.1 인적분할 후 'DL'(지주)로 남고 건설은 DL이앤씨(375500)로 신설.
    # 2017~2020 보고서는 'DL'(구 대림산업) 법인에서 검색해야 나온다.
    'DL이앤씨':   ['DL이앤씨', 'DL'],
    # 삼호(001880)가 고려개발을 흡수(2020.7)해 대림건설 → DL건설. 같은 법인이므로 현재 이름으로 검색.
    # 고려개발(2017~2020.6)은 별도 법인이라 따로 검색한다.
    'DL건설':     ['DL건설', '고려개발'],
    '롯데건설':   ['롯데건설'],
    '포스코이앤씨': ['포스코이앤씨'],
    'SK에코플랜트': ['SK에코플랜트'],
}

# ---------------------------------------------------------------------------
# 2. 키워드
#    strong : 걸리면 거의 확실히 데이터센터 관련
#    weak   : 데이터센터가 아닌 것도 섞이므로 사람이 확인
# ---------------------------------------------------------------------------
STRONG = [
    '데이터센터', '데이타센터', '데이터센타', '데이타센타', 'datacenter', 'data center',
    'idc', 'hpc', '전산센터', '전산센타', 'it센터', '통합it', '통합전산',
    '정보자원관리원', '통합데이터', '클라우드센터', '클라우드 센터', '슈퍼컴',
    '컴퓨팅센터', '서버', '디지털리얼티', 'digital realty', '에퀴닉스', 'equinix',
    'stt', '디지털엣지', 'digital edge', '캐피탈랜드', '퍼시픽써니', '액티스', 'actis',
    '가산아이윌', 'kt클라우드', '마이크로소프트', 'microsoft', '이지스자산운용',
    '혁신센터',            # 대구은행 DGB혁신센터(=DGB금융 차세대 데이터센터)
    '데이터베이스센터',
]
WEAK = [
    'dc', '센터', 'center', '클라우드', 'cloud', '전산', '통신센터', '정보센터',
    '네이버', '카카오', '삼성sds', 'sds', '삼성전자', '삼성디스플레이', 'lg cns', 'lgcns',
    '국민은행', '기업은행', '하나', '우리은행', '신한', '농협', 'nh', 'kb', 'ibk', 'dgb', '대구은행',
    '롯데정보통신', '현대정보기술', '현대정보통신', '다우기술', 'kt', '다우',
]

# 수주 표가 들어 있을 만한 문서 노드 이름
NODE_HINTS = ['수주', '사업의 내용', '건설계약', '매출 및 수주', '영업의 현황', '영업의 개황',
              '주요 계약', '진행중인 공사', '공사현황', '진행 중인 공사']


def norm_text(s):
    return re.sub(r'\s+', '', str(s or '')).lower()


def classify(text):
    t = norm_text(text)
    for k in STRONG:
        if norm_text(k) in t:
            return 'strong', k
    for k in WEAK:
        kk = norm_text(k)
        if kk in ('dc', 'kt', 'nh', 'kb', 'sds', 'stt', 'ibk', 'dgb'):
            # 짧은 영문 약어는 앞뒤가 영문이 아닐 때만 인정 (예: 'DC'가 'DCM' 안에 걸리는 것 방지)
            if re.search(r'(?<![a-z])' + re.escape(kk) + r'(?![a-z])', t):
                return 'weak', k
        elif kk in t:
            return 'weak', k
    return None, None


def period_of(name):
    """'분기보고서 (2018.03)' -> (2018, 1). 사업보고서(2018.12) -> (2018, 4)."""
    m = re.search(r'\((\d{4})\.(\d{2})\)', name)
    if not m:
        return None
    q = {'03': 1, '06': 2, '09': 3, '12': 4}.get(m.group(2))
    return (int(m.group(1)), q) if q else None


def all_reports(names, y0, y1):
    """이름 여러 개로 검색해 {rcpNo: 보고서명} 합집합. 원본·정정본 모두 남긴다."""
    out = {}
    for nm in names:
        try:
            out.update(D.search_reports(nm, y0, y1))
        except Exception as e:  # 네트워크 차단 등
            print(f'   [경고] {nm} 검색 실패: {e}', file=sys.stderr)
        time.sleep(0.5)
    return out


def pick_nodes(nodes):
    """수주 표가 들어 있을 만한 노드. 없으면 전체 노드(비용은 크지만 누락 방지)."""
    sel = [n for n in nodes if any(h in n['text'] for h in NODE_HINTS)]
    return sel if sel else nodes


def scan_report(rcp, name, company, writer_hits):
    nodes = D.get_nodes(rcp)
    if not nodes:
        return 0, 'no_nodes'
    targets = pick_nodes(nodes)
    n_hits = 0
    seen = set()
    for n in targets:
        key = (n['eleId'], n['offset'])
        if key in seen:
            continue
        seen.add(key)
        try:
            html_ = D.fetch_section(n)
        except Exception as e:
            print(f'   [경고] {company} {rcp} {n["text"]} 수신 실패: {e}', file=sys.stderr)
            continue
        tables = D.tables_text(html_)
        for ti, tbl in enumerate(tables):
            for ri, row in enumerate(tbl):
                joined = ' | '.join(row)
                level, kw = classify(joined)
                if level:
                    n_hits += 1
                    writer_hits.writerow({
                        'company': company, 'period': '%dQ%d' % period_of(name) if period_of(name) else '',
                        'report': name, 'rcpNo': rcp, 'dart_url': D.dart_url(rcp),
                        'node': n['text'], 'table_idx': ti, 'row_idx': ri,
                        'match_level': level, 'keyword': kw, 'row': joined,
                    })
        # 표 밖 본문(주석·설명문)에서도 strong 키워드가 있으면 남긴다 (표를 못 잡은 경우 대비)
        plain = D.plain(html_)
        for k in STRONG[:8]:
            if norm_text(k) in norm_text(plain):
                writer_hits.writerow({
                    'company': company, 'period': '%dQ%d' % period_of(name) if period_of(name) else '',
                    'report': name, 'rcpNo': rcp, 'dart_url': D.dart_url(rcp),
                    'node': n['text'], 'table_idx': -1, 'row_idx': -1,
                    'match_level': 'text', 'keyword': k, 'row': '(표 밖 본문에 키워드 존재)',
                })
                break
    return n_hits, 'ok'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='out')
    ap.add_argument('--y0', type=int, default=2017)
    ap.add_argument('--y1', type=int, default=2023)   # 2022 사업보고서는 2023.3 제출
    ap.add_argument('--company', default=None, help='특정 회사만')
    ap.add_argument('--first', default='2017Q1')
    ap.add_argument('--last', default='2022Q4')
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    def pkey(s):
        y, q = s.upper().split('Q')
        return (int(y), int(q))
    lo, hi = pkey(args.first), pkey(args.last)

    f_rep = open(os.path.join(args.out, 'reports.csv'), 'w', newline='', encoding='utf-8-sig')
    f_hit = open(os.path.join(args.out, 'hits.csv'), 'w', newline='', encoding='utf-8-sig')
    f_mis = open(os.path.join(args.out, 'missing.csv'), 'w', newline='', encoding='utf-8-sig')
    w_rep = csv.DictWriter(f_rep, fieldnames=['company', 'period', 'report', 'rcpNo', 'dart_url', 'hits', 'status'])
    w_hit = csv.DictWriter(f_hit, fieldnames=['company', 'period', 'report', 'rcpNo', 'dart_url', 'node',
                                              'table_idx', 'row_idx', 'match_level', 'keyword', 'row'])
    w_mis = csv.DictWriter(f_mis, fieldnames=['company', 'period', 'reason'])
    for w in (w_rep, w_hit, w_mis):
        w.writeheader()

    for company, names in COMPANIES.items():
        if args.company and company != args.company:
            continue
        print(f'== {company} ({", ".join(names)})')
        reps = all_reports(names, args.y0, args.y1)
        by_period = {}
        for rcp, nm in reps.items():
            if not any(k in nm for k in ('분기보고서', '반기보고서', '사업보고서')):
                continue
            p = period_of(nm)
            if not p or not (lo <= p <= hi):
                continue
            by_period.setdefault(p, []).append((rcp, nm))
        # 기간별 결측 점검
        y, q = lo
        while (y, q) <= hi:
            if (y, q) not in by_period:
                w_mis.writerow({'company': company, 'period': f'{y}Q{q}', 'reason': '보고서 검색 결과 없음'})
            q += 1
            if q == 5:
                y, q = y + 1, 1
        for p in sorted(by_period):
            for rcp, nm in sorted(by_period[p]):
                hits, status = scan_report(rcp, nm, company, w_hit)
                w_rep.writerow({'company': company, 'period': f'{p[0]}Q{p[1]}', 'report': nm, 'rcpNo': rcp,
                                'dart_url': D.dart_url(rcp), 'hits': hits, 'status': status})
                print(f'   {p[0]}Q{p[1]} {nm} {rcp} hits={hits}')
                f_hit.flush(); f_rep.flush()
                time.sleep(0.3)
    for f in (f_rep, f_hit, f_mis):
        f.close()
    print('완료:', args.out)


if __name__ == '__main__':
    main()
