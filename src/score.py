"""사후 검증: 과거에 내놓은 예측이 실제로 맞았는지 채점한다.

단위 테스트가 '코드가 의도대로 도는가'를 보고, 스모크 테스트가 '실제 데이터로 도는가'를
본다면, 이 모듈은 '예측이 쓸모 있었는가'를 본다. 이게 이 시스템의 진짜 검증이다.

채점 대상
- 미반영으로 꼽은 수혜 후보가 이후 실제로 올랐는가 (기반영 후보 대비)
- 갭이 클수록 이후 수익률이 높았는가 (갭 지표 자체의 유효성)
- 수직축과 수평축 중 어느 쪽 예측이 더 맞았는가

cases/*.json이 며칠 이상 쌓여야 의미 있는 수치가 나온다.
"""

from __future__ import annotations

import argparse
import json
import statistics
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from pykrx import stock

ROOT = Path(__file__).resolve().parent.parent
CASES_DIR = ROOT / "cases"
REPORTS_DIR = ROOT / "reports"


def _forward_return(ticker: str, from_date: str, horizon: int) -> float | None:
    """from_date 종가 대비 정확히 horizon 거래일 뒤 종가 수익률.

    거래일이 부족하면 None을 돌려준다. 더 짧은 기간 수익률을 horizon일 수익률로
    라벨링하면 채점 결과가 조용히 편향되기 때문에, 짧은 구간은 채점에서 제외한다.
    """
    end = (datetime.strptime(from_date, "%Y%m%d")
           + timedelta(days=int(horizon * 1.7) + 14)).strftime("%Y%m%d")
    try:
        df = stock.get_market_ohlcv_by_date(from_date, end, ticker)
    except Exception:
        return None
    if df is None or df.empty or "종가" not in df.columns:
        return None

    closes = df["종가"][df["종가"] > 0]
    if len(closes) <= horizon:
        return None  # 거래일 부족(정지 포함) — 채점 불가
    base = float(closes.iloc[0])
    if base <= 0:
        return None
    return float(closes.iloc[horizon]) / base - 1


def collect_predictions(min_age_days: int) -> list[dict]:
    """채점 가능한 시점(예측 후 horizon 거래일 경과)의 예측만 모은다."""
    cutoff = datetime.now() - timedelta(days=min_age_days)
    rows: list[dict] = []

    for f in sorted(CASES_DIR.glob("2*.json")):
        base_date = f.stem
        try:
            if datetime.strptime(base_date, "%Y%m%d") > cutoff:
                continue
            case = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, json.JSONDecodeError):
            continue

        synthesis = case.get("synthesis") or {}
        for idea in synthesis.get("ideas", []):
            for b in idea.get("beneficiaries", []):
                rows.append({
                    "base_date": base_date,
                    "idea": idea.get("title"),
                    "axis": idea.get("axis"),
                    "name": b.get("name"),
                    "ticker": b.get("ticker"),
                    "impact": b.get("impact", "수혜"),
                    "priced_in": b.get("priced_in"),
                    "gap": b.get("gap"),
                    "graph_backed": b.get("graph_backed"),
                    "via": b.get("via"),
                })
    return [r for r in rows if r["ticker"]]


def _by_path(scored: list[dict], summarize, min_n: int = 3) -> dict:
    """경로별 성과. '건설·EPC→후방→시멘트'가 몇 번 맞았는지 누적하면
    밸류체인 맵이 고정 지식이 아니라 신뢰도가 갱신되는 자산이 된다.

    표본이 min_n 미만인 경로는 버린다 — 1~2건짜리 적중률은 잡음이다.
    """
    buckets: dict[str, list[dict]] = {}
    for r in scored:
        if r.get("via"):
            buckets.setdefault(r["via"], []).append(r)
    out = {k: summarize(v) for k, v in buckets.items() if len(v) >= min_n}
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["n"]))


