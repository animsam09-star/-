"""KRX OpenAPI 데이터 소스 (인증키 방식).

pykrx는 data.krx.co.kr **웹사이트에 로그인**해서 긁는다(POST에 mbrId/pw). 계정이
없거나 아이디가 다르면 통째로 막히고, 네트워크가 제한된 환경에서는 검증조차 못 한다.

KRX OpenAPI(openapi.krx.co.kr에서 발급, 호출은 data-dbg.krx.co.kr)는 인증키 하나로
같은 시세 데이터를 준다. 이 모듈은 그 API를 파이프라인이 기대하는 형태로 바꾼다.

    GET https://data-dbg.krx.co.kr/svc/apis/{category}/{endpoint}
        ?AUTH_KEY={key}&basDd=YYYYMMDD
    → {"OutBlock_1": [{...}, ...]}

하루치 전 종목이 한 번에 오므로 스크리너의 스냅샷 방식과 그대로 맞는다.

## 이 API로 못 하는 것

31개 엔드포인트가 전부 일별매매정보·종목기본정보·지수시세다. **ETF 구성종목(PDF)과
테마지수 구성종목이 없다.** 그게 수평축 그룹(groups.py)의 원천이므로, 인증키만
쓰는 구성에서는 그 경로가 비어 있다. 대안은 DART 산업 노드(`I:*`)의 소속 종목을
그룹으로 쓰는 것인데 **아직 구현하지 않았다.** pykrx 자격 증명이 있으면 기존
경로가 그대로 동작한다.

**거래일 달력 엔드포인트도 없다.** 그래서 날짜를 하루씩 거슬러 올라가며 조회해
'응답이 비었으면 휴장일'로 판정한다(iter_trading_days). 어차피 그 날의 시세가
필요하므로 헛된 호출이 아니고, 응답은 캐시해 재사용한다.

## 필드명 (2026-07-27 라이브 응답으로 확정)

    stk_bydd_trd / ksq_bydd_trd:
      BAS_DD ISU_CD ISU_NM MKT_NM SECT_TP_NM
      TDD_CLSPRC TDD_OPNPRC TDD_HGPRC TDD_LWPRC CMPPREVDD_PRC FLUC_RT
      ACC_TRDVOL ACC_TRDVAL MKTCAP LIST_SHRS

종목명(ISU_NM)과 시가총액(MKTCAP)이 시세 응답에 함께 들어 있다. 그래서 종목
마스터를 만드는 데 종목기본정보(stk_isu_base_info) 엔드포인트가 필요 없다 —
그쪽은 별도 이용신청 대상이라, 안 써도 되는 편이 낫다.

여전히 후보 목록으로 해석한다. 잘못 짚으면 KeyError가 아니라 **빈 컬럼**이 되어
조용히 틀리므로, 실패하면 **실제 필드 목록을 담아 예외**를 올린다.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pandas as pd
import requests

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CACHE_DIR = DATA_DIR / "krx_cache"

BASE_URL = "https://data-dbg.krx.co.kr/svc/apis"
ENV_KEY = "KRX_OPENAPI_KEY"

# (category, endpoint)
EP_KOSPI_OHLCV = ("sto", "stk_bydd_trd")
EP_KOSDAQ_OHLCV = ("sto", "ksq_bydd_trd")
EP_KOSPI_INFO = ("sto", "stk_isu_base_info")
EP_KOSDAQ_INFO = ("sto", "ksq_isu_base_info")
EP_ETF_OHLCV = ("etp", "etf_bydd_trd")
EP_ETN_OHLCV = ("etp", "etn_bydd_trd")
# 지수 — 시장수익률(β_시장)의 원천. '시리즈 일별시세정보'가 이 엔드포인트다.
EP_KOSPI_INDEX = ("idx", "kospi_dd_trd")
EP_KOSDAQ_INDEX = ("idx", "kosdaq_dd_trd")
EP_KRX_INDEX = ("idx", "krx_dd_trd")

MARKET_OHLCV = {"KOSPI": EP_KOSPI_OHLCV, "KOSDAQ": EP_KOSDAQ_OHLCV}
MARKET_INFO = {"KOSPI": EP_KOSPI_INFO, "KOSDAQ": EP_KOSDAQ_INFO}

# 캐논컬 이름 → 응답 필드 후보. 앞에 있을수록 우선.
FIELD_CANDIDATES = {
    "ticker": ["ISU_SRT_CD", "ISU_CD", "SHORT_CD", "ISU_SRT_CODE"],
    "name": ["ISU_ABBRV", "ISU_NM", "ISU_KOR_ABBRV", "ISU_KOR_NM"],
    "close": ["TDD_CLSPRC", "CLSPRC", "TDD_CLSPRC_IDX", "CLSPRC_IDX"],
    "open": ["TDD_OPNPRC", "OPNPRC"],
    "high": ["TDD_HGPRC", "HGPRC"],
    "low": ["TDD_LWPRC", "LWPRC"],
    "volume": ["ACC_TRDVOL", "TRDVOL"],
    "value": ["ACC_TRDVAL", "TRDVAL"],
    "market_cap": ["MKTCAP", "MKT_CAP"],
    "sector": ["IDX_IND_NM", "SECT_TP_NM", "IND_TP_NM", "KRX_IND_NM"],
    "listed_shares": ["LIST_SHRS", "LISTED_SHRS"],
}

_RATE_MIN_INTERVAL = 0.12   # 초당 10회 제한에 여유를 둔다
_last_call = 0.0


class KrxApiError(RuntimeError):
    pass


def _key() -> str:
    key = os.getenv(ENV_KEY)
    if not key:
        raise KrxApiError(
            f"{ENV_KEY} 환경 변수가 설정되지 않았습니다.\n"
            "  openapi.krx.co.kr에서 인증키를 발급받아 등록하세요.\n"
            "  GitHub Actions: Settings → Secrets and variables → Actions")
    return key


def fetch_raw(category: str, endpoint: str, bas_dd: str,
              timeout: int = 30, retries: int = 3) -> list[dict]:
    """한 엔드포인트의 하루치 원본 레코드."""
    global _last_call
    url = f"{BASE_URL}/{category}/{endpoint}"
    params = {"AUTH_KEY": _key(), "basDd": bas_dd}

    last_error: Exception | None = None
    for attempt in range(retries):
        gap = time.monotonic() - _last_call
        if gap < _RATE_MIN_INTERVAL:
            time.sleep(_RATE_MIN_INTERVAL - gap)
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            _last_call = time.monotonic()

            if resp.status_code >= 400:
                # 응답 본문에 KRX가 적어 보낸 사유가 들어 있다. 버리면 원인을
                # 영영 모른 채 상태 코드만 보고 추측하게 된다.
                body = (resp.text or "").strip()[:400]
                if resp.status_code == 401:
                    raise KrxApiError(
                        f"인증 실패(401) — {ENV_KEY}가 유효하지 않습니다.\n"
                        f"  응답: {body}")
                if resp.status_code == 403:
                    raise KrxApiError(
                        f"접근 거부(403) — 키는 인식되지만 '{endpoint}'에 접근할 수 없습니다.\n"
                        f"  응답: {body}\n"
                        "  가능한 원인:\n"
                        "   1. 해당 API 이용신청이 아직 '승인 대기' 상태\n"
                        "   2. 호출 IP 제한 — KRX가 해외/클라우드 IP를 막는 경우\n"
                        "      (GitHub Actions 러너는 해외 IP입니다)\n"
                        "   3. 일일 호출 한도 소진")
                if resp.status_code == 429:
                    raise KrxApiError(f"요청 한도 초과(429)\n  응답: {body}")
                raise KrxApiError(
                    f"HTTP {resp.status_code} — {endpoint}\n  응답: {body}")

            payload = resp.json()
        except KrxApiError:
            raise
        except (requests.RequestException, ValueError) as e:
            last_error = e
            if attempt == retries - 1:
                break
            time.sleep(1.5 * (attempt + 1))
            continue

        if "OutBlock_1" not in payload:
            raise KrxApiError(
                f"{endpoint} 응답에 OutBlock_1이 없습니다. 실제 키: {list(payload)[:10]}")
        return payload["OutBlock_1"] or []

    raise KrxApiError(f"{endpoint} 조회 실패({bas_dd}): {last_error}")


def resolve_field(records: list[dict], canonical: str, required: bool = True) -> str | None:
    """캐논컬 이름을 실제 응답 필드명으로 해석한다.

    못 찾으면 **실제 필드 목록을 담아 예외**를 올린다. 조용히 빈 컬럼을 만들면
    수익률이 전부 NaN이 되고, 그건 예외 없이 틀린 결과로 이어진다.
    """
    if not records:
        return None
    keys = set(records[0])
    for candidate in FIELD_CANDIDATES[canonical]:
        if candidate in keys:
            return candidate
    if not required:
        return None
    raise KrxApiError(
        f"'{canonical}'에 해당하는 필드를 찾지 못했습니다.\n"
        f"  시도한 후보: {FIELD_CANDIDATES[canonical]}\n"
        f"  실제 응답 필드: {sorted(keys)}\n"
        f"  → src/krx_api.py의 FIELD_CANDIDATES에 실제 이름을 추가하세요.")


_STANDARD_CODE = re.compile(r"^KR[A-Z0-9]([0-9]{6})[0-9]{3}$")
_SIX_DIGITS = re.compile(r"^\d{5}[0-9A-Z]$")   # 005930, 종류주 08104K 등


def normalize_ticker(raw) -> str:
    """응답의 종목코드를 6자리 단축코드로 맞춘다.

    KRX는 자리에 따라 단축코드('005930')와 표준코드('KR7005930003')를 섞어 쓴다.
    표준코드가 그대로 인덱스가 되면 예외 없이 **DART·밸류체인과의 조인이 전부
    빗나간다.** 조용히 틀리느니 여기서 형태를 확정한다.

    zfill만 쓰면 안 되는 이유: 표준코드는 12자리라 zfill(6)이 아무 일도 하지 않는다.
    """
    s = str(raw).strip().upper()
    m = _STANDARD_CODE.match(s)
    if m:
        return m.group(1)
    return s.zfill(6)


def to_frame(records: list[dict], fields: list[str],
             optional: tuple[str, ...] = ()) -> pd.DataFrame:
    """원본 레코드를 캐논컬 컬럼명의 DataFrame으로. 인덱스는 티커(문자열)."""
    if not records:
        return pd.DataFrame(columns=fields)
    mapping = {}
    for canonical in fields:
        actual = resolve_field(records, canonical, required=canonical not in optional)
        if actual:
            mapping[canonical] = actual

    df = pd.DataFrame(records)
    out = pd.DataFrame({c: df[a] for c, a in mapping.items()})

    # 수치 컬럼은 문자열로 온다('1,234' 형태 포함). 안 바꾸면 종가 비교가
    # 사전순으로 이뤄져 수익률이 통째로 엉킨다.
    for col in out.columns:
        if col in ("ticker", "name", "sector"):
            continue
        out[col] = pd.to_numeric(
            out[col].astype(str).str.replace(",", "", regex=False).str.strip(),
            errors="coerce")

    if "ticker" in out.columns:
        out["ticker"] = out["ticker"].map(normalize_ticker)
        odd = [t for t in out["ticker"] if not _SIX_DIGITS.match(t)]
        if len(odd) > len(out) * 0.05:
            raise KrxApiError(
                f"종목코드 {len(odd)}/{len(out)}건이 6자리 형태가 아닙니다. "
                f"샘플: {odd[:5]}\n"
                "  → 응답의 코드 체계가 바뀌었을 수 있습니다. "
                "src/krx_api.py의 normalize_ticker를 확인하세요.")
        out = out.set_index("ticker")
    return out


# ---- 파이프라인이 쓰는 조회 --------------------------------------------

def get_market_ohlcv(bas_dd: str, market: str = "KOSPI") -> pd.DataFrame:
    """하루치 전 종목 시세. pykrx.get_market_ohlcv와 같은 자리에 들어간다."""
    category, endpoint = MARKET_OHLCV[market]
    records = fetch_raw(category, endpoint, bas_dd)
    return to_frame(records, ["ticker", "name", "close", "volume", "value", "market_cap"],
                    optional=("name", "market_cap"))


def get_market_base_info(bas_dd: str, market: str = "KOSPI") -> pd.DataFrame:
    """종목기본정보(종목명·업종 등). 종목 마스터와 대체 그룹의 원천."""
    category, endpoint = MARKET_INFO[market]
    records = fetch_raw(category, endpoint, bas_dd)
    return to_frame(records, ["ticker", "name", "sector", "listed_shares"],
                    optional=("sector", "listed_shares"))


def get_etp_ohlcv(bas_dd: str, kind: str = "ETF") -> pd.DataFrame:
    """ETF/ETN 하루치 시세. 매크로 팩터 대용치의 원천이다."""
    category, endpoint = EP_ETF_OHLCV if kind == "ETF" else EP_ETN_OHLCV
    records = fetch_raw(category, endpoint, bas_dd)
    return to_frame(records, ["ticker", "name", "close", "volume", "value"],
                    optional=("volume", "value"))


# ---- 일별 스냅샷 · 거래일 --------------------------------------------

SNAPSHOT_COLUMNS = ["name", "close", "open", "high", "low",
                    "volume", "value", "market_cap", "sector"]


def _cache_path(bas_dd: str) -> Path:
    return CACHE_DIR / f"snapshot_{bas_dd}.json"


def daily_snapshot(bas_dd: str, use_cache: bool = True,
                   markets: tuple[str, ...] = ("KOSPI", "KOSDAQ")) -> pd.DataFrame:
    """하루치 전 종목 스냅샷(KOSPI+KOSDAQ). 휴장일이면 빈 DataFrame.

    응답을 디스크에 캐시한다. 130거래일 패널을 만들려면 260회 넘게 호출해야 하는데,
    실패해서 다시 돌릴 때마다 처음부터 두드리면 한도만 태운다.
    """
    cache = _cache_path(bas_dd)
    if use_cache and cache.exists():
        try:
            records = json.loads(cache.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            records = None
        if records is not None:
            return _snapshot_frame(records)

    records: list[dict] = []
    for market in markets:
        category, endpoint = MARKET_OHLCV[market]
        rows = fetch_raw(category, endpoint, bas_dd)
        for r in rows:
            r.setdefault("_MARKET", market)
        records.extend(rows)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    return _snapshot_frame(records)


def _snapshot_frame(records: list[dict]) -> pd.DataFrame:
    if not records:
        return pd.DataFrame(columns=SNAPSHOT_COLUMNS)
    df = to_frame(records,
                  ["ticker", "name", "close", "open", "high", "low",
                   "volume", "value", "market_cap", "sector"],
                  optional=("name", "open", "high", "low",
                            "market_cap", "sector"))
    df["market"] = [r.get("_MARKET") for r in records][:len(df)]
    # 같은 코드가 두 시장에 동시에 있을 수는 없지만, 재상장 등으로 중복 행이
    # 오면 인덱스가 중복돼 이후 join이 행을 부풀린다.
    return df[~df.index.duplicated(keep="first")]


def iter_trading_days(end_date: str, n_days: int, *, max_lookback: int = 500,
                      use_cache: bool = True, progress_every: int = 20):
    """end_date에서 거슬러 올라가며 (날짜, 스냅샷)을 최신순으로 내놓는다.

    거래일 달력 API가 없으므로 **응답이 비었으면 휴장일**로 본다. 주말은 조회하지
    않는다(호출 낭비). 임시휴장·데이터 지연도 똑같이 '빈 응답'이라 구분되지 않지만,
    어느 쪽이든 그 날은 패널에서 빠지는 게 맞다.
    """
    day = pd.Timestamp(end_date)
    found = 0
    for _ in range(max_lookback):
        if found >= n_days:
            return
        if day.weekday() < 5:      # 월~금만
            bas_dd = day.strftime("%Y%m%d")
            snap = daily_snapshot(bas_dd, use_cache=use_cache)
            if not snap.empty:
                found += 1
                if progress_every and found % progress_every == 0:
                    print(f"  ... 가격 수집 {found}/{n_days}일 ({bas_dd})")
                yield bas_dd, snap
        day -= pd.Timedelta(days=1)

    if found < n_days:
        raise KrxApiError(
            f"{end_date}에서 {max_lookback}일을 거슬러 올라갔으나 거래일이 "
            f"{found}일뿐입니다(요청 {n_days}일).")


def fetch_panel(end_date: str, n_days: int, use_cache: bool = True
                ) -> tuple[list[str], pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """가격 패널을 만든다.

    반환: (거래일 오름차순, close, volume, value, 기준일 스냅샷)
    패널의 index=날짜, columns=티커 — pykrx 경로가 만들던 것과 같은 형태다.
    """
    closes, volumes, values = {}, {}, {}
    latest: pd.DataFrame | None = None
    for bas_dd, snap in iter_trading_days(end_date, n_days, use_cache=use_cache):
        if latest is None:
            latest = snap
        # 거래정지 등으로 종가 0인 행은 제외한다. 0을 그대로 두면 수익률이 -100%가 된다.
        live = snap[snap["close"] > 0]
        closes[bas_dd] = live["close"]
        volumes[bas_dd] = live["volume"]
        values[bas_dd] = live["value"]

    dates = sorted(closes)
    close = pd.DataFrame(closes).T.sort_index()
    volume = pd.DataFrame(volumes).T.sort_index()
    value = pd.DataFrame(values).T.sort_index()
    return dates, close, volume, value, (latest if latest is not None else pd.DataFrame())


def describe_schema(bas_dd: str) -> dict[str, list[str]]:
    """각 엔드포인트의 실제 응답 필드 목록.

    문서 없이 필드명을 단정할 수 없으므로, 첫 라이브 실행에서 이걸 찍어
    FIELD_CANDIDATES를 확정한다. 스모크 워크플로우가 호출한다.
    """
    out: dict[str, list[str]] = {}
    for label, (category, endpoint) in (
        ("KOSPI 시세", EP_KOSPI_OHLCV), ("KOSDAQ 시세", EP_KOSDAQ_OHLCV),
        ("KOSPI 기본정보", EP_KOSPI_INFO), ("ETF 시세", EP_ETF_OHLCV),
        ("ETN 시세", EP_ETN_OHLCV), ("KOSPI 지수", EP_KOSPI_INDEX),
        ("KOSDAQ 지수", EP_KOSDAQ_INDEX), ("KRX 지수", EP_KRX_INDEX),
    ):
        try:
            records = fetch_raw(category, endpoint, bas_dd)
            # 지수는 종목코드가 없고 지수명으로 식별한다. 필드 구성이 시세와
            # 다르므로 샘플 한 건을 그대로 보여 준다.
            out[f"{label} ({endpoint})"] = (
                sorted(records[0]) if records else ["(응답 0건)"])
            if records and category == "idx":
                out[f"{label} 샘플"] = [f"{k}={v}" for k, v in list(records[0].items())[:12]]
        except KrxApiError as e:
            out[f"{label} ({endpoint})"] = [f"조회 실패: {e}"]
    return out
