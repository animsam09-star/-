"""정량 스크리닝: KRX 전 종목에서 강세 지속형/상승 전환형 후보를 추출한다.

일자별 스냅샷(전 종목 OHLCV)을 모아 가격 패널을 만들고, 모멘텀·전환 시그널을
계산한 뒤 상위 후보를 JSON으로 저장한다.

데이터 원천은 src/prices.py가 고른다(KRX OpenAPI 인증키 우선, 없으면 pykrx).
이 모듈은 어느 쪽인지 알 필요가 없다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from . import prices

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def compute_signals(close: pd.DataFrame, volume: pd.DataFrame, value: pd.DataFrame,
                    cfg: dict) -> pd.DataFrame:
    """종목별 시그널 테이블을 계산한다.

    거래정지 종목은 당일 종가가 결측이라 수익률이 NaN이 되고, 마지막 dropna에서 빠진다.
    (결측을 앞값으로 채우면 '전혀 안 움직인 종목'으로 보여 잘못 편입되므로 채우지 않는다.)
    """
    # 데이터가 60일 미만인 종목(신규상장 등)은 제외
    valid = close.count() >= 60
    close = close.loc[:, valid]

    def period_return(days: int) -> pd.Series:
        """days 거래일 전 대비 수익률. 기간이 부족하면 빈 결과를 돌려준다."""
        if len(close) <= days:
            return pd.Series(dtype=float)
        return close.iloc[-1] / close.iloc[-(days + 1)] - 1

    ret5, ret20, ret60 = period_return(5), period_return(20), period_return(60)
    if ret20.empty:
        raise ValueError(
            f"20일 수익률을 계산하려면 최소 21거래일이 필요합니다(현재 {len(close)}일). "
            "config.yaml의 screener.lookback_days를 늘리세요.")

    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()

    # 골든크로스: 최근 N일 내 ma20이 ma60을 상향 돌파
    gc_window = cfg["golden_cross_window"]
    above = ma20 > ma60
    golden_cross = (above.iloc[-1]) & (~above.iloc[-(gc_window + 1)])

    # 60일 이평 기울기 전환: 최근 10일 기울기 (+), 30일 전 10일 기울기 (-)
    slope_now = ma60.iloc[-1] - ma60.iloc[-11] if len(ma60) > 11 else pd.Series(dtype=float)
    slope_before = ma60.iloc[-31] - ma60.iloc[-41] if len(ma60) > 41 else pd.Series(dtype=float)
    slope_turn = (slope_now > 0) & (slope_before < 0)

    # 거래량 급증: 최근 5일 평균 / 직전 20일 평균
    vol_recent = volume.iloc[-5:].mean()
    vol_base = volume.iloc[-25:-5].mean()
    vol_surge = vol_recent / vol_base.replace(0, pd.NA)

    # 120일 신고가 근접도
    high120 = close.iloc[-120:].max()
    high_proximity = close.iloc[-1] / high120

    avg_turnover = value.iloc[-20:].mean()

    sig = pd.DataFrame({
        "close": close.iloc[-1],
        "ret5": ret5,
        "ret20": ret20,
        "ret60": ret60,
        "golden_cross": golden_cross,
        "slope_turn": slope_turn,
        "vol_surge": vol_surge,
        "high_proximity": high_proximity,
        "avg_turnover": avg_turnover,
    })
    return sig.dropna(subset=["ret20", "high_proximity"])


def screen(end_date: str, cfg: dict) -> tuple[dict, pd.DataFrame]:
    """스크리닝 실행. 후보 목록과 전 종목 수익률 스냅샷을 반환/저장한다."""
    dates, close, volume, value, snapshot = prices.fetch_panel(
        end_date, cfg["lookback_days"])
    if not dates or close.empty:
        # 여기까지 왔다는 건 자격 증명은 통했는데 데이터가 안 왔다는 뜻이다.
        # compute_signals까지 흘려보내면 'lookback_days를 늘리세요'라는 엉뚱한
        # 메시지가 나온다.
        raise RuntimeError(
            f"거래일 {len(dates)}일을 조회했으나 가격 스냅샷이 하나도 오지 않았습니다. "
            "KRX 응답 또는 조회 권한을 확인하세요.")
    base_date = dates[-1]
    print(f"기준일: {base_date}, 조회 거래일 수: {len(dates)}")

    sig = compute_signals(close, volume, value, cfg)

    # 시가총액·종목명은 기준일 스냅샷에 이미 들어 있다(추가 조회 없음).
    if "market_cap" not in snapshot.columns or snapshot["market_cap"].isna().all():
        raise RuntimeError(
            "기준일 스냅샷에 시가총액이 없습니다. 시총 필터를 적용할 수 없어 "
            "중단합니다. (필터를 조용히 건너뛰면 유니버스가 통째로 달라집니다.)")
    names = {str(t): v for t, v in snapshot["name"].dropna().items()}
    sig = sig.join(snapshot["market_cap"].dropna(), how="inner")
    sig = sig[(sig["market_cap"] >= cfg["min_market_cap"])
              & (sig["avg_turnover"] >= cfg["min_avg_turnover"])]

    # 유형 태깅
    momentum = (sig["ret20"] >= cfg["momentum_ret20"]) | (sig["high_proximity"] >= cfg["high_proximity"])
    turnaround = (sig["golden_cross"] | sig["slope_turn"]) & (sig["vol_surge"] >= cfg["volume_surge_ratio"])
    sig["trigger"] = None
    sig.loc[turnaround, "trigger"] = "상승전환"
    sig.loc[momentum, "trigger"] = "강세지속"          # 둘 다 해당하면 강세지속 우선
    cand = sig.dropna(subset=["trigger"]).copy()

    # 복합 점수: 수익률 순위 + 거래량 급증 순위 + 신고가 근접 순위
    cand["score"] = (cand["ret20"].rank(pct=True)
                     + cand["vol_surge"].rank(pct=True)
                     + cand["high_proximity"].rank(pct=True))
    cand = cand.sort_values("score", ascending=False).head(cfg["top_n"])

    candidates = []
    for t, row in cand.iterrows():
        candidates.append({
            "ticker": t,
            "name": names.get(t),
            "trigger": row["trigger"],
            "close": int(row["close"]),
            "ret5": round(float(row["ret5"]), 4),
            "ret20": round(float(row["ret20"]), 4),
            "ret60": round(float(row["ret60"]), 4) if pd.notna(row["ret60"]) else None,
            "vol_surge": round(float(row["vol_surge"]), 2),
            "high_proximity": round(float(row["high_proximity"]), 3),
            "market_cap": int(row["market_cap"]),
        })

    # 전 종목 수익률 스냅샷: 파급 분석에서 수혜 후보의 '미반영 체크'에 사용
    returns = {}
    for t in sig.index:
        row = sig.loc[t]
        returns[t] = {
            "name": names.get(t),
            "ret5": round(float(row["ret5"]), 4) if pd.notna(row["ret5"]) else None,
            "ret20": round(float(row["ret20"]), 4),
            "ret60": round(float(row["ret60"]), 4) if pd.notna(row["ret60"]) else None,
        }

    result = {"base_date": base_date, "candidates": candidates, "universe_size": len(sig)}

    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / f"candidates_{base_date}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (DATA_DIR / f"returns_{base_date}.json").write_text(
        json.dumps(returns, ensure_ascii=False), encoding="utf-8")

    # 종목 마스터는 스크리닝 필터와 **독립적으로** 전 종목을 담아야 한다.
    # 같은 스냅샷에서 만들면 추가 조회가 없고, 시총 하한에 걸린 소형 후방
    # 소재주도 티커 해석이 된다.
    from . import universe as _universe
    _universe.save_from_snapshot(base_date, snapshot)

    print(f"후보 {len(candidates)}종목 (유니버스 {len(sig)}종목)")
    # 가격 패널은 팩터 베타·그룹 갭 회귀에 재사용한다(재조회 방지).
    return result, close.loc[:, close.columns.intersection(sig.index)]