def score(horizon: int = 10, min_age_days: int | None = None) -> dict:
    # horizon 거래일이 실제로 지나야 채점할 수 있다(주말·공휴일 감안 여유 포함)
    if min_age_days is None:
        min_age_days = int(horizon * 1.7) + 7
    preds = collect_predictions(min_age_days)
    if not preds:
        print("채점할 예측이 없습니다. cases/에 데이터가 더 쌓여야 합니다.")
        return {"n": 0}

    print(f"예측 {len(preds)}건 채점 중 (보유기간 {horizon}거래일)...")
    for p in preds:
        p["forward_return"] = _forward_return(p["ticker"], p["base_date"], horizon)
    scored = [p for p in preds if p["forward_return"] is not None]
    if not scored:
        print("수익률을 조회하지 못했습니다.")
        return {"n": 0}

    def summarize(rows: list[dict]) -> dict | None:
        if not rows:
            return None
        rets = [r["forward_return"] for r in rows]
        return {
            "n": len(rows),
            "mean": round(statistics.fmean(rets), 4),
            "median": round(statistics.median(rets), 4),
            "hit_rate": round(sum(r > 0 for r in rets) / len(rets), 3),
        }

    # 부호 판정이 맞았는지: '피해'로 꼽은 종목은 하락해야 맞는 예측이다
    def directional_hit(r: dict) -> bool:
        return r["forward_return"] > 0 if r["impact"] == "수혜" else r["forward_return"] < 0

    result = {
        "horizon": horizon,
        "n": len(scored),
        "overall": summarize(scored),
        "by_priced_in": {k: summarize([r for r in scored if r["priced_in"] == k])
                         for k in ("미반영", "일부반영", "기반영", "확인불가")},
        "by_axis": {k: summarize([r for r in scored if r["axis"] == k])
                    for k in ("수직", "수평")},
        "by_impact": {k: summarize([r for r in scored if r["impact"] == k])
                      for k in ("수혜", "피해")},
        "directional_hit_rate": round(
            sum(directional_hit(r) for r in scored) / len(scored), 3),
        # 밸류체인 맵이 실제로 값을 하는가 — 그래프에서 나온 후보와 LLM이 새로
        # 만든 후보를 갈라 본다. 이 비교가 수직축 투자에 대한 유일한 정직한 답이다.
        "by_provenance": {
            "그래프기반": summarize([r for r in scored if r.get("graph_backed") is True]),
            "LLM창작": summarize([r for r in scored if r.get("graph_backed") is False]),
        },
        "by_path": _by_path(scored, summarize),
    }

    # 갭 지표 자체가 유효한지: 갭과 이후 수익률의 상관
    with_gap = [r for r in scored if r.get("gap") is not None]
    if len(with_gap) >= 5:
        s = pd.DataFrame(with_gap)
        result["gap_correlation"] = round(float(s["gap"].corr(s["forward_return"])), 3)
        result["gap_top_quartile"] = summarize(
            s.nlargest(max(1, len(s) // 4), "gap").to_dict("records"))

    result["_scored"] = scored
    return result


def render(result: dict) -> str:
    if not result.get("n"):
        return "채점 가능한 예측이 없습니다."

    def line(label, s):
        if not s:
            return f"  {label}: -"
        return (f"  {label}: n={s['n']} 평균 {s['mean']:+.1%} "
                f"중앙 {s['median']:+.1%} 상승비율 {s['hit_rate']:.0%}")

    out = [f"=== 예측 성과 ({result['horizon']}거래일 보유, {result['n']}건) ===",
           line("전체", result["overall"]),
           "",
           "[미반영 판정별]"]
    out += [line(k, v) for k, v in result["by_priced_in"].items() if v]
    out += ["", "[파급 축별]"]
    out += [line(k, v) for k, v in result["by_axis"].items() if v]
    out += ["", "[부호별]"]
    out += [line(k, v) for k, v in result["by_impact"].items() if v]
    out += ["", "[후보 출처별]"]
    out += [line(k, v) for k, v in result["by_provenance"].items() if v]
    out += ["  (그래프기반이 LLM창작보다 낫지 않다면 밸류체인 맵은 값을 못 하고 있다)"]

    if result.get("by_path"):
        out += ["", "[파급 경로별]"]
        out += [line(k, v) for k, v in result["by_path"].items()]

    out += ["", f"방향 적중률(수혜↑·피해↓): {result['directional_hit_rate']:.0%}"]

    if "gap_correlation" in result:
        out += ["", f"갭 ↔ 이후수익률 상관: {result['gap_correlation']:+.2f}",
                line("갭 상위 25%", result.get("gap_top_quartile"))]
        out.append("  (상관이 양수면 갭 지표가 실제로 작동한다는 뜻)")
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description="과거 예측의 실제 성과를 채점한다.")
    parser.add_argument("--horizon", type=int, default=10, help="보유 거래일 수")
    parser.add_argument("--min-age-days", type=int, default=None,
                        help="이 일수 이상 지난 예측만 채점 (기본: horizon에서 자동 산출)")
    args = parser.parse_args()

    result = score(args.horizon, args.min_age_days)
    print(render(result))

    if result.get("n"):
        result.pop("_scored", None)
        out = REPORTS_DIR / "score.json"
        REPORTS_DIR.mkdir(exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n저장: {out}")


if __name__ == "__main__":
    main()
