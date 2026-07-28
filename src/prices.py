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


def resolve_factor_proxies(specs: list[dict], dates: list[str],
                           use_cache: bool = True
                           ) -> tuple[dict[str, dict], pd.DataFrame]:
    """팩터 이름 → 대용 ETF, 그리고 그 ETF들의 종가 패널.

    티커를 하드코딩하지 않고 **상품명 키워드로 런타임에 해석**한다. ETF는 신규
    상장·상장폐지가 잦아 티커를 박아 두면 조용히 빈 컬럼이 된다.

    OpenAPI 경로에서는 ETN을 쓰지 않는다 — etn_bydd_trd가 401(이용신청 별도)이다.
    설정의 팩터 6종은 전부 ETF 대용치가 있으므로 손실이 없지만, 어떤 팩터가
    해석되지 않았는지는 **반드시 찍는다.** 조용히 빠지면 그 팩터의 노출도가
    '0'이 아니라 '없음'인데도 리포트는 똑같아 보인다.
    """
    src = require_source()
    if src != "openapi":
        from . import factors                     # pykrx 경로는 기존 구현 그대로
        base = dates[-1] if dates else ""
        resolved = factors.resolve_factor_tickers(specs, base)
        return resolved, factors.fetch_factor_panel(resolved, dates)

    close, catalog = krx_api.fetch_etf_panel(dates, use_cache=use_cache)
    names = catalog["name"].dropna().astype(str)

    resolved: dict[str, dict] = {}
    for spec in specs:
        keywords = spec.get("keywords") or []
        exclude = spec.get("exclude") or []
        hits = [(t, n) for t, n in names.items()
                if any(k in n for k in keywords) and not any(x in n for x in exclude)]
        # 종가가 실제로 있는 것만 남긴다. 신규 상장이라 이력이 짧으면 회귀가 못 돈다.
        hits = [(t, n) for t, n in hits if t in close.columns and close[t].notna().sum() >= 2]
        if not hits:
            print(f"  팩터 '{spec['name']}' 대용 ETF를 찾지 못해 건너뜁니다"
                  f" (키워드 {keywords})")
            continue
        # 수식어(레버리지·헤지형 등)가 붙지 않은 기본 상품일 가능성이 높은 쪽
        ticker, name = min(hits, key=lambda e: len(e[1]))
        resolved[spec["name"]] = {"ticker": ticker, "proxy_name": name, "kind": "etf"}

    panel = pd.DataFrame({fac: close[meta["ticker"]]
                          for fac, meta in resolved.items()})
    return resolved, panel


def market_series(close: pd.DataFrame, snapshot_caps: pd.Series | None = None
                  ) -> pd.Series:
    """시장 수익률 대용 — 시가총액 가중 종합 지수.

    지수 엔드포인트(idx/*)는 필드명을 라이브로 확인하지 못했고 이용신청 상태도
    불확실하다. 확인되지 않은 엔드포인트에 기대는 대신, **이미 받아 둔 전 종목
    패널로 직접 만든다.** KOSPI가 정의상 전 종목 시총가중 지수이므로 이건 대용이
    아니라 재구성에 가깝다. 추가 호출도 0회다.

    가중치가 없으면 동일가중으로 떨어진다. 그건 소형주에 과대 가중이 실려
    시장 요인을 왜곡하므로, 그렇게 될 때는 로그로 알린다.
    """
    ret = close.pct_change()
    if snapshot_caps is None or snapshot_caps.dropna().empty:
        print("  시가총액이 없어 시장 수익률을 동일가중으로 계산합니다"
              " — 소형주 쪽으로 치우칩니다")
        return ret.mean(axis=1)
    w = snapshot_caps.reindex(close.columns).astype(float)
    w = w.where(w > 0).fillna(0.0)
    if w.sum() <= 0:
        return ret.mean(axis=1)
    w = w / w.sum()
    # 결측 종목이 있는 날은 남은 종목들 사이에서 가중치를 다시 정규화한다.
    # 안 하면 상장 전 구간에서 시장 수익률이 통째로 축소된다.
    mask = ret.notna()
    denom = mask.mul(w, axis=1).sum(axis=1)
    return ret.mul(w, axis=1).sum(axis=1).where(denom > 0).div(denom.replace(0, pd.NA))


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
