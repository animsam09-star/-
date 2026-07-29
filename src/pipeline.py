"""전체 파이프라인 오케스트레이터.

사용법:
  python -m src.pipeline                 # 오늘 기준 전체 실행
  python -m src.pipeline --date 20260724 # 특정일 기준
  python -m src.pipeline --skip-analyze  # LLM 분석 생략 (스크리닝+리포트만)
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yaml

from . import analyze as analyze_mod
from . import collect as collect_mod
from . import corr
from . import factors as factors_mod
from . import graph_build
from . import groups as groups_mod
from . import notify as notify_mod
from . import prices
from . import report as report_mod
from . import screener
from . import universe as universe_mod

ROOT = Path(__file__).resolve().parent.parent
KST = timezone(timedelta(hours=9))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="기준일 YYYYMMDD (기본: 오늘 KST)")
    parser.add_argument("--skip-analyze", action="store_true", help="LLM 분석 생략")
    parser.add_argument("--skip-notify", action="store_true", help="텔레그램 알림 생략")
    parser.add_argument("--skip-horizontal", action="store_true", help="수평 그래프 생략")
    parser.add_argument("--smoke", action="store_true",
                        help="스모크 모드: 조회 기간·후보 수를 줄여 라이브 경로만 빠르게 확인")
    args = parser.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    if args.smoke:
        # 라이브 경로가 뚫리는지만 보는 모드. 베타 구간은 factors 쪽에서 자동 축소된다.
        cfg["screener"]["lookback_days"] = 60
        cfg["screener"]["top_n"] = 5
        cfg["analyze"]["max_candidates"] = 2
        print("스모크 모드: lookback 60일 / 후보 5종목 / 분석 2종목")
    end_date = args.date or datetime.now(KST).strftime("%Y%m%d")

    # 1. 정량 스크리닝
    print("== 1/8 스크리닝 ==")
    screen_result, close = screener.screen(end_date, cfg["screener"])
    base_date = screen_result["base_date"]
    candidates = screen_result["candidates"]

    # 2. 종목 마스터 (Layer 0) — 스크리닝 필터와 독립적인 전 종목 인덱스.
    #    이름→티커 해석의 단일 출처라 그래프보다 먼저 서야 한다.
    print("== 2/8 종목 마스터 ==")
    uni = universe_mod.build(base_date)

    # 3. 수직축(영속 그래프) — 수평축의 그룹이 여기 산업 노드에서 나오므로 먼저 세운다.
    #    저장된 edges.jsonl을 읽게 두면 밸류체인 YAML을 고쳐도 그룹이 한 실행 늦게
    #    반영된다. 이번 실행의 소속 엣지를 그대로 넘긴다.
    print("== 3/8 수직 그래프 ==")
    persistent, vc_report = graph_build.build_persistent(uni, base_date)

    # 시가총액은 여기서 한 번만 만든다. 시장 요인·산업 수익률·팩터가 모두 쓴다.
    caps = pd.Series({t: e["market_cap"] for t, e in uni.entries.items()
                      if e.get("market_cap")}, dtype="float64")

    # 3b. 가격이 그 관계를 뒷받침하는가 — **채점만 한다. 엣지를 만들지 않는다.**
    #     상관으로 엣지를 만들면 같이 움직인 종목만 연결되고, 안 움직인 종목은
    #     그룹에서 빠진다. 미반영 갭이 찾으려던 바로 그 종목이 사라진다.
    #     점수는 저장하지 않는다(build_persistent가 이미 저장을 끝냈다) — 매일
    #     다시 계산되는 값이라 영속 파일을 불릴 이유가 없다.
    price_review: dict = {}
    if not close.empty:
        try:
            members = {name[len("산업:"):]: tickers
                       for name, tickers in groups_mod.fetch_industry_groups(
                           min_members=corr.MIN_MEMBERS, edges=persistent).items()}
            ind_ret = corr.residualize(
                corr.industry_returns(close, caps, members),
                prices.market_series(close, caps))
            persistent = corr.score_edges(persistent, ind_ret)
            # 사이클 시차(월 단위)는 별도 워크플로가 긴 패널로 재서 남긴 값이다.
            # 여기 lookback(130거래일)으로는 6~12개월 시차를 잴 수 없다.
            cycle = corr.load_cycle_lags()
            if cycle:
                persistent = corr.attach_cycle_lags(persistent, cycle)
            price_review = {
                "cycle_lags": cycle,
                "weak": corr.review_queue(persistent),
                "candidates": corr.candidates(ind_ret, persistent),
            }
            scored = sum(1 for e in persistent if "price_corr" in e)
            print(f"  가격 채점 {scored}개 / 근거 약한 엣지 {len(price_review['weak'])}개"
                  f" / 발굴 후보 {len(price_review['candidates'])}개(공시 확인 필요)")
            if cycle:
                print(f"  사이클 시차 {len(cycle)}건 적용 (별도 실측, python -m src.corr)")
            else:
                print("  사이클 시차 표 없음 — lead-lag 워크플로를 돌리면 채워진다")
        except Exception as e:
            print(f"  가격 채점 건너뜀: {e}")

    # 4. 수평 그래프 (섹터 초월 그룹 + 매크로 팩터 노출 + 미반영 갭)
    print("== 4/8 수평 그래프 ==")
    hcfg = cfg["horizontal"]
    horizontal: dict = {}
    if hcfg.get("enabled") and not args.skip_horizontal and not close.empty:
        try:
            grp = groups_mod.build_groups(base_date, universe=set(close.columns),
                                          edges=persistent)
            group_ret = groups_mod.group_returns(close, grp["groups"])
            fac = factors_mod.build(close, group_ret, grp["groups"], base_date, hcfg,
                                    market_caps=caps)
            horizontal = {**fac, "membership": grp["membership"], "groups": grp["groups"]}
        except Exception as e:
            print(f"수평 그래프 생성 실패(건너뜀): {e}")
    else:
        print("건너뜀")

    # 5. 관계 그래프 (Layer 1) — 수직축(영속)과 수평축(일자별)이 여기서 하나가 된다.
    print("== 5/8 관계 그래프 ==")
    daily = graph_build.build_daily(horizontal, base_date)
    relation_graph = graph_build.assemble(persistent, daily)
    print(f"통합 그래프 엣지 {len(relation_graph)}개")

    # 6. 뉴스·공시 수집
    print("== 6/8 근거 수집 ==")
    evidence = collect_mod.collect(candidates, base_date, cfg["collect"]) if candidates else {}

    # 7. 원인 분석 + 파급 추론
    print("== 7/8 원인 분석 ==")
    gcfg = cfg.get("graph") or {}
    acfg = {**cfg["analyze"],
            "max_peers_per_group": hcfg.get("max_peers_per_group", 6),
            "min_edge_confidence": gcfg.get("min_edge_confidence", 0.0),
            "max_members_per_industry": gcfg.get("max_members_per_industry", 8),
            "max_drivers": gcfg.get("max_drivers", 6)}
    if args.skip_analyze or not candidates:
        analysis = {"base_date": base_date, "analyses": [], "synthesis": None}
    else:
        analysis = analyze_mod.analyze(candidates, evidence, base_date, acfg,
                                       horizontal, uni, relation_graph)
    analysis["valuechain_coverage"] = vc_report
    analysis["price_review"] = price_review

    # 8. 리포트 생성 + 케이스 축적
    print("== 8/8 리포트 생성 ==")
    report_mod.build(base_date, candidates, analysis, cfg["report"], horizontal, uni)
    case_file = ROOT / "cases" / f"{base_date}.json"
    case_file.parent.mkdir(exist_ok=True)
    case_file.write_text(json.dumps({
        "base_date": base_date,
        "candidates": candidates,
        "analyses": analysis.get("analyses", []),
        "synthesis": analysis.get("synthesis"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # 알림
    print("== 알림 ==")
    if not args.skip_notify:
        pages_url = os.getenv("PAGES_URL")
        if pages_url:
            pages_url = pages_url.rstrip("/") + f"/{base_date}.html"
        text = report_mod.render_telegram(base_date, candidates, analysis, pages_url)
        notify_mod.send_telegram(text)

    print("완료.")


if __name__ == "__main__":
    main()
