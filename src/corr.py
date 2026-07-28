"""주가 상관으로 밸류체인 엣지를 **채점**한다. 만들지는 않는다.

## 왜 만들지 않는가 — 순환 논리

이 파이프라인이 노리는 것은 '아직 반응하지 않은 종목'(미반영 갭)이다. 그런데
엣지를 상관에서 만들면, 정의상 **같이 움직인 종목만** 연결되고 안 움직인 종목은
그룹에서 빠진다. 찾으려던 바로 그 종목이 사라진다.

구체적으로: compute_group_gaps는 그룹 안에서 아직 안 오른 종목을 찾는다. 그룹
소속이 상관에서 나왔다면, 안 오른 종목은 애초에 그 그룹에 없다. **상관으로 그룹을
만들면 알파가 구조적으로 제거된다.**

그래서 규칙은 하나다. **엣지의 존재는 절대 가격에서 나오지 않는다.** 가격은
가중치를 주고(weight), 시차를 재고(lag), 의심스러운 엣지에 표시를 달 수 있다
(review). 만들 수는 없다.

## 시장 요인을 반드시 뺀다

KOSPI 전체가 시장을 통해 서로 0.5쯤 상관된다. 원시 상관을 쓰면 아무 두 산업이나
'연관 있음'으로 나온다. 시장 조정 후 잔차로만 본다 — factors.compute_exposures가
쓰는 것과 같은 논리다.

## 다중비교

2,765종목이면 쌍이 380만 개다. p<0.01에서도 우연히 3.8만 쌍이 유의하게 나온다.
그래서 발굴 후보(candidates)는 '자동 추가'가 아니라 **공시로 확인할 목록**이고,
관측 수에 맞춘 임계값을 넘긴 것만 내놓는다.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from . import graph as G

ROOT = Path(__file__).resolve().parent.parent

MIN_MEMBERS = 2        # 산업 수익률을 만들 최소 소속 종목 수
MIN_OBS = 60           # 상관을 낼 최소 관측일

# 시차는 **두 축으로 나눠 본다.** 하나로 합칠 수 없다.
#
#  (가) 단기(일간, ~20거래일) — 뉴스가 며칠 안에 옆 종목으로 번지는가.
#       미반영 갭의 타이밍이 여기 걸린다.
#  (나) 장기(월간, ~18개월) — 수주 사이클이 몇 달 뒤 기자재에 붙는가.
#       밸류체인 YAML에 '기자재 6~12개월 후행'이라고 손으로 적힌 그 값이다.
#
# 일간 창을 260일로 늘리는 것으로는 (나)를 못 잡는다. '조선의 t일 수익률이
# 기자재의 t+130일 수익률을 예측하는가'를 묻는 꼴인데, 수주 사이클의 효과는
# 특정 하루에 몰리지 않고 여러 달에 퍼진다. 느린 전파는 **누적 수익률**로 본다.
MAX_LAG = 20                 # 단기: 거래일
TRADING_DAYS_PER_MONTH = 21
MAX_LAG_MONTHS = 18          # 장기: 개월. 6~12개월 관찰치를 여유 있게 덮는다
MIN_OBS_MONTHS = 24          # 월간 관측 최소치 — 이보다 적으면 재지 않는다

# 장기 시차를 재려면 시차(최대 18개월) + 관측(24개월)이 필요하다. 지금 파이프라인의
# lookback(130거래일)으로는 불가능하다. 그래서 장기 추정은 파이프라인에서 매일 돌리지
# 않고 별도 워크플로가 긴 패널로 계산해 파일로 남긴다. 시차는 구조적 상수에 가깝지
# 매일 바뀌는 신호가 아니다.
LONG_PANEL_DAYS = (MAX_LAG_MONTHS + MIN_OBS_MONTHS + 2) * TRADING_DAYS_PER_MONTH

# 이 값을 밑도는 엣지는 '가격이 뒷받침하지 않는다'고 본다. 삭제 기준이 아니라
# 사람이 볼 검토 대상이다 — 신생 관계는 아직 가격에 안 실렸을 수 있다.
WEAK_CORR = 0.15


def industry_returns(close: pd.DataFrame, caps: pd.Series | None,
                     members: dict[str, list[str]],
                     min_members: int = MIN_MEMBERS) -> pd.DataFrame:
    """산업별 시총가중 일간 수익률.

    동일가중을 쓰면 소형주 한 종목의 급등이 산업 전체 움직임으로 잡힌다.
    가중치가 없으면 동일가중으로 떨어지되, 그 사실이 보이도록 컬럼을 남긴다.
    """
    ret = close.pct_change(fill_method=None)
    out: dict[str, pd.Series] = {}
    for name, tickers in (members or {}).items():
        cols = [t for t in tickers if t in ret.columns]
        if len(cols) < min_members:
            continue
        sub = ret[cols]
        if caps is None or caps.reindex(cols).dropna().empty:
            series = sub.mean(axis=1)
        else:
            w = caps.reindex(cols).astype(float)
            w = w.where(w > 0).fillna(0.0)
            if w.sum() <= 0:
                series = sub.mean(axis=1)
            else:
                mask = sub.notna()
                denom = mask.mul(w, axis=1).sum(axis=1)
                num = sub.mul(w, axis=1).sum(axis=1)
                series = (num / denom.where(denom > 0)).astype("float64")
        # 유효 종목이 min_members 미만인 날은 버린다. 남은 한 종목의 등락이
        # 산업 수익률로 둔갑하면 안 된다.
        out[name] = series.where(sub.notna().sum(axis=1) >= min_members)
    return pd.DataFrame(out)


def residualize(ret: pd.DataFrame, market: pd.Series | None) -> pd.DataFrame:
    """시장 요인을 제거한 잔차 수익률.

    이걸 빠뜨리면 무관한 두 산업도 시장을 통해 상관되어 '연관 있음'이 된다.
    """
    if market is None or ret.empty:
        return ret
    m = market.reindex(ret.index).astype("float64")
    if m.notna().sum() < MIN_OBS:
        return ret
    m = m.fillna(0.0)
    var = float((m ** 2).mean() - m.mean() ** 2)
    if var <= 0:
        return ret
    out = {}
    for col in ret.columns:
        y = ret[col]
        ok = y.notna()
        if ok.sum() < MIN_OBS:
            out[col] = y
            continue
        cov = float(((y[ok] - y[ok].mean()) * (m[ok] - m[ok].mean())).mean())
        beta = cov / var
        out[col] = y - beta * m
    return pd.DataFrame(out, index=ret.index)


def lead_lag(a: pd.Series, b: pd.Series, max_lag: int = MAX_LAG,
             min_obs: int = MIN_OBS) -> tuple[int, float, int]:
    """|상관|이 가장 큰 시차와 그때의 상관, 그리고 관측 수.

    반환 lag이 **양수면 a가 b를 선행**한다. 밸류체인에서 실제로 알고 싶은 것이
    이것이다 — 조선 수주가 기자재에 몇 달 뒤 붙는지는 밸류체인 YAML에 사람이
    '6~12개월 후행'이라고 손으로 적어 둔 값인데, 여기서 실측할 수 있다.

    시차는 **관계의 존재가 아니라 타이밍**을 위해 쓴다. 상관이 커서 엣지를
    만드는 게 아니라, 이미 있는 엣지가 언제 전달되는지를 재는 것이다.
    """
    best = (0, 0.0, 0)
    for lag in range(-max_lag, max_lag + 1):
        # lag>0: a를 뒤로 밀어 b의 과거와 맞춘다 → a가 선행
        x, y = (a.shift(lag), b) if lag >= 0 else (a, b.shift(-lag))
        pair = pd.concat([x, y], axis=1).dropna()
        if len(pair) < min_obs:
            continue
        c = pair.iloc[:, 0].corr(pair.iloc[:, 1])
        if pd.isna(c):
            continue
        if abs(c) > abs(best[1]):
            best = (lag, float(c), len(pair))
    return best


def to_monthly(ret: pd.DataFrame | pd.Series,
               days: int = TRADING_DAYS_PER_MONTH) -> pd.DataFrame | pd.Series:
    """일간 수익률 → 겹치지 않는 월간 누적 수익률.

    **겹치는(rolling) 구간을 쓰지 않는 이유** — 겹치면 이웃 관측이 대부분 같은
    날을 공유해 자기상관이 생기고, 유효 표본이 실제보다 훨씬 크게 보인다. 그러면
    우연한 시차가 유의해 보인다. 사이클 시차처럼 표본이 원래 적은 곳에서는
    그 착시가 결론을 바꾼다.

    합으로 근사한다(로그수익률 가정). 월 수익률이 작은 범위라 곱셈과 차이가 미미하고,
    결측이 섞였을 때 동작이 단순하다.
    """
    r = ret.dropna(how="all")
    if len(r) < days:
        return r.iloc[0:0]
    # 최근 구간이 잘리지 않도록 뒤에서부터 묶는다. 최신 관측이 가장 값어치 있다.
    start = len(r) % days
    blocks = r.iloc[start:]
    idx = np.arange(len(blocks)) // days
    grouped = blocks.groupby(idx).sum(min_count=days // 2)
    grouped.index = [blocks.index[min((i + 1) * days - 1, len(blocks) - 1)]
                     for i in range(len(grouped))]
    return grouped


def cycle_lead_lag(a: pd.Series, b: pd.Series,
                   max_lag_months: int = MAX_LAG_MONTHS,
                   min_obs_months: int = MIN_OBS_MONTHS) -> tuple[int, float, int]:
    """월 단위 선행·후행. 반환 lag이 양수면 a가 b를 **개월 수만큼 선행**한다.

    밸류체인 YAML의 '기자재 6~12개월 후행' 같은 손으로 적은 값을 실측으로 바꾼다.
    표본이 월 단위라 원래 적다 — 24개월 미만이면 재지 않고 (0, 0.0, 0)을 돌려준다.
    억지로 내놓은 숫자가 손으로 적은 관찰보다 나쁠 수 있다.
    """
    pair = pd.concat([a, b], axis=1).dropna(how="all")
    if len(pair) < min_obs_months:
        return (0, 0.0, 0)
    return lead_lag(pair.iloc[:, 0], pair.iloc[:, 1],
                    max_lag=max_lag_months, min_obs=min_obs_months)


def estimate_cycle_lags(ind_ret_daily: pd.DataFrame, edges: list[dict], *,
                        max_lag_months: int = MAX_LAG_MONTHS,
                        min_obs_months: int = MIN_OBS_MONTHS) -> dict:
    """산업 간 엣지별 사이클 시차 표. 별도 워크플로가 긴 패널로 돌려 저장한다.

    **엣지를 만들지 않는다.** 이미 있는 엣지에 대해서만 시차를 잰다.
    """
    monthly = to_monthly(ind_ret_daily)
    out: dict[str, dict] = {}
    seen = set()
    for e in edges:
        sk, sn = G.split_node(e["src"])
        dk, dn = G.split_node(e["dst"])
        if sk != "I" or dk != "I":
            continue
        if (sn, dn) in seen or sn not in monthly.columns or dn not in monthly.columns:
            continue
        seen.add((sn, dn))
        lag, c, n = cycle_lead_lag(monthly[sn], monthly[dn],
                                   max_lag_months=max_lag_months,
                                   min_obs_months=min_obs_months)
        if n:
            out[f"{sn}→{dn}"] = {"lag_months": lag, "corr": round(c, 3), "obs": n}
    return out


def attach_cycle_lags(edges: list[dict], table: dict) -> list[dict]:
    """저장된 사이클 시차 표를 엣지에 붙인다. 없으면 조용히 그냥 둔다."""
    out = []
    for e in edges:
        new = dict(e)
        sk, sn = G.split_node(e["src"])
        dk, dn = G.split_node(e["dst"])
        hit = table.get(f"{sn}→{dn}") if (sk == "I" and dk == "I") else None
        if hit:
            new["cycle_lag_months"] = hit["lag_months"]
            new["cycle_corr"] = hit["corr"]
        out.append(new)
    return out


def corr_threshold(n_obs: int, n_pairs: int, alpha: float = 0.01) -> float:
    """다중비교를 감안한 |상관| 하한(본페로니 근사).

    380만 쌍을 p<0.01로 걸러도 우연히 3.8만 쌍이 남는다. 쌍 수를 넣어 임계를
    올린다. 정밀한 검정이 아니라, '이 정도는 넘어야 눈여겨볼 값'이라는 선이다.
    """
    if n_obs <= 3 or n_pairs <= 0:
        return 1.0
    z = 2.575 + math.log(max(n_pairs, 1)) ** 0.5   # 쌍이 많을수록 보수적으로
    r = z / math.sqrt(n_obs - 3)
    return min(0.99, math.tanh(r))


def score_edges(edges: list[dict], ind_ret: pd.DataFrame, *,
                max_lag: int = MAX_LAG, min_obs: int = MIN_OBS) -> list[dict]:
    """산업 간 엣지에 가격 근거를 붙인다. **엣지를 추가하지도 삭제하지도 않는다.**

    반환은 입력과 같은 길이·같은 순서의 새 리스트다. 원본은 건드리지 않는다.
    """
    out: list[dict] = []
    for e in edges:
        new = dict(e)
        src_kind, src_name = G.split_node(e["src"])
        dst_kind, dst_name = G.split_node(e["dst"])
        if (src_kind == "I" and dst_kind == "I"
                and src_name in ind_ret.columns and dst_name in ind_ret.columns):
            lag, c, n = lead_lag(ind_ret[src_name], ind_ret[dst_name],
                                 max_lag=max_lag, min_obs=min_obs)
            if n:
                new["price_corr"] = round(c, 3)
                new["price_lag"] = lag        # 양수면 src가 선행
                new["price_obs"] = n
        out.append(new)
    return out


def review_queue(scored: list[dict], weak: float = WEAK_CORR) -> list[dict]:
    """가격이 뒷받침하지 않는 엣지 목록.

    **삭제 목록이 아니다.** 신생 관계는 아직 가격에 안 실렸을 수 있고, 소속
    종목이 적은 산업은 잡음이 크다. 사람이 볼 대기열이다.
    """
    out = []
    for e in scored:
        c = e.get("price_corr")
        if c is None:
            continue
        if abs(c) < weak:
            out.append({"src": e["src"], "dst": e["dst"], "rel": e["rel"],
                        "source": e.get("source"), "origin": e.get("origin"),
                        "price_corr": c, "price_obs": e.get("price_obs"),
                        "evidence": (e.get("evidence") or "")[:120]})
    return sorted(out, key=lambda r: abs(r["price_corr"]))


def candidates(ind_ret: pd.DataFrame, edges: list[dict], *,
               max_lag: int = MAX_LAG, min_obs: int = MIN_OBS,
               limit: int = 40) -> list[dict]:
    """엣지가 없는데 잔차 상관이 높은 산업 쌍 — **공시로 확인할 후보**.

    자동으로 엣지를 만들지 않는다. 여기서 나온 쌍은 해당 산업 소속 종목의
    사업보고서를 실제로 읽어 근거 문장을 찾은 뒤에만 엣지가 된다. 그래야
    '인용이 붙은 관계'라는 품질 기준이 유지된다.
    """
    linked = set()
    for e in edges:
        sk, sn = G.split_node(e["src"])
        dk, dn = G.split_node(e["dst"])
        if sk == "I" and dk == "I":
            linked.add((sn, dn))
            linked.add((dn, sn))

    cols = list(ind_ret.columns)
    n_pairs = max(1, len(cols) * (len(cols) - 1) // 2)
    found = []
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            if (a, b) in linked:
                continue
            lag, c, n = lead_lag(ind_ret[a], ind_ret[b],
                                 max_lag=max_lag, min_obs=min_obs)
            if not n:
                continue
            if abs(c) < corr_threshold(n, n_pairs):
                continue
            found.append({"a": a, "b": b, "corr": round(c, 3),
                          "lag": lag, "obs": n,
                          "note": "공시로 확인 필요 — 자동으로 엣지를 만들지 않는다"})
    return sorted(found, key=lambda r: -abs(r["corr"]))[:limit]


# ---- 사이클 시차 표의 저장·적재 ------------------------------------------

LAG_FILE = ROOT / "data" / "lead_lag.json"


def load_cycle_lags(path: Path = LAG_FILE) -> dict:
    """저장된 사이클 시차 표. 없으면 빈 dict — 파이프라인은 그냥 진행한다."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("lags") or {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_cycle_lags(table: dict, asof: str, panel_days: int,
                    path: Path = LAG_FILE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"asof": asof, "panel_days": panel_days,
         "max_lag_months": MAX_LAG_MONTHS, "min_obs_months": MIN_OBS_MONTHS,
         "lags": table}, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


