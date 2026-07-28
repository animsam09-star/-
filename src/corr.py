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

import math

import numpy as np
import pandas as pd

from . import graph as G

MIN_MEMBERS = 2        # 산업 수익률을 만들 최소 소속 종목 수
MIN_OBS = 60           # 상관을 낼 최소 관측일
MAX_LAG = 20           # 선행·후행을 볼 최대 거래일

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
