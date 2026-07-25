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
    args = parser.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    end_date = args.date or datetime.now(KST).strftime("%Y%m%d")

    # 1. 정량 스크리닝
    print("== 1/5 스크리닝 ==")
    screen_result = screener.screen(end_date, cfg["screener"])
    base_date = screen_result["base_date"]
    candidates = screen_result["candidates"]

    # 2. 뉴스·공시 수집
    print("== 2/5 근거 수집 ==")
    evidence = collect_mod.collect(candidates, base_date, cfg["collect"]) if candidates else {}

    # 3. 원인 분석 + 파급 추론
    print("== 3/5 원인 분석 ==")
    if args.skip_analyze or not candidates:
        analysis = {"base_date": base_date, "analyses": [], "synthesis": None}
    else:
        analysis = analyze_mod.analyze(candidates, evidence, base_date, cfg["analyze"])

    # 4. 리포트 생성 + 케이스 축적
    print("== 4/5 리포트 생성 ==")
    report_mod.build(base_date, candidates, analysis, cfg["report"])
    case_file = ROOT / "cases" / f"{base_date}.json"
    case_file.parent.mkdir(exist_ok=True)
    case_file.write_text(json.dumps({
        "base_date": base_date,
        "candidates": candidates,
        "analyses": analysis.get("analyses", []),
        "synthesis": analysis.get("synthesis"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # 5. 텔레그램 알림
    print("== 5/5 알림 ==")
    if not args.skip_notify:
        pages_url = os.getenv("PAGES_URL")
        if pages_url:
            pages_url = pages_url.rstrip("/") + f"/{base_date}.html"
        text = report_mod.render_telegram(base_date, candidates, analysis, pages_url)
        notify_mod.send_telegram(text)

    print("완료.")


if __name__ == "__main__":
    main()