MD_FILE = ROOT / "data" / "lead_lag.md"


def render_markdown(table: dict, asof: str, panel_days: int) -> str:
    """사람이 읽는 표. JSON만 남기면 실제로 아무도 안 본다.

    GitHub에서 파일을 클릭하면 그대로 렌더링되므로, 코드를 몰라도 확인할 수 있다.
    """
    months = panel_days / TRADING_DAYS_PER_MONTH
    head = [
        f"# 밸류체인 사이클 시차 (기준일 {asof})",
        "",
        f"패널 {panel_days:,}거래일(약 {months:.0f}개월) · 월간 누적 수익률 · 시장 요인 제거 후",
        "",
        "**읽는 법** — `+6개월`이면 왼쪽 산업이 오른쪽보다 6개월 **먼저** 움직였다는 뜻이다.",
        "왼쪽이 오르면 오른쪽은 대략 그만큼 뒤에 따라올 여지가 있다.",
        "",
        "**주의** — 이건 관계의 *타이밍*이지 *존재*가 아니다. 관계 자체는 사업보고서에서",
        "인용과 함께 확인된 것만 그래프에 있다. 상관이 낮다고 관계가 없는 게 아니라,",
        "아직 가격에 안 실렸거나 소속 종목이 적어 잡음이 큰 것일 수 있다.",
        "",
    ]
    if not table:
        head += ["측정된 시차가 없습니다. 패널이 짧거나 산업별 소속 종목이 부족합니다.", ""]
        return "\n".join(head)

    head += ["| 선행 산업 | 후행 산업 | 시차 | 상관 | 관측(개월) |",
             "|---|---|---:|---:|---:|"]
    rows = sorted(table.items(), key=lambda kv: -abs(kv[1]["corr"]))
    for key, v in rows:
        a, _, b = key.partition("→")
        lag = v["lag_months"]
        label = f"{lag:+d}개월" if lag else "동행"
        head.append(f"| {a} | {b} | {label} | {v['corr']:+.2f} | {v['obs']} |")
    head += ["", f"총 {len(table)}건. 상관 절대값이 큰 순.", ""]
    return "\n".join(head)


