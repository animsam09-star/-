"""가격·종목 데이터의 단일 진입점.

두 개의 원천이 있다.

* **KRX OpenAPI** (`KRX_OPENAPI_KEY`) — 인증키 한 개. 권장 경로.
* **pykrx** (`KRX_ID`/`KRX_PW`) — data.krx.co.kr 웹 로그인. 계정이 필요하고,
  로그인이 막히면 예외 없이 빈 결과를 돌려줘 원인을 숨긴다.

어느 쪽을 쓰는지는 **환경 변수 하나로 결정되고, 실행 로그에 찍힌다.** 호출부가
원천을 알 필요는 없지만, 사람은 알아야 한다. 둘 다 없으면 여기서 멈춘다 —
데이터 없이 진행해서 '후보 0종목'이라는 그럴듯한 빈 결과를 내는 게 최악이다.
"""

from __future__ import annotations

import os

import pandas as pd

from . import krx_api

MISSING_HINT = (
    "KRX 데이터 접근 수단이 없습니다. 둘 중 하나를 등록하세요.\n"
    "  (권장) KRX_OPENAPI_KEY — openapi.krx.co.kr에서 인증키 발급 후\n"
    "         '유가증권 일별매매정보', '코스닥 일별매매정보' 이용신청(승인 필요)\n"
    "  (대체) KRX_ID + KRX_PW — data.krx.co.kr 웹 계정\n"
    "  GitHub Actions: Settings → Secrets and variables → Actions")


def source() -> str:
    """'openapi' | 'pykrx' | '' — 값은 절대 돌려주지 않는다."""
    if os.getenv(krx_api.ENV_KEY):
        return "openapi"
    if os.getenv("KRX_ID") and os.getenv("KRX_PW"):
        return "pykrx"
    return ""


def require_source() -> str:
    src = source()
    if not src:
        raise RuntimeError(MISSING_HINT)
    return src


# ---- pykrx 경로 --------------------------------------------------------
# OpenAPI가 표준 경로이므로 pykrx는 필요할 때만 import한다. 인증키만 가진
# 환경에서 pykrx가 죽어도 파이프라인은 돌아야 한다.

def _pykrx_panel(end_date: str, n_days: int):
    from pykrx import stock

    start = (pd.Timestamp(end_date)
             - pd.Timedelta(days=int(n_days * 1.8) + 30)).strftime("%Y%m%d")
    raw = stock.get_previous_business_days(fromdate=start, todate=end_date) or []
    dates = [d.strftime("%Y%m%d") for d in raw][-n_days:]
    if not dates:
        raise RuntimeError(
            f"거래일을 하나도 조회하지 못했습니다({start}~{end_date}).\n"
            "  KRX_ID/KRX_PW는 설정돼 있으나 로그인이 거부됐을 수 있습니다.\n"
            "  KRX_ID는 이메일이 아니라 가입 시 만든 아이디여야 합니다.")

    closes, volumes, values = {}, {}, {}
    latest = pd.DataFrame()
    for i, d in enumerate(dates):
        frames = []
        for market in ("KOSPI", "KOSDAQ"):
            df = stock.get_market_ohlcv(d, market=market)
            if df is not None and not df.empty:
                df = df.rename(columns={"종가": "close", "거래량": "volume",
                                        "거래대금": "value", "시가": "open",
                                        "고가": "high", "저가": "low"})
                df["market"] = market
                frames.append(df)
        if not frames:
            continue
        snap = pd.concat(frames)
        snap.index = snap.index.astype(str)
        latest = snap
        live = snap[snap["close"] > 0]
        closes[d] = live["close"]
        volumes[d] = live["volume"]
        values[d] = live["value"]
        if (i + 1) % 20 == 0:
            print(f"  ... 가격 수집 {i + 1}/{len(dates)}일")

    ordered = sorted(closes)
    return (ordered,
            pd.DataFrame(closes).T.sort_index(),
            pd.DataFrame(volumes).T.sort_index(),
            pd.DataFrame(values).T.sort_index(),
            latest)


def _pykrx_entries(base_date: str) -> dict[str, dict]:
    from . import universe as _u   # 순환 import 회피: 함수 안에서만 쓴다
    return _u.fetch_entries_pykrx(base_date)


# ---- 공개 API ----------------------------------------------------------

def fetch_panel(end_date: str, n_days: int, use_cache: bool = True):
    """(거래일 오름차순, close, volume, value, 기준일 스냅샷).

    기준일 스냅샷에는 name/market_cap/market 컬럼이 들어 있어, 종목 마스터와
    시총 필터를 **추가 조회 없이** 만들 수 있다.
    """
    src = require_source()
    print(f"가격 원천: {src}")
    if src == "openapi":
        return krx_api.fetch_panel(end_date, n_days, use_cache=use_cache)
    return _pykrx_panel(end_date, n_days)


def entries_from_snapshot(snapshot: pd.DataFrame, base_date: str) -> dict[str, dict]:
    """기준일 스냅샷 → 종목 마스터 엔트리.

    시세 응답에 종목명·시가총액이 함께 오므로 별도 조회가 필요 없다.
    (종목기본정보 엔드포인트는 이용신청이 따로라, 안 쓰는 편이 낫다.)
    """
    entries: dict[str, dict] = {}
    for ticker, row in snapshot.iterrows():
        name = row.get("name")
        cap = row.get("market_cap")
        entries[str(ticker)] = {
            "name": None if pd.isna(name) else str(name),
            "market": row.get("market"),
            "market_cap": None if pd.isna(cap) else int(cap),
            "sector": (None if "sector" not in snapshot.columns
                       or pd.isna(row.get("sector")) else str(row.get("sector"))),
        }
    return entries
