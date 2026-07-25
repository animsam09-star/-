"""수평 그래프 2: 매크로 팩터 노출도(베타)와 미반영 갭 계산.

핵심 아이디어
- 팩터(환율·구리·유가·금리 등)는 섹터를 관통해 동시에 작용한다. 노출도는 부호를 가진다.
  구리 상승은 제련업체엔 (+), 전선업체엔 (−)다. 따라서 베타의 부호가 반드시 필요하다.
- 수평 파급은 대개 같은 날 함께 일어나므로, 목록만으로는 알파가 없다.
  실제 기회는 "노출도는 높은데 아직 반응하지 않은 종목"이다.
  기대수익률(베타 × 팩터 변동) 대비 실제수익률의 차이 = 미반영 갭.

팩터 대용치는 전부 국내 상장 ETF/ETN이라 별도 데이터 소스가 필요 없다.
티커를 하드코딩하지 않고 종목명 키워드로 런타임에 해석한다(상장·폐지 대응).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
from pykrx import stock

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

MARKET_INDEX = "1001"  # KOSPI 지수
MIN_VALID_RATIO = 0.8  # 회귀에 쓰기 위한 최소 관측 비율


def _retry(fn, *args, retries=3, delay=1.5, **kwargs):
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(delay * (attempt + 1))


def as_date_index(obj):
    """인덱스를 'YYYYMMDD' 문자열로 통일한다.

    pykrx는 함수마다 인덱스 타입이 다르다. 전 종목 스냅샷(get_market_ohlcv(date, market=))은
    티커 인덱스를 주고, 그것으로 만든 스크리너의 가격 패널은 문자열 날짜 인덱스가 된다.
    반면 시계열 조회(get_index_ohlcv_by_date / get_etf_ohlcv_by_date)는 DatetimeIndex를 준다.

    이 둘을 섞으면 pandas가 서로 다른 라벨로 보아 정렬에 실패하는데, **예외가 아니라 전부
    NaN이 된다.** 시장 수익률이 통째로 NaN이 되면 fillna(0)을 거쳐 시장 요인이 0으로
    취급되고, 2팩터 보정이 조용히 무력화된다. 그래서 외부에서 들어오는 모든 시계열을
    이 함수로 통과시킨다.
    """
    idx = obj.index
    if isinstance(idx, pd.DatetimeIndex):
        new = idx.strftime("%Y%m%d")
    else:
        new = pd.Index([i.strftime("%Y%m%d") if hasattr(i, "strftime") else str(i)
                        for i in idx])
    out = obj.copy()
    out.index = new
    return out


def resolve_factor_tickers(specs: list[dict], base_date: str) -> dict[str, dict]:
    """종목명 키워드로 팩터 대용 ETF/ETN 티커를 해석한다.

    같은 키워드에 여러 상품이 걸리면 이름이 가장 짧은 것을 고른다
    (레버리지·헤지형 등 수식어가 붙지 않은 기본 상품일 가능성이 높다).
    """
    catalog: list[tuple[str, str, str]] = []  # (ticker, name, kind)
    for kind, lister, namer in (("etf", stock.get_etf_ticker_list, stock.get_etf_ticker_name),
                                ("etn", stock.get_etn_ticker_list, stock.get_etn_ticker_name)):
        try:
            for t in _retry(lister, date=base_date):
                try:
                    catalog.append((t, _retry(namer, t), kind))
                except Exception:
                    continue
        except Exception as e:
            print(f"  {kind.upper()} 목록 조회 실패: {e}")

    resolved: dict[str, dict] = {}
    for spec in specs:
        matches = [
            entry for entry in catalog
            if any(k in entry[1] for k in spec["keywords"])
            and not any(x in entry[1] for x in spec.get("exclude", []))
        ]
        if not matches:
            print(f"  팩터 '{spec['name']}' 대용 상품을 찾지 못해 건너뜁니다.")
            continue
        # 수식어(레버리지·헤지형 등)가 붙지 않은 기본 상품일 가능성이 높은 쪽을 고른다
        ticker, name, kind = min(matches, key=lambda e: len(e[1]))
        resolved[spec["name"]] = {"ticker": ticker, "proxy_name": name, "kind": kind}
    return resolved


def _fetch_close_series(ticker: str, kind: str, start: str, end: str) -> pd.Series | None:
    """ETF/ETN 종가 시계열. ETN 전용 조회 함수는 pykrx에 없으므로 일반 시세로 받는다."""
    fetchers = ([stock.get_etf_ohlcv_by_date, stock.get_market_ohlcv_by_date]
                if kind == "etf" else [stock.get_market_ohlcv_by_date])
    for fetcher in fetchers:
        try:
            df = _retry(fetcher, start, end, ticker)
        except Exception:
            continue
        if df is not None and not df.empty and "종가" in df.columns:
            s = as_date_index(df["종가"])
            s = s[s > 0]
            if not s.empty:
                return s
    return None


def fetch_factor_panel(resolved: dict[str, dict], dates: list[str]) -> pd.DataFrame:
    """팩터별 종가 시계열 패널. 시장(KOSPI 지수)은 항상 포함한다.

    모든 시계열은 as_date_index로 문자열 날짜 인덱스에 맞춘다. 이걸 빠뜨리면
    가격 패널과 정렬되지 않아 조용히 전부 NaN이 된다(as_date_index 설명 참조).
    """
    start, end = dates[0], dates[-1]
    series: dict[str, pd.Series] = {}

    try:
        idx = _retry(stock.get_index_ohlcv_by_date, start, end, MARKET_INDEX)
        if idx is not None and not idx.empty and "종가" in idx.columns:
            series["시장"] = as_date_index(idx["종가"])
    except Exception as e:
        print(f"  시장지수 조회 실패: {e}")

    for name, info in resolved.items():
        s = _fetch_close_series(info["ticker"], info.get("kind", "etf"), start, end)
        if s is None:
            print(f"  팩터 '{name}'({info['proxy_name']}) 시계열 조회 실패 — 제외")
            continue
        series[name] = s

    if not series:
        return pd.DataFrame()
    return pd.DataFrame(series).sort_index()


def _beta_vs(returns: pd.DataFrame, x: pd.Series) -> tuple[pd.Series, pd.Series]:
    """returns의 각 컬럼에 대해 x 단일 회귀의 베타와 상관계수를 벡터 연산으로 계산."""
    common = returns.index.intersection(x.index)
    y, xs = returns.loc[common], x.loc[common]

    keep = y.notna().mean() >= MIN_VALID_RATIO
    y = y.loc[:, keep].fillna(0.0)
    xs = xs.fillna(0.0)

    xc = xs - xs.mean()
    yc = y - y.mean()
    var_x = float((xc ** 2).sum())
    if var_x == 0 or y.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float)

    cov = yc.mul(xc, axis=0).sum()
    beta = cov / var_x
    denom = np.sqrt((yc ** 2).sum() * var_x)
    corr = cov / denom.replace(0, np.nan)
    return beta, corr


def compute_exposures(close: pd.DataFrame, factor_panel: pd.DataFrame,
                      window: int = 120, min_abs_corr: float = 0.2) -> dict:
    """시장 조정 후 팩터 노출도(베타·상관)를 계산한다.

    시장 요인을 먼저 제거하고 잔차에 각 팩터를 회귀하므로,
    '시장이 올라서 같이 올랐다'가 팩터 노출로 잘못 잡히지 않는다.
    """
    if factor_panel.empty:
        return {}

    close, factor_panel = as_date_index(close), as_date_index(factor_panel)

    # 조회 기간이 베타 구간보다 짧아도(스모크 테스트·신규 상장) 동작하도록 축소한다.
    window = max(20, min(window, len(close) - 1))
    stock_ret = close.pct_change(fill_method=None).iloc[-window:]
    factor_ret = factor_panel.pct_change(fill_method=None).reindex(stock_ret.index)

    if "시장" not in factor_ret.columns:
        return {}
    mkt = factor_ret["시장"]
    if mkt.notna().sum() < window * MIN_VALID_RATIO:
        # 시장 시계열이 가격 패널과 정렬되지 않은 상태. 여기서 진행하면 시장 요인이
        # 0으로 취급되어 조용히 틀린 결과가 나오므로 중단한다.
        print("  시장 시계열이 가격 패널과 정렬되지 않아 팩터 노출 계산을 건너뜁니다.")
        return {}
    mkt = mkt.fillna(0.0)

    # 1) 시장 베타 → 잔차
    beta_mkt, _ = _beta_vs(stock_ret, mkt)
    if beta_mkt.empty:
        return {}
    aligned = stock_ret.loc[:, beta_mkt.index].fillna(0.0)
    resid = aligned - np.outer(mkt.values, beta_mkt.values)
    resid = pd.DataFrame(resid, index=aligned.index, columns=aligned.columns)

    exposures: dict[str, dict[str, dict]] = {}
    factor_moves: dict[str, dict] = {}

    for fname in factor_ret.columns:
        if fname == "시장":
            continue
        f = factor_ret[fname].dropna()
        if len(f) < window * MIN_VALID_RATIO:
            continue
        # 팩터도 시장 성분을 제거해 다중공선성을 줄인다
        f_beta, _ = _beta_vs(f.to_frame("f"), mkt)
        f_resid = f - mkt.reindex(f.index).fillna(0.0) * float(f_beta.iloc[0])

        beta, corr = _beta_vs(resid, f_resid)
        for t in beta.index:
            c = corr.get(t)
            if pd.isna(c) or abs(c) < min_abs_corr:
                continue
            exposures.setdefault(t, {})[fname] = {
                "beta": round(float(beta[t]), 3),
                "corr": round(float(c), 3),
            }
        factor_moves[fname] = {
            "ret5": round(float(f.iloc[-5:].sum()), 4),
            "ret20": round(float(f.iloc[-20:].sum()), 4),
        }

    return {"exposures": exposures, "factor_moves": factor_moves,
            "market_beta": {t: round(float(v), 3) for t, v in beta_mkt.items()}}


def compute_group_gaps(close: pd.DataFrame, group_ret: pd.DataFrame,
                       groups: dict[str, list[str]], market_ret: pd.Series | None = None,
                       window: int = 120, recent: int = 5,
                       min_beta_corr: float = 0.3) -> dict[str, dict]:
    """그룹별 미반영 갭을 2팩터(시장 + 그룹 고유) 모델로 계산한다.

    시장 요인을 분리하지 않으면 모든 종목이 시장을 통해 서로 상관되어,
    그룹과 무관한 종목까지 '미반영'으로 잡힌다. 그래서 그룹 수익률에서 시장 성분을
    제거한 뒤 그 잔차에 대한 베타를 그룹 고유 민감도로 쓴다.

        기대수익률 = β_시장 × 시장 최근수익률 + β_그룹 × 그룹고유 최근수익률
        갭 = 기대 − 실제

    갭이 클수록 '같이 움직였어야 하는데 아직 안 움직인' 종목이다.
    판정 대상은 해당 그룹의 구성종목으로 한정한다(그룹 밖 종목의 갭은 근거가 없다).
    """
    close, group_ret = as_date_index(close), as_date_index(group_ret)
    stock_ret = close.pct_change(fill_method=None)

    if market_ret is None:
        market_ret = stock_ret.mean(axis=1)  # 시장 시계열이 없으면 유니버스 평균으로 대용
    else:
        market_ret = as_date_index(market_ret)
        overlap = market_ret.reindex(close.index).notna().sum()
        if overlap < len(close) * MIN_VALID_RATIO:
            print("  시장 시계열이 가격 패널과 정렬되지 않아 유니버스 평균으로 대체합니다.")
            market_ret = stock_ret.mean(axis=1)

    window = max(20, min(window, len(close) - 1))
    gaps: dict[str, dict] = {}

    for g in group_ret.columns:
        members = [m for m in groups.get(g, []) if m in stock_ret.columns]
        if len(members) < 3:
            continue

        gr = group_ret[g].dropna()
        hist_idx = gr.index[-window:]
        if len(hist_idx) < window * MIN_VALID_RATIO:
            continue

        mkt = market_ret.reindex(hist_idx).fillna(0.0)
        grp = gr.reindex(hist_idx).fillna(0.0)

        # 그룹 수익률에서 시장 성분 제거 → 그룹 고유 요인
        gb, _ = _beta_vs(grp.to_frame("g"), mkt)
        if gb.empty:
            continue
        grp_resid = grp - mkt * float(gb.iloc[0])

        recent_grp = float(grp_resid.iloc[-recent:].sum())
        recent_mkt = float(mkt.iloc[-recent:].sum())
        if abs(recent_grp) < 0.02:  # 그룹 고유 움직임이 없으면 판정 의미 없음
            continue

        member_ret = stock_ret.loc[hist_idx, members]
        beta_mkt, _ = _beta_vs(member_ret, mkt)
        if beta_mkt.empty:
            continue
        aligned = member_ret.loc[:, beta_mkt.index].fillna(0.0)
        resid = aligned - np.outer(mkt.values, beta_mkt.values)
        resid = pd.DataFrame(resid, index=aligned.index, columns=aligned.columns)

        beta_grp, corr = _beta_vs(resid, grp_resid)

        # 최근 구간 실제 수익률. 거래정지로 관측이 없으면 sum()이 0을 돌려주는데,
        # 그대로 두면 '안 움직인 종목'으로 보여 미반영 1순위가 된다. 관측 수를 세어
        # 실제로 거래된 종목만 판정 대상으로 삼는다.
        recent_block = stock_ret.iloc[-recent:]
        actual = recent_block.sum(min_count=1)
        valid_recent = recent_block.notna().sum()
        min_recent_obs = max(1, int(recent * 0.6))

        rows = []
        for t in beta_grp.index:
            c = corr.get(t)
            if pd.isna(c) or c < min_beta_corr:
                continue  # 그룹 고유 요인과 함께 움직인 이력이 없으면 파급 대상이 아니다
            if int(valid_recent.get(t, 0)) < min_recent_obs:
                continue  # 최근 구간에 거의 거래되지 않음(거래정지 등)
            act = actual.get(t)
            if pd.isna(act):
                continue
            expected = float(beta_mkt[t]) * recent_mkt + float(beta_grp[t]) * recent_grp
            act = float(act)
            rows.append({
                "ticker": t,
                "beta": round(float(beta_grp[t]), 2),
                "corr": round(float(c), 2),
                "expected": round(expected, 4),
                "actual": round(act, 4),
                "gap": round(expected - act, 4),
            })
        if rows:
            rows.sort(key=lambda r: r["gap"], reverse=True)
            gaps[g] = {"group_move": round(recent_grp, 4), "members": rows}
    return gaps


def build(close: pd.DataFrame, group_ret: pd.DataFrame, groups: dict[str, list[str]],
          base_date: str, cfg: dict) -> dict:
    """팩터 노출 + 그룹 갭을 계산해 저장한다."""
    dates = list(close.index)
    resolved = resolve_factor_tickers(cfg["factors"], base_date)
    if resolved:
        print("  팩터 대용: " + ", ".join(f"{k}={v['proxy_name']}" for k, v in resolved.items()))
    panel = fetch_factor_panel(resolved, dates)

    market_ret = None
    if not panel.empty and "시장" in panel.columns:
        market_ret = panel["시장"].pct_change(fill_method=None).reindex(close.index)

    exposure = compute_exposures(close, panel, window=cfg["beta_window"],
                                 min_abs_corr=cfg["min_abs_corr"])
    gaps = compute_group_gaps(close, group_ret, groups, market_ret,
                              window=cfg["beta_window"], recent=cfg["gap_window"],
                              min_beta_corr=cfg["min_group_corr"])

    out = {
        "base_date": base_date,
        "factor_proxies": resolved,
        "factor_moves": exposure.get("factor_moves", {}),
        "exposures": exposure.get("exposures", {}),
        "group_gaps": gaps,
    }
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / f"factors_{base_date}.json").write_text(
        json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"팩터 {len(out['factor_moves'])}개, 노출 종목 {len(out['exposures'])}개, "
          f"갭 산출 그룹 {len(gaps)}개")
    return out