def main() -> None:
    """긴 패널로 사이클 시차를 재서 파일로 남긴다 (python -m src.corr).

    파이프라인에서 매일 돌리지 않는다. 4년치 패널이 필요해 조회가 무겁고,
    시차는 매일 바뀌는 값이 아니다.
    """
    import argparse
    from datetime import datetime, timedelta, timezone

    from . import graph_build, groups, prices, universe

    p = argparse.ArgumentParser()
    p.add_argument("--date", help="기준일 YYYYMMDD (기본: 오늘 KST)")
    p.add_argument("--days", type=int, default=LONG_PANEL_DAYS,
                   help=f"패널 거래일 수 (기본 {LONG_PANEL_DAYS})")
    args = p.parse_args()

    end = args.date or datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d")
    print(f"긴 패널 수집: {end} 기준 {args.days}거래일 "
          f"(시차 최대 {MAX_LAG_MONTHS}개월 + 관측 {MIN_OBS_MONTHS}개월)")
    dates, close, _vol, _val, snapshot = prices.fetch_panel(end, args.days)
    base = dates[-1]
    print(f"  거래일 {len(dates)}일 / 종목 {len(close.columns)}개 ({dates[0]}~{base})")

    uni = universe.save_from_snapshot(base, snapshot)
    caps = pd.Series({t: e["market_cap"] for t, e in uni.entries.items()
                      if e.get("market_cap")}, dtype="float64")
    edges, _ = graph_build.build_persistent(uni, base)
    members = {name[len("산업:"):]: tickers
               for name, tickers in groups.fetch_industry_groups(
                   min_members=MIN_MEMBERS, edges=edges).items()}
    print(f"  산업 {len(members)}개")

    ind = residualize(industry_returns(close, caps, members),
                      prices.market_series(close, caps))
    table = estimate_cycle_lags(ind, edges)
    out = save_cycle_lags(table, base, len(dates))
    MD_FILE.write_text(render_markdown(table, base, len(dates)), encoding="utf-8")

    print(f"\n사이클 시차 {len(table)}건 → {out}, {MD_FILE}")
    for k, v in sorted(table.items(), key=lambda kv: -abs(kv[1]["corr"]))[:25]:
        arrow = "선행" if v["lag_months"] > 0 else ("후행" if v["lag_months"] < 0 else "동행")
        print(f"  {k:44} {v['lag_months']:+3d}개월({arrow})  "
              f"corr {v['corr']:+.2f}  n={v['obs']}")
    if not table:
        print("  ::warning:: 시차를 하나도 재지 못했습니다 — 패널이 짧거나 "
              "산업별 소속 종목이 부족합니다.")


if __name__ == "__main__":
    main()
