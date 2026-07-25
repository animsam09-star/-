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

import yaml

from . import analyze as analyze_mod
from . import collect as collect_mod
from . import factors as factors_mod
from . import groups as groups_mod
from . import notify as notify_mod
from . import report as report_mod
from . import screener

ROOT = Path(__file__).resolve().parent.parent
KST = timezone(timedelta(hours=9))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="기준일 YYYYMMDD (기본: 오늘 KST)")
    parser.add_argument("--skip-analyze", action="store_true", help="LLM 분석 생략")
    parser.add_argument("--skip-notify", action="store_true", help="텔레그램 알림 생략")
    parser.add_argument("--skip-horizontal", action="store_true", help="수평 그래프 생략")
    args = parser.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    end_date = args.date or datetime.now(KST).strftime("%Y%m%d")

    # 1. 정량 스크리닝
    print("== 1/6 스크리닝 ==")
    screen_result, close = screener.screen(end_date, cfg["screener"])
    base_date = screen_result["base_date"]
    candidates = screen_result["candidates"]

    # 2. 수평 그래프 (섹터 초월 그룹 + 매크로 팩터 노출 + 미반영 갭)
    print("== 2/6 수평 그래프 ==")
    hcfg = cfg["horizontal"]
    horizontal: dict = {}
    if hcfg.get("enabled") and not args.skip_horizontal and not close.empty:
        try:
            grp = groups_mod.build_groups(base_date, universe=set(close.columns))
            group_ret = groups_mod.group_returns(close, grp["groups"])
            fac = factors_mod.build(close, group_ret, grp["groups"], base_date, hcfg)
            horizontal = {**fac, "membership": grp["membership"], "groups": grp["groups"]}
        except Exception as e:
            print(f"수평 그래프 생성 실패(건너뜀): {e}")
    else:
        print("건너뜀")

    # 3. 뉴스·공시 수집
    print("== 3/6 근거 수집 ==")
    evidence = collect_mod.collect(candidates, base_date, cfg["collect"]) if candidates else {}

    # 4. 원인 분석 + 파급 추론
    print("== 4/6 원인 분석 ==")
    acfg = {**cfg["analyze"], "max_peers_per_group": hcfg.get("max_peers_per_group", 6)}
    if args.skip_analyze or not candidates:
        analysis = {"base_date": base_date, "analyses": [], "synthesis": None}
    else:
        analysis = analyze_mod.analyze(candidates, evidence, base_date, acfg, horizontal)

    # 5. 리포트 생성 + 케이스 축적
    print("== 5/6 리포트 생성 ==")
    report_mod.build(base_date, candidates, analysis, cfg["report"], horizontal)
    case_file = ROOT / "cases" / f"{base_date}.json"
    case_file.parent.mkdir(exist_ok=True)
    case_file.write_text(json.dumps({
        "base_date": base_date,
        "candidates": candidates,
        "analyses": analysis.get("analyses", []),
        "synthesis": analysis.get("synthesis"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # 6. 텔레그램 알림
    print("== 6/6 알림 ==")
    if not args.skip_notify:
        pages_url = os.getenv("PAGES_URL")
        if pages_url:
            pages_url = pages_url.rstrip("/") + f"/{base_date}.html"
        text = report_mod.render_telegram(base_date, candidates, analysis, pages_url)
        notify_mod.send_telegram(text)

    print("완료.")


if __name__ == "__main__":
    main()
