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


def corr_t(r: float, n: int) -> float:
    """상관의 t값 |r|·√(n-2)/√(1-r²). 관측 수를 반영한 '증거의 무게'다.

    시차마다 관측 수가 다른데 |r|만으로 비교하면 표본이 얇은 시차가 이긴다.
    n이 3 미만이거나 |r|이 1이면 비교할 값이 없으므로 0을 돌려준다.
    """
    if n < 3:
        return 0.0
    r2 = min(abs(float(r)), 0.999999) ** 2
    return abs(r) * math.sqrt(n - 2) / math.sqrt(1.0 - r2)


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

    ## 왜 |상관|이 아니라 t값으로 고르는가 — 최대값 선택 편향

    시차를 밀수록 겹치는 구간이 짧아진다. 월간 42개월 패널에서 lag 0은 42개,
    lag 18은 24개다. 그런데 상관의 표준오차는 대략 1/√(n-3)이라, **관측이 적은
    시차일수록 잡음만으로도 큰 |상관|이 나온다.** 37개 시차를 훑어 최대값 하나를
    고르면, 그 최대값은 구조가 아니라 표본이 가장 얇은 경계에서 나오기 쉽다.

    실측이 정확히 그랬다 — 302건 중 경계(±14~18개월)에 116건이 몰렸고, 그
    구간의 관측 중앙값은 27개월로 가장 적은데 |상관| 중앙값은 0.46으로 lag 1~6
    구간(0.46)과 다르지 않았다. 구조가 아니라 자유도가 만든 분포다.

    그래서 비교를 t = |r|·√(n-2)/√(1-r²)로 한다. 같은 |상관|이면 관측이 많은
    쪽이 이긴다. 시차가 얼마든 **같은 증거의 무게**로 견주게 하는 게 목적이지,
    긴 시차에 벌을 주려는 게 아니다.
    """
    best = (0, 0.0, 0)
    best_t = -1.0
    for lag in range(-max_lag, max_lag + 1):
        # lag>0: a를 뒤로 밀어 b의 과거와 맞춘다 → a가 선행
        x, y = (a.shift(lag), b) if lag >= 0 else (a, b.shift(-lag))
        pair = pd.concat([x, y], axis=1).dropna()
        if len(pair) < min_obs:
            continue
        c = pair.iloc[:, 0].corr(pair.iloc[:, 1])
        if pd.isna(c):
            continue
        t = corr_t(float(c), len(pair))
        # 동점이면 시차가 짧은 쪽을 남긴다. 같은 근거라면 더 단순한 설명이 낫다.
        if t > best_t or (t == best_t and abs(lag) < abs(best[0])):
            best, best_t = (lag, float(c), len(pair)), t
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


# 두 산업의 소속 종목이 이만큼 겹치면 시차를 재지 않는다. 재 봐야 자기 자신과의
# 상관이라 언제나 lag 0, corr 1에 가깝게 나온다.
MAX_MEMBER_OVERLAP = 0.5


def member_overlap(a: list[str], b: list[str]) -> float:
    """두 산업 소속의 자카드 겹침(0~1)."""
    sa, sb = set(a or ()), set(b or ())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def estimate_cycle_lags(ind_ret_daily: pd.DataFrame, edges: list[dict], *,
                        members: dict[str, list[str]] | None = None,
                        max_lag_months: int = MAX_LAG_MONTHS,
                        min_obs_months: int = MIN_OBS_MONTHS,
                        max_overlap: float = MAX_MEMBER_OVERLAP) -> dict:
    """산업 간 엣지별 사이클 시차 표. 별도 워크플로가 긴 패널로 돌려 저장한다.

    **엣지를 만들지 않는다.** 이미 있는 엣지에 대해서만 시차를 잰다.

    ## 소속이 겹치는 쌍은 재지 않는다

    첫 라이브 실행에서 `철강 → 후판·강재`가 상관 **+1.00**으로 나왔다. 발견이
    아니라 두 노드에 같은 회사 목록(POSCO홀딩스·현대제철·동국제강)이 들어 있어서다.
    산업 수익률이 문자 그대로 같은 시계열이니 상관이 1일 수밖에 없다.

    이건 데이터 입력의 그림자이지 시장 사실이 아니다. 더 나쁜 건, 그 가짜 1.00이
    상관 순 정렬에서 맨 위에 올라와 진짜 관계를 가린다는 점이다.

    구조적으로 피할 수 없는 경우도 있다 — '후판·강재'는 '철강' 회사들의 제품
    라인이라 소속이 겹치는 게 정상이다. 그런 쌍은 **가격으로 분리할 수 없다**는
    사실을 기록으로 남기고(reason) 시차 추정에서 뺀다.

    ## 한 쌍은 한 번만 잰다

    그래프는 같은 관계를 후방·전방 두 방향으로 저장한다. 방향마다 따로 재면
    A→B와 B→A가 각각 나오는데, 이 둘은 서로 다른 측정이 아니라 **같은 측정을
    부호만 뒤집은 것**이다(lead_lag(a,b)의 최적 시차는 lead_lag(b,a)의 부호
    반전과 같다). 실제로 302건이라던 표는 고유 쌍 148개가 정확히 두 번씩 들어간
    것이었고, 그래서 시차 분포가 부호에 대해 완벽히 대칭이었다. 건수가 두 배로
    보이면 표본이 두 배인 줄로 읽힌다.

    한 번만 재고, 반대 방향은 `mirrored`를 달아 부호를 뒤집어 넣는다. 엣지에
    붙일 때는 두 방향 모두 필요하므로 키는 그대로 둔다.
    """
    monthly = to_monthly(ind_ret_daily)
    members = members or {}
    out: dict[str, dict] = {}
    seen = set()
    for e in edges:
        sk, sn = G.split_node(e["src"])
        dk, dn = G.split_node(e["dst"])
        if sk != "I" or dk != "I":
            continue
        pair_key = tuple(sorted((sn, dn)))
        if pair_key in seen or sn not in monthly.columns or dn not in monthly.columns:
            continue
        seen.add(pair_key)

        ov = member_overlap(members.get(sn, []), members.get(dn, []))
        if ov >= max_overlap:
            skip = {"skipped": "구성 중복", "overlap": round(ov, 2)}
            out[f"{sn}→{dn}"] = skip
            out[f"{dn}→{sn}"] = {**skip, "mirrored": True}
            continue

        lag, c, n = cycle_lead_lag(monthly[sn], monthly[dn],
                                   max_lag_months=max_lag_months,
                                   min_obs_months=min_obs_months)
        if n:
            row = {"lag_months": lag, "corr": round(c, 3), "obs": n,
                   "overlap": round(ov, 2)}
            out[f"{sn}→{dn}"] = row
            out[f"{dn}→{sn}"] = {**row, "lag_months": -lag, "mirrored": True}
    return out


def attach_cycle_lags(edges: list[dict], table: dict) -> list[dict]:
    """저장된 사이클 시차 표를 엣지에 붙인다. 없으면 조용히 그냥 둔다."""
    out = []
    for e in edges:
        new = dict(e)
        sk, sn = G.split_node(e["src"])
        dk, dn = G.split_node(e["dst"])
        hit = table.get(f"{sn}→{dn}") if (sk == "I" and dk == "I") else None
        if hit and "lag_months" in hit:
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

    # 반대 방향(mirrored)은 같은 측정의 부호 반전이다. 표에 둘 다 실으면 건수가
    # 두 배로 보이고, 읽는 쪽은 근거가 두 배인 줄 안다.
    measured = {k: v for k, v in table.items()
                if "lag_months" in v and not v.get("mirrored")}
    skipped = {k: v for k, v in table.items()
               if "lag_months" not in v and not v.get("mirrored")}

    head += ["| 선행 산업 | 후행 산업 | 시차 | 상관 | 소속 겹침 | 관측(개월) |",
             "|---|---|---:|---:|---:|---:|"]
    for key, v in sorted(measured.items(), key=lambda kv: -abs(kv[1]["corr"])):
        a, _, b = key.partition("→")
        lag = v["lag_months"]
        label = f"{lag:+d}개월" if lag else "동행"
        head.append(f"| {a} | {b} | {label} | {v['corr']:+.2f} | "
                    f"{v.get('overlap', 0):.0%} | {v['obs']} |")
    head += ["", f"총 {len(measured)}건. 상관 절대값이 큰 순.", ""]

    if skipped:
        head += ["## 가격으로 분리할 수 없는 쌍", "",
                 "두 산업의 소속 종목이 절반 넘게 겹치면 산업 수익률이 사실상 같은",
                 "시계열이라 상관이 언제나 1에 가깝게 나온다. 그건 시장 사실이 아니라",
                 "구성의 그림자다. 관계 자체는 유효하되(예: '후판·강재'는 '철강' 회사들의",
                 "제품 라인이다) 시차를 가격으로 재는 것이 불가능하므로 제외했다.", "",
                 "| 관계 | 소속 겹침 |", "|---|---:|"]
        for key, v in sorted(skipped.items(), key=lambda kv: -kv[1]["overlap"]):
            head.append(f"| {key.replace('→', ' → ')} | {v['overlap']:.0%} |")
        head.append("")
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
    table = estimate_cycle_lags(ind, edges, members=members)
    out = save_cycle_lags(table, base, len(dates))
    MD_FILE.write_text(render_markdown(table, base, len(dates)), encoding="utf-8")

    print(f"\n사이클 시차 {len(table)}건 → {out}, {MD_FILE}")
    measured = {k: v for k, v in table.items()
                if "lag_months" in v and not v.get("mirrored")}
    skipped = {k: v for k, v in table.items()
               if "lag_months" not in v and not v.get("mirrored")}
    print(f"  고유 쌍 {len(measured)}개 측정 (반대 방향은 부호만 뒤집어 함께 저장)")
    for k, v in sorted(measured.items(), key=lambda kv: -abs(kv[1]["corr"]))[:25]:
        arrow = "선행" if v["lag_months"] > 0 else ("후행" if v["lag_months"] < 0 else "동행")
        print(f"  {k:44} {v['lag_months']:+3d}개월({arrow})  "
              f"corr {v['corr']:+.2f}  n={v['obs']}  겹침 {v.get('overlap', 0):.0%}")
    if skipped:
        print(f"\n  소속 중복으로 제외 {len(skipped)}쌍 (가격으로 분리 불가):")
        for k, v in list(skipped.items())[:12]:
            print(f"    {k}  겹침 {v['overlap']:.0%}")
    if not table:
        print("  ::warning:: 시차를 하나도 재지 못했습니다 — 패널이 짧거나 "
              "산업별 소속 종목이 부족합니다.")


if __name__ == "__main__":
    main()
