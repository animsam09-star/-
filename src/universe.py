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
import unicodedata
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# 개별 종목명 조회는 1건당 1회 요청이라, 일괄 조회가 실패했을 때 전 종목을 개별로
# 긁으면 수천 번 두드리게 된다. 상한을 두고 초과분은 경고로 드러낸다.
MAX_NAME_BACKFILL = 300

_NAME_NOISE = re.compile(r"\s|㈜|\(주\)|（주）|주식회사")

# '이름(티커)' 표기. 미해석 목록이 후보를 바로 이 형태로 찍어 주므로, 사람이
# 로그에서 복사해 YAML에 붙여 넣으면 그대로 동작한다.
# 티커 자리는 KRX 단축코드 형태(첫 자리 숫자 + 영숫자 5자)만 받는다 — 사명에
# 들어간 괄호('한국ANKOR유전(1호)' 같은)를 티커로 오인하지 않기 위해서다.
_EXPLICIT_TICKER = re.compile(r"^(.*?)\(([0-9][0-9A-Z]{5})\)$")


def normalize_name(name: str) -> str:
    """종목명 대조용 정규화. 공백·법인 표기·대소문자·문자폭 차이를 흡수한다.

    NFKC를 먼저 거는 이유: KRX 응답과 사람이 쓴 YAML 사이에 **전각/반각** 차이가
    섞일 수 있다. 'ＬＩＧ넥스원'과 'LIG넥스원'은 눈으로는 같지만 코드포인트가
    달라 정확 일치에 실패하고, 그 실패는 예외 없이 조용히 빠진다.

    우선주 접미('우', '우B')는 **지우지 않는다.** 보통주와 우선주는 다른 종목이고,
    합치면 엉뚱한 종목에 수혜가 귀속된다.
    """
    folded = unicodedata.normalize("NFKC", str(name))
    return _NAME_NOISE.sub("", folded).upper()


def _longest_common_substring(a: str, b: str) -> int:
    """두 이름이 공유하는 가장 긴 연속 구간의 길이."""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


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
        """'삼성전자' 또는 '삼성전자(005930)' → 티커.

        괄호 안에 티커를 적으면 이름 대조를 건너뛰고 그 티커를 쓴다. 사명이
        표기마다 갈리는 회사(HD현대미포/현대미포조선/에이치디현대미포)를 이름으로
        맞히려 하면 KRX 표기를 매번 확인해야 하고, 틀리면 조용히 빠진다.
        티커는 사명이 바뀌어도 그대로다.

        적어 둔 티커가 유니버스에 없으면 **해석 실패로 둔다.** 이름으로 되돌아가면
        상장폐지된 티커가 동명 회사에 붙어도 알 수 없다.
        """
        m = _EXPLICIT_TICKER.match(str(name).strip())
        if m:
            return m.group(2) if m.group(2) in self.entries else None
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

    def near_misses(self, name: str, limit: int = 3) -> list[str]:
        """해석 실패한 이름의 후보를 찾는다. **자동으로 붙이지는 않는다.**

        미해석 목록만 던져 놓으면 사람이 원인을 알 수 없다. 사명 변경(현대미포조선
        → HD현대미포)인지, 비상장(세메스)인지, 상장폐지(쌍용C&E)인지에 따라
        조치가 완전히 다른데, 그 판단에 필요한 재료가 바로 '비슷한 이름이 있는가'다.

        유사도 매칭을 해석에 쓰지 않는 이유는 resolve()에 적어 둔 대로다 —
        '한화'와 '한화솔루션'을 붙여 버리면 조용히 빠지는 것보다 나쁘다.
        여기서는 사람이 보는 제안일 뿐이라 안전하다.
        """
        norm = normalize_name(name)
        if len(norm) < 2:
            return []

        # 접두사만 보면 사명 변경을 놓친다: '현대미포조선' → 'HD현대미포'는
        # 첫 글자부터 다르지만 '현대미포'를 공유한다. 실제 미해석 목록에 있던
        # 사례라 최장 공통 부분문자열로 본다.
        min_overlap = max(2, min(len(norm), 4) - 1)
        hits = []
        for other_norm, ticker in self._by_norm.items():
            if other_norm == norm:
                continue
            overlap = _longest_common_substring(norm, other_norm)
            if overlap >= min_overlap:
                contained = norm in other_norm or other_norm in norm
                hits.append((0 if contained else 1, -overlap, other_norm, ticker))
        hits.sort(key=lambda h: (h[0], h[1], abs(len(h[2]) - len(norm))))
        return [f"{self.name(t) or n}({t})" for _, _, n, t in hits[:limit]]

    def to_dict(self) -> dict:
        return {"base_date": self.base_date, "entries": self.entries}


def from_entries(base_date: str, entries: dict[str, dict]) -> Universe:
    return Universe(base_date, entries)


def _fetch_market(base_date: str, market: str) -> dict[str, dict]:
    """한 시장의 전 종목 엔트리(pykrx 경로). 상장 목록을 기준으로 이름·시총을 붙인다."""
    from pykrx import stock

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


def fetch_entries_pykrx(base_date: str) -> dict[str, dict]:
    entries: dict[str, dict] = {}
    for market in ("KOSPI", "KOSDAQ"):
        entries.update(_fetch_market(base_date, market))
    return entries


def _finalize(base_date: str, entries: dict[str, dict]) -> Universe:
    uni = Universe(base_date, entries)
    named = sum(1 for e in entries.values() if e.get("name"))
    print(f"종목 마스터 {len(uni)}종목 (이름 확보 {named}종목)")
    if named < len(uni) * 0.9:
        print(f"  ::warning:: 이름 없는 종목이 {len(uni) - named}건입니다. "
              "이름으로 오는 수혜 후보가 그만큼 티커 해석에 실패합니다.")
    if uni.collisions:
        sample = list(uni.collisions.items())[:5]
        print(f"  ::warning:: 이름 충돌 {len(uni.collisions)}건 — {sample}")

    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / f"universe_{base_date}.json").write_text(
        json.dumps(uni.to_dict(), ensure_ascii=False), encoding="utf-8")
    return uni


def save_from_snapshot(base_date: str, snapshot) -> Universe:
    """스크리너가 이미 받아 둔 기준일 스냅샷으로 마스터를 만든다.

    시세 응답에 종목명·시가총액이 함께 오므로 **추가 조회가 0회**다. 스크리닝
    필터를 적용하기 전의 스냅샷을 넘겨야 한다는 점이 중요하다 — 필터 후를 넘기면
    이 모듈이 존재하는 이유(소형 후방주 해석)가 사라진다.
    """
    from . import prices
    return _finalize(base_date, prices.entries_from_snapshot(snapshot, base_date))


def build(base_date: str, use_cache: bool = True) -> Universe:
    """전 종목 마스터를 구성한다(캐시 우선).

    보통은 스크리너가 save_from_snapshot으로 이미 만들어 둔 캐시를 읽는다.
    캐시가 없을 때만 직접 조회한다.
    """
    if use_cache:
        cached = load(base_date)
        if cached is not None:
            print(f"종목 마스터 캐시 사용: {len(cached)}종목")
            return cached

    from . import krx_api, prices
    if prices.source() == "openapi":
        snap = krx_api.daily_snapshot(base_date)
        if snap.empty:
            raise RuntimeError(f"{base_date}는 거래일이 아니거나 응답이 비었습니다.")
        return save_from_snapshot(base_date, snap)
    return _finalize(base_date, fetch_entries_pykrx(base_date))


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
