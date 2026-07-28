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


def fetch_industry_groups(min_members: int = 3,
                          edges: list[dict] | None = None) -> dict[str, list[str]]:
    """산업 노드의 소속 종목을 그룹으로 쓴다.

    테마지수 구성종목과 ETF PDF는 **KRX OpenAPI에 없다**(31개 엔드포인트가 전부
    일별매매정보·종목기본정보·지수시세다). 인증키만 쓰는 구성에서 위의 두 경로는
    통째로 비므로, 수직축이 이미 만들어 둔 `I:*` 노드를 대신 쓴다.

    성격이 다르다는 점은 알고 써야 한다. ETF PDF는 '운용사가 같은 바구니에
    담았다'는 시장의 판단이고, 여기는 '사업보고서가 같은 산업이라고 적었다'는
    사업 실체다. 후자가 테마 순환매를 늦게 반영하는 대신, 근거에 인용이 붙는다.
    """
    from . import graph as G

    if edges is None:
        edges = G.load()
    groups: dict[str, list[str]] = {}
    for e in edges:
        if e.get("rel") != G.REL_MEMBER:
            continue
        src_kind, ticker = G.split_node(e["src"])
        dst_kind, industry = G.split_node(e["dst"])
        if src_kind != "T" or dst_kind != "I":
            continue
        # 같은 소속을 밸류체인과 DART가 각각 주장하면 엣지가 둘이다. 그대로 넣으면
        # 그룹 동일가중 수익률에서 그 종목만 두 번 세어진다.
        members = groups.setdefault(f"산업:{industry}", [])
        if ticker not in members:
            members.append(ticker)
    return {g: m for g, m in groups.items() if len(m) >= min_members}


def build_groups(base_date: str, universe: set[str] | None = None,
                 edges: list[dict] | None = None) -> dict:
    """테마지수 + ETF + 산업 그룹을 합쳐 저장하고, 종목→그룹 역인덱스도 반환한다.

    테마·ETF 경로는 pykrx(웹 로그인) 전용이다. 인증키만 있는 환경에서 호출하면
    매번 실패 로그만 쌓이므로 아예 건너뛴다 — 실패가 아니라 부재다.
    """
    from . import prices

    groups: dict[str, list[str]] = {}
    if prices.source() == "pykrx":
        groups.update(fetch_theme_groups(base_date))
        groups.update(fetch_etf_groups(base_date))
    else:
        print("  테마지수·ETF 구성종목은 KRX OpenAPI에 없습니다 — 산업 그룹만 씁니다")

    industry = fetch_industry_groups(edges=edges)
    print(f"  산업 그룹 {len(industry)}개 (수직축 소속 엣지에서)")
    groups.update(industry)

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


def group_returns(close: pd.DataFrame, groups: dict[str, list[str]],
                  min_members: int = 3) -> pd.DataFrame:
    """그룹별 동일가중 일간 수익률 시계열.

    거래정지·신규상장으로 일부 종목이 비는 날에 남은 한두 종목의 등락이 그룹 전체
    수익률로 잡히면 안 되므로, 유효 종목이 min_members 미만인 날은 결측 처리한다.
    """
    rets = close.pct_change(fill_method=None)
    data = {}
    for g, members in groups.items():
        cols = [m for m in members if m in rets.columns]
        if len(cols) < min_members:
            continue
        sub = rets[cols]
        mean = sub.mean(axis=1)
        data[g] = mean.where(sub.notna().sum(axis=1) >= min_members)
    return pd.DataFrame(data)
