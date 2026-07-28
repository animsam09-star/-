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
from . import factors as factors_mod
from . import graph_build
from . import groups as groups_mod
from . import notify as notify_mod
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
    print("== 1/7 스크리닝 ==")
    screen_result, close = screener.screen(end_date, cfg["screener"])
    base_date = screen_result["base_date"]
    candidates = screen_result["candidates"]

    # 2. 종목 마스터 (Layer 0) — 스크리닝 필터와 독립적인 전 종목 인덱스.
    #    이름→티커 해석의 단일 출처라 그래프보다 먼저 서야 한다.
    print("== 2/7 종목 마스터 ==")
    uni = universe_mod.build(base_date)

    # 3. 수평 그래프 (섹터 초월 그룹 + 매크로 팩터 노출 + 미반영 갭)
    print("== 3/7 수평 그래프 ==")
    hcfg = cfg["horizontal"]
    horizontal: dict = {}
    if hcfg.get("enabled") and not args.skip_horizontal and not close.empty:
        try:
            grp = groups_mod.build_groups(base_date, universe=set(close.columns))
            group_ret = groups_mod.group_returns(close, grp["groups"])
            # 시장 요인을 시총가중으로 만들려면 가중치가 필요하다. 종목 마스터에
            # 이미 있으므로 추가 조회는 없다.
            caps = pd.Series({t: e["market_cap"] for t, e in uni.entries.items()
                              if e.get("market_cap")}, dtype="float64")
            fac = factors_mod.build(close, group_ret, grp["groups"], base_date, hcfg,
                                    market_caps=caps)
            horizontal = {**fac, "membership": grp["membership"], "groups": grp["groups"]}
        except Exception as e:
            print(f"수평 그래프 생성 실패(건너뜀): {e}")
    else:
        print("건너뜀")

    # 4. 관계 그래프 (Layer 1) — 수직축(영속)과 수평축(일자별)이 여기서 하나가 된다.
    print("== 4/7 관계 그래프 ==")
    persistent, vc_report = graph_build.build_persistent(uni, base_date)
    daily = graph_build.build_daily(horizontal, base_date)
    relation_graph = graph_build.assemble(persistent, daily)
    print(f"통합 그래프 엣지 {len(relation_graph)}개")

    # 5. 뉴스·공시 수집
    print("== 5/7 근거 수집 ==")
    evidence = collect_mod.collect(candidates, base_date, cfg["collect"]) if candidates else {}

    # 6. 원인 분석 + 파급 추론
    print("== 6/7 원인 분석 ==")
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

    # 7. 리포트 생성 + 케이스 축적
    print("== 7/7 리포트 생성 ==")
    report_mod.build(base_date, candidates, analysis, cfg["report"], horizontal)
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
