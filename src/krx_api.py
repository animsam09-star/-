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

31개 엔드포인트가 전부 일별매매정보와 종목기본정보다. **ETF 구성종목(PDF)과
지수 구성종목이 없다.** 그게 수평축 그룹의 원천이었으므로, OpenAPI만 쓰는 구성에서는
그룹을 DART 산업 노드에서 만든다(graph_build.groups_from_industries).

## 필드명을 하드코딩하지 않는 이유

응답 필드는 영문 대문자 코드(BAS_DD, ISU_CD, TDD_CLSPRC …)인데, 정확한 이름을
문서 없이 단정할 수 없다. 잘못 짚으면 KeyError가 아니라 **빈 컬럼**이 되어 조용히
틀린다. 그래서 후보 목록으로 해석하고, 실패하면 **실제 필드 목록을 담아 예외**를
올린다. 첫 라이브 실행이 스키마를 알려주는 구조다.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pandas as pd
import requests

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

BASE_URL = "https://data-dbg.krx.co.kr/svc/apis"
ENV_KEY = "KRX_OPENAPI_KEY"

# (category, endpoint)
EP_KOSPI_OHLCV = ("sto", "stk_bydd_trd")
EP_KOSDAQ_OHLCV = ("sto", "ksq_bydd_trd")
EP_KOSPI_INFO = ("sto", "stk_isu_base_info")
EP_KOSDAQ_INFO = ("sto", "ksq_isu_base_info")
EP_ETF_OHLCV = ("etp", "etf_bydd_trd")
EP_ETN_OHLCV = ("etp", "etn_bydd_trd")

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
            if resp.status_code == 401:
                raise KrxApiError(
                    f"인증 실패(401) — {ENV_KEY}가 유효하지 않거나 "
                    f"'{endpoint}' 서비스 이용 신청이 되어 있지 않습니다.\n"
                    "  openapi.krx.co.kr에서 API별로 별도 신청이 필요합니다.")
            if resp.status_code == 429:
                raise KrxApiError("요청 한도 초과(429)")
            resp.raise_for_status()
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
    if "ticker" in out.columns:
        # 티커는 반드시 6자리 문자열이어야 한다. 숫자로 변환되면 앞자리 0이 사라져
        # '005930'이 5930이 되고, 다른 소스와의 조인이 통째로 어긋난다.
        out["ticker"] = out["ticker"].astype(str).str.strip().str.zfill(6)
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


def describe_schema(bas_dd: str) -> dict[str, list[str]]:
    """각 엔드포인트의 실제 응답 필드 목록.

    문서 없이 필드명을 단정할 수 없으므로, 첫 라이브 실행에서 이걸 찍어
    FIELD_CANDIDATES를 확정한다. 스모크 워크플로우가 호출한다.
    """
    out: dict[str, list[str]] = {}
    for label, (category, endpoint) in (
        ("KOSPI 시세", EP_KOSPI_OHLCV), ("KOSDAQ 시세", EP_KOSDAQ_OHLCV),
        ("KOSPI 기본정보", EP_KOSPI_INFO), ("ETF 시세", EP_ETF_OHLCV),
        ("ETN 시세", EP_ETN_OHLCV),
    ):
        try:
            records = fetch_raw(category, endpoint, bas_dd)
            out[f"{label} ({endpoint})"] = sorted(records[0]) if records else []
        except KrxApiError as e:
            out[f"{label} ({endpoint})"] = [f"조회 실패: {e}"]
    return out
