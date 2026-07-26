"""Layer 0 — 종목 마스터.

시스템 전체에서 "이 이름이 어느 종목인가"를 판정하는 단일 진실 원천이다.

**스크리닝 필터와 독립적으로 전 종목을 담는 것이 이 모듈의 존재 이유다.**
이전에는 종목명 인덱스를 `returns_*.json`에서 만들었는데, 그 파일은 시총·거래대금
필터를 통과한 종목만 담는다. 그래서 소형 후방 소재주 — 수직축이 잡아내야 할 바로 그
대상 — 는 티커 해석 자체가 불가능했고, 갭 주입과 사후 채점에서 조용히 빠졌다.
엔티티 레이어가 스크리닝의 부산물이면 안 된다.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from pykrx import stock

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# 개별 종목명 조회는 1건당 1회 요청이라, 일괄 조회가 실패했을 때 전 종목을 개별로
# 긁으면 수천 번 두드리게 된다. 상한을 두고 초과분은 경고로 드러낸다.
MAX_NAME_BACKFILL = 300

_NAME_NOISE = re.compile(r"\s|㈜|\(주\)|（주）|주식회사")


def normalize_name(name: str) -> str:
    """종목명 대조용 정규화. 공백·법인 표기·대소문자 차이를 흡수한다.

    우선주 접미('우', '우B')는 **지우지 않는다.** 보통주와 우선주는 다른 종목이고,
    합치면 엉뚱한 종목에 수혜가 귀속된다.
    """
    return _NAME_NOISE.sub("", str(name)).upper()


def _retry(fn, *args, retries=3, delay=2, **kwargs):
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(delay * (attempt + 1))


class Universe:
    """티커 ↔ 종목명 양방향 인덱스.

    이름 매칭은 정규화 후 **정확 일치까지만** 허용한다. 유사도 매칭은 '한화'와
    '한화솔루션'처럼 실제로 다른 회사를 붙여 버려서, 조용히 빠지는 것보다 나쁘다.
    """

    def __init__(self, base_date: str, entries: dict[str, dict]):
        self.base_date = base_date
        self.entries = entries
        self._by_norm: dict[str, str] = {}
        self.collisions: dict[str, list[str]] = {}

        for ticker, info in entries.items():
            name = info.get("name")
            if not name:
                continue
            norm = normalize_name(name)
            prev = self._by_norm.get(norm)
            if prev is None:
                self._by_norm[norm] = ticker
                continue
            # 정규화 후 이름이 겹치면 시총이 큰 쪽을 대표로 둔다.
            # 어느 쪽을 골라도 절반은 틀리므로, 조용히 넘기지 않고 기록해 드러낸다.
            self.collisions.setdefault(norm, [prev]).append(ticker)
            if (info.get("market_cap") or 0) > (entries[prev].get("market_cap") or 0):
                self._by_norm[norm] = ticker

    def __len__(self) -> int:
        return len(self.entries)

    def __contains__(self, ticker: str) -> bool:
        return ticker in self.entries

    def name(self, ticker: str) -> str | None:
        return (self.entries.get(ticker) or {}).get("name")

    def market_cap(self, ticker: str) -> int | None:
        return (self.entries.get(ticker) or {}).get("market_cap")

    def label(self, ticker: str) -> str:
        """프롬프트·리포트 표기용 '종목명(티커)'."""
        name = self.name(ticker)
        return f"{name}({ticker})" if name else ticker

    def resolve(self, name: str) -> str | None:
        return self._by_norm.get(normalize_name(name))

    def resolve_many(self, names) -> tuple[dict[str, str], list[str]]:
        """이름 목록을 티커로 해석한다. (해석된 것, 실패한 것)을 함께 돌려준다.

        실패를 돌려주지 않으면 커버리지 구멍이 보이지 않는다.
        """
        resolved: dict[str, str] = {}
        unmatched: list[str] = []
        for n in names:
            t = self.resolve(n)
            if t:
                resolved[n] = t
            else:
                unmatched.append(n)
        return resolved, unmatched

    def to_dict(self) -> dict:
        return {"base_date": self.base_date, "entries": self.entries}


def from_entries(base_date: str, entries: dict[str, dict]) -> Universe:
    return Universe(base_date, entries)


def _fetch_market(base_date: str, market: str) -> dict[str, dict]:
    """한 시장의 전 종목 엔트리. 상장 목록을 기준으로 삼고 이름·시총을 붙인다."""
    tickers = _retry(stock.get_market_ticker_list, base_date, market=market)
    if not tickers:
        return {}

    caps: dict[str, int] = {}
    try:
        cap_df = _retry(stock.get_market_cap, base_date, market=market)
        if cap_df is not None and "시가총액" in cap_df.columns:
            caps = {str(t): int(v) for t, v in cap_df["시가총액"].items()}
    except Exception as e:
        print(f"  {market} 시가총액 조회 실패(계속): {e}")

    names: dict[str, str] = {}
    try:
        pc = _retry(stock.get_market_price_change, base_date, base_date, market=market)
        if pc is not None and "종목명" in pc.columns:
            names = {str(t): str(v) for t, v in pc["종목명"].items()}
    except Exception as e:
        print(f"  {market} 종목명 일괄 조회 실패(개별 조회로 대체): {e}")

    # 거래정지 종목은 일괄 조회에서 빠질 수 있어 개별로 메운다
    missing = [t for t in tickers if t not in names]
    if len(missing) > MAX_NAME_BACKFILL:
        print(f"  ::warning:: {market} 종목명 누락 {len(missing)}건 — "
              f"상한 {MAX_NAME_BACKFILL}건만 개별 조회합니다. 나머지는 이름 없이 남습니다.")
    for t in missing[:MAX_NAME_BACKFILL]:
        try:
            names[t] = _retry(stock.get_market_ticker_name, t, retries=2)
        except Exception:
            continue

    return {
        str(t): {"name": names.get(str(t)), "market": market,
                 "market_cap": caps.get(str(t))}
        for t in tickers
    }


def build(base_date: str, use_cache: bool = True) -> Universe:
    """전 종목 마스터를 구성한다(캐시 우선)."""
    if use_cache:
        cached = load(base_date)
        if cached is not None:
            print(f"종목 마스터 캐시 사용: {len(cached)}종목")
            return cached

    entries: dict[str, dict] = {}
    for market in ("KOSPI", "KOSDAQ"):
        entries.update(_fetch_market(base_date, market))

    uni = Universe(base_date, entries)
    named = sum(1 for e in entries.values() if e.get("name"))
    print(f"종목 마스터 {len(uni)}종목 (이름 확보 {named}종목)")
    if uni.collisions:
        sample = list(uni.collisions.items())[:5]
        print(f"  ::warning:: 이름 충돌 {len(uni.collisions)}건 — {sample}")

    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / f"universe_{base_date}.json").write_text(
        json.dumps(uni.to_dict(), ensure_ascii=False), encoding="utf-8")
    return uni


def load(base_date: str) -> Universe | None:
    f = DATA_DIR / f"universe_{base_date}.json"
    if not f.exists():
        return None
    try:
        payload = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    entries = payload.get("entries")
    if not entries:
        return None
    return Universe(payload.get("base_date", base_date), entries)
