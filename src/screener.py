"""정량 스크리닝: KRX 전 종목에서 강세 지속형/상승 전환형 후보를 추출한다.

pykrx로 일자별 스냅샷(전 종목 OHLCV)을 수집해 가격 패널을 만들고,
모멘텀·전환 시그널을 계산한 뒤 상위 후보를 JSON으로 저장한다.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pandas as pd
from pykrx import stock

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _retry(fn, *args, retries=3, delay=2, **kwargs):
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(delay * (attempt + 1))


KRX_MISSING_HINT = (
    "KRX_ID / KRX_PW 환경 변수가 설정되지 않았습니다.\n"
    "  KRX가 대량 조회에 로그인을 요구하므로 이 자격 증명 없이는 가격 수집이 되지 않습니다.\n"
    "  로컬: .env 또는 셸 환경변수로 설정\n"
    "  GitHub Actions: Settings → Secrets and variables → Actions → New repository secret\n"
    "  계정 발급: https://data.krx.co.kr")

KRX_REJECTED_HINT = (
    "KRX_ID / KRX_PW는 설정돼 있으나 KRX가 로그인을 거부했습니다.\n"
    "  (로그 위쪽의 'KRX 로그인 실패' 메시지가 pykrx가 받은 응답입니다.)\n"
    "  가장 흔한 원인: KRX_ID에 **이메일이 아니라 가입 시 만든 아이디**를 넣어야 합니다.\n"
    "    data.krx.co.kr 로그인 화면에서 실제로 입력하는 값과 같아야 합니다.\n"
    "  그 외: 비밀번호 변경 후 Secret 미갱신, 휴면 계정, 약관 재동의 필요.\n"
    "  확인: https://data.krx.co.kr 에서 그 ID/PW로 직접 로그인이 되는지 먼저 보세요.")


def get_trading_dates(end_date: str, n_days: int) -> list[str]:
    """end_date(YYYYMMDD) 기준 최근 n_days 거래일 목록(오름차순).

    pykrx는 로그인에 실패해도 예외를 올리지 않고 **빈 목록을 돌려준다.** 그대로
    두면 호출부에서 `dates[-1]`이 IndexError로 죽어, 진짜 원인이 스택트레이스
    어디에도 나오지 않는다. 여기서 원인을 지목한다.

    '설정 안 됨'과 '설정됐는데 거부됨'은 조치가 전혀 다르므로 구분해서 안내한다.
    """
    start = (pd.Timestamp(end_date) - pd.Timedelta(days=int(n_days * 1.8) + 30)).strftime("%Y%m%d")
    dates = _retry(stock.get_previous_business_days, fromdate=start, todate=end_date)
    dates = [d.strftime("%Y%m%d") for d in (dates or [])]
    if not dates:
        detail = (KRX_REJECTED_HINT
                  if (os.getenv("KRX_ID") and os.getenv("KRX_PW"))
                  else KRX_MISSING_HINT)
        raise RuntimeError(f"거래일을 하나도 조회하지 못했습니다({start}~{end_date}).\n{detail}")
    return dates[-n_days:]


def fetch_panel(dates: list[str]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """일자별 전 종목 스냅샷을 모아 close/volume/value 패널(index=date, columns=ticker)을 만든다."""
    closes, volumes, values = {}, {}, {}
    for i, d in enumerate(dates):
        frames = []
        for market in ("KOSPI", "KOSDAQ"):
            df = _retry(stock.get_market_ohlcv, d, market=market)
            if df is not None and not df.empty:
                frames.append(df)
        if not frames:
            continue
        snap = pd.concat(frames)
        # 거래정지 등으로 종가 0인 행 제외
        snap = snap[snap["종가"] > 0]
        closes[d] = snap["종가"]
        volumes[d] = snap["거래량"]
        values[d] = snap["거래대금"]
        if (i + 1) % 20 == 0:
            print(f"  ... 가격 수집 {i + 1}/{len(dates)}일")
    close = pd.DataFrame(closes).T.sort_index()
    volume = pd.DataFrame(volumes).T.sort_index()
    value = pd.DataFrame(values).T.sort_index()
    return close, volume, value


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


def fetch_names(base_date: str) -> dict[str, str]:
    """전 종목 티커 → 종목명 맵 (시장별 일괄 조회)."""
    names: dict[str, str] = {}
    for market in ("KOSPI", "KOSDAQ"):
        try:
            df = _retry(stock.get_market_price_change, base_date, base_date, market=market)
            if "종목명" in df.columns:
                names.update(df["종목명"].to_dict())
        except Exception:
            continue
    return names


def screen(end_date: str, cfg: dict) -> tuple[dict, pd.DataFrame]:
    """스크리닝 실행. 후보 목록과 전 종목 수익률 스냅샷을 반환/저장한다."""
    dates = get_trading_dates(end_date, cfg["lookback_days"])
    base_date = dates[-1]
    print(f"기준일: {base_date}, 조회 거래일 수: {len(dates)}")

    close, volume, value = fetch_panel(dates)
    if close.empty:
        # 거래일은 받았는데 스냅샷이 전부 비었다면 인증이 아니라 조회 쪽 문제다.
        # compute_signals까지 흘려보내면 'lookback_days를 늘리세요'라는 엉뚱한
        # 메시지가 나온다.
        raise RuntimeError(
            f"거래일 {len(dates)}일을 조회했으나 가격 스냅샷이 하나도 오지 않았습니다. "
            "KRX 응답 또는 조회 권한을 확인하세요.")
    sig = compute_signals(close, volume, value, cfg)

    # 시가총액 필터
    caps = []
    for market in ("KOSPI", "KOSDAQ"):
        cap_df = _retry(stock.get_market_cap, base_date, market=market)
        caps.append(cap_df["시가총액"])
    market_cap = pd.concat(caps)
    sig = sig.join(market_cap.rename("market_cap"), how="inner")
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

    names = fetch_names(base_date)
    for t in cand.index:
        if t not in names:
            names[t] = _retry(stock.get_market_ticker_name, t)

    candidates = []
    for t, row in cand.iterrows():
        candidates.append({
            "ticker": t,
            "name": names[t],
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
    snapshot = {}
    for t in sig.index:
        row = sig.loc[t]
        snapshot[t] = {
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
        json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

    print(f"후보 {len(candidates)}종목 (유니버스 {len(sig)}종목)")
    # 가격 패널은 팩터 베타·그룹 갭 회귀에 재사용한다(재조회 방지).
    return result, close.loc[:, close.columns.intersection(sig.index)]
