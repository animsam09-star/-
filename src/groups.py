"""수평 그래프 1: 섹터를 관통하는 종목 그룹 구성.

KRX 테마지수와 섹터·테마 ETF의 구성종목을 그대로 가져온다.
거래소와 운용사가 이미 "같은 동인으로 움직인다"고 판단해 묶어 놓은 바구니이므로,
사전지식으로 작성한 peers 목록보다 정확하고 신규 상장·사업 전환도 자동 반영된다.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pandas as pd
from pykrx import stock

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# 광범위 시장·해외·원자재·파생 ETF는 그룹으로 쓰지 않는다(동인이 특정되지 않음).
EXCLUDE_PATTERNS = re.compile(
    r"미국|차이나|중국|일본|인도|베트남|유럽|글로벌|선진|신흥|아시아|나스닥|S&P|다우|"
    r"달러|엔화|유로|채권|국고채|국채|통안|회사채|금리|CD금리|KOFR|단기자금|머니마켓|"
    r"원유|WTI|골드|금선물|은선물|구리|니켈|농산물|콩|옥수수|천연가스|팔라듐|플래티넘|"
    r"레버리지|인버스|2X|3X|커버드콜|"
    r"코스피200|KRX300|코스닥150|MSCI|고배당|배당성장|가치주|퀄리티|모멘텀|저변동|로우볼",
    re.IGNORECASE,
)

_TICKER_RE = re.compile(r"^\d{6}$")


def _retry(fn, *args, retries=3, delay=1.5, **kwargs):
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(delay * (attempt + 1))


def fetch_theme_groups(base_date: str) -> dict[str, list[str]]:
    """KRX 테마지수별 구성종목."""
    groups: dict[str, list[str]] = {}
    try:
        tickers = _retry(stock.get_index_ticker_list, date=base_date, market="테마")
    except Exception as e:
        print(f"  테마지수 목록 조회 실패: {e}")
        return groups

    for idx in tickers:
        try:
            name = _retry(stock.get_index_ticker_name, idx)
            members = _retry(stock.get_index_portfolio_deposit_file, idx,
                             date=base_date, alternative=True)
        except Exception:
            continue
        members = [m for m in (members or []) if _TICKER_RE.match(str(m))]
        if len(members) >= 3:
            groups[f"테마:{name}"] = members
    return groups


def fetch_etf_groups(base_date: str, min_members: int = 5,
                     max_members: int = 80) -> dict[str, list[str]]:
    """섹터·테마 ETF의 PDF(구성내역)를 그룹으로 사용.

    구성종목 수가 max_members를 넘으면 시장 전체를 담은 광범위 ETF로 보고 제외한다.
    """
    groups: dict[str, list[str]] = {}
    try:
        etfs = _retry(stock.get_etf_ticker_list, date=base_date)
    except Exception as e:
        print(f"  ETF 목록 조회 실패: {e}")
        return groups

    targets = []
    for t in etfs:
        try:
            name = _retry(stock.get_etf_ticker_name, t)
        except Exception:
            continue
        if name and not EXCLUDE_PATTERNS.search(name):
            targets.append((t, name))

    print(f"  ETF {len(etfs)}개 중 {len(targets)}개 후보 → PDF 조회")
    for i, (t, name) in enumerate(targets):
        try:
            pdf = _retry(stock.get_etf_portfolio_deposit_file, t, date=base_date)
        except Exception:
            continue
        if pdf is None or pdf.empty:
            continue
        members = [str(x) for x in pdf.index if _TICKER_RE.match(str(x))]
        if min_members <= len(members) <= max_members:
            groups[f"ETF:{name}"] = members
        if (i + 1) % 50 == 0:
            print(f"  ... PDF {i + 1}/{len(targets)}")
    return groups


def build_groups(base_date: str, universe: set[str] | None = None) -> dict:
    """테마지수 + ETF 그룹을 합쳐 저장하고, 종목→그룹 역인덱스를 함께 반환한다."""
    groups = fetch_theme_groups(base_date)
    groups.update(fetch_etf_groups(base_date))

    if universe:
        groups = {g: [m for m in members if m in universe] for g, members in groups.items()}
        groups = {g: m for g, m in groups.items() if len(m) >= 3}

    # 종목 → 소속 그룹 역인덱스
    membership: dict[str, list[str]] = {}
    for g, members in groups.items():
        for m in members:
            membership.setdefault(m, []).append(g)

    out = {"base_date": base_date, "groups": groups, "membership": membership}
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / f"groups_{base_date}.json").write_text(
        json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"그룹 {len(groups)}개 구성 (종목 {len(membership)}개가 1개 이상 그룹에 소속)")
    return out


def group_returns(close: pd.DataFrame, groups: dict[str, list[str]]) -> pd.DataFrame:
    """그룹별 동일가중 일간 수익률 시계열."""
    rets = close.pct_change()
    data = {}
    for g, members in groups.items():
        cols = [m for m in members if m in rets.columns]
        if len(cols) >= 3:
            data[g] = rets[cols].mean(axis=1)
    return pd.DataFrame(data)
