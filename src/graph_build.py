"""관계 그래프 빌더 — 각 데이터 소스를 엣지로 변환한다.

## 엣지에는 수명이 두 종류다

**영속 엣지** (`graph/edges.jsonl`, 커밋 대상) — 밸류체인·DART·LLM에서 나온 수직축
관계. 느리게 변하고, 사후 채점으로 신뢰도가 누적 갱신되는 자산이다.

**일자별 엣지** (`data/edges_{date}.jsonl`, 비커밋) — 테마지수·ETF PDF·팩터 베타에서
나온 수평축 노출. 매일 재계산되므로 커밋하면 diff만 더럽힌다.

추론 시점에는 둘을 합쳐 하나의 `Graph`로 본다. 같은 테이블에 들어가는 게 핵심이다.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from . import graph as G
from .universe import Universe

ROOT = Path(__file__).resolve().parent.parent
VALUECHAIN_DIR = ROOT / "valuechain"
DATA_DIR = ROOT / "data"

# "profile" — 기사·IR 자료로 채운 종목 프로필. 여기 없으면 매 실행 사라진다.
PERSISTENT_SOURCES = ("valuechain", "dart", "llm", "profile")
DAILY_SOURCES = ("theme_index", "etf_pdf", "factor_beta", "krx_sector")

# 사전지식으로 손으로 쓴 맵이라는 사실을 신뢰도에 반영한다.
# DART 사업보고서에서 뽑은 관계는 이보다 높게 준다.
VALUECHAIN_CONFIDENCE = 0.5
THEME_CONFIDENCE = 0.7      # 거래소 큐레이션
ETF_CONFIDENCE = 0.55       # 운용사 큐레이션 — 부수 편입이 섞인다


def _segments(block) -> list[dict]:
    """upstream/downstream 블록을 [{segment, companies}] 형태로 정규화한다."""
    out = []
    for item in block or []:
        if not isinstance(item, dict):
            continue
        seg = item.get("segment")
        if not seg:
            continue
        out.append({"segment": str(seg), "companies": list(item.get("companies") or [])})
    return out


def from_valuechain(universe: Universe, asof: str,
                    directory: Path = VALUECHAIN_DIR) -> tuple[list[dict], dict]:
    """밸류체인 YAML → 소속·후방·전방 엣지.

    YAML은 사람이 쓰기 좋은 형식이지만 회사가 **이름 문자열**로만 적혀 있다.
    여기서 Universe로 티커를 해석해 붙이는 것이 구조의 요점이다. 티커가 붙어야
    갭 주입·사후 채점·수평축 교차가 전부 코드로 가능해진다.

    해석 실패는 조용히 버리지 않고 리포트로 돌려준다. 그게 커버리지 구멍이다.
    """
    edges: list[dict] = []
    unresolved: dict[str, list[str]] = {}
    industries: set[str] = set()

    def add_members(names, industry: str, origin: str):
        resolved, missing = universe.resolve_many(names)
        if missing:
            unresolved.setdefault(origin, []).extend(missing)
        for name, ticker in resolved.items():
            edges.append(G.make_edge(
                G.ticker_node(ticker), G.industry_node(industry), G.REL_MEMBER,
                "valuechain", confidence=VALUECHAIN_CONFIDENCE,
                evidence=f"{origin}: {name}", asof=asof))

    # '_'로 시작하는 파일은 맵이 아닌 부속 파일(별칭 표 등)이다
    for f in sorted(p for p in directory.glob("*.yaml") if not p.name.startswith("_")):
        try:
            doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            print(f"  밸류체인 파싱 실패 {f.name}: {e}")
            continue

        industry = doc.get("industry")
        if not industry:
            print(f"  ::warning:: {f.name}에 industry가 없어 건너뜁니다")
            continue
        industry = str(industry)
        industries.add(industry)
        origin = f.name

        add_members(doc.get("peers") or [], industry, origin)

        for rel, block in ((G.REL_UPSTREAM, doc.get("upstream")),
                           (G.REL_DOWNSTREAM, doc.get("downstream"))):
            for seg in _segments(block):
                target = seg["segment"]
                industries.add(target)
                # 산업 간 관계는 양방향으로 넣는다. 한 방향만 넣으면 소형 소재주에서
                # 출발했을 때 전방 수요처(건설)를 찾지 못한다 — 수직축이 실제로
                # 잡아내야 하는 방향이 바로 그쪽이다.
                edges.append(G.make_edge(
                    G.industry_node(industry), G.industry_node(target), rel,
                    "valuechain", confidence=VALUECHAIN_CONFIDENCE,
                    evidence=origin, asof=asof))
                edges.append(G.make_edge(
                    G.industry_node(target), G.industry_node(industry),
                    G._OPPOSITE[rel], "valuechain", confidence=VALUECHAIN_CONFIDENCE,
                    evidence=origin, asof=asof))
                add_members(seg["companies"], target, origin)

    report = {
        "industries": sorted(industries),
        "edges": len(edges),
        "unresolved": {k: sorted(set(v)) for k, v in unresolved.items()},
        "unresolved_count": sum(len(set(v)) for v in unresolved.values()),
    }
    if report["unresolved_count"]:
        flat = sorted({n for v in unresolved.values() for n in v})
        print(f"  밸류체인 종목명 미해석 {len(flat)}건:")
        # 이름만 나열하면 원인을 알 수 없다. 사명 변경인지, 비상장인지,
        # 상장폐지인지에 따라 조치가 전혀 다르고, 그 판단 재료가 후보 이름이다.
        suggestions: dict[str, list[str]] = {}
        for n in flat[:25]:
            near = universe.near_misses(n)
            suggestions[n] = near
            print(f"    {n} → " + (", ".join(near) if near
                                   else "상장 종목 중 유사 이름 없음(비상장·상장폐지 가능)"))
        if len(flat) > 25:
            print(f"    … 외 {len(flat) - 25}건")
        report["unresolved_suggestions"] = suggestions
    return edges, report


def from_groups(membership: dict[str, list[str]], asof: str,
                group_gaps: dict | None = None) -> list[dict]:
    """테마지수·ETF 구성종목 → 동인 노출 엣지.

    그룹 소속은 정의상 양(+)의 동조다. 부호가 갈리는 건 매크로 팩터 쪽이다.
    갭 회귀에서 나온 그룹 베타가 있으면 강도로 붙인다.
    """
    beta_by_pair: dict[tuple[str, str], float] = {}
    for g, info in (group_gaps or {}).items():
        for m in info.get("members", []):
            beta_by_pair[(m["ticker"], g)] = m.get("beta")

    edges = []
    for ticker, group_names in (membership or {}).items():
        for g in group_names:
            # '산업:*' 그룹은 여기서 노출 엣지를 만들지 않는다. 그 소속은 이미
            # 수직축의 T→I 소속 엣지로 있고, 여기서 D:산업:철강을 또 만들면 같은
            # 관계가 산업 노드와 동인 노드 두 네임스페이스로 쪼개진다. 그룹은
            # 미반영 갭 회귀에만 쓴다.
            #
            # 접두를 안 보고 넘기면 출처가 'etf_pdf'로 찍혀, ETF 구성종목이라는
            # 근거가 없는 엣지에 ETF 신뢰도가 붙는다.
            if g.startswith("산업:"):
                continue
            source = "theme_index" if g.startswith("테마:") else "etf_pdf"
            conf = THEME_CONFIDENCE if source == "theme_index" else ETF_CONFIDENCE
            edges.append(G.make_edge(
                G.ticker_node(ticker), G.driver_node(g), G.REL_EXPOSURE, source,
                sign=1, weight=beta_by_pair.get((ticker, g)),
                confidence=conf, evidence=g, asof=asof))
    return edges


def from_factor_exposures(exposures: dict, asof: str) -> list[dict]:
    """매크로 팩터 베타 → 부호 있는 노출 엣지.

    **부호가 이 엣지의 존재 이유다.** 구리 상승은 비철 제련에 수혜(+)지만
    전선에는 원가 부담(−)이다. 베타 부호를 그대로 sign에 싣는다.
    신뢰도는 상관의 절대값을 쓴다(팩터 산출 단계에서 이미 하한으로 걸러진 값).
    """
    edges = []
    for ticker, per_factor in (exposures or {}).items():
        for fname, e in (per_factor or {}).items():
            beta = e.get("beta")
            corr = e.get("corr")
            if beta is None or corr is None:
                continue
            edges.append(G.make_edge(
                G.ticker_node(ticker), G.driver_node(fname), G.REL_EXPOSURE,
                "factor_beta", sign=1 if beta >= 0 else -1, weight=beta,
                confidence=min(0.9, abs(float(corr))),
                evidence=f"β{beta:+.2f} (상관 {corr:+.2f})", asof=asof))
    return edges


# ---- 조립 ---------------------------------------------------------------

PROFILES_FILE = VALUECHAIN_DIR / "_company_profiles.yaml"
PROFILE_CONFIDENCE = 0.6      # 공시(0.8~0.85)보다 낮고 손으로 쓴 맵(0.5)보다 높다


def from_profiles(universe: Universe, asof: str,
                  path: Path = PROFILES_FILE) -> tuple[list[dict], dict]:
    """종목 프로필 표 → 품목이 붙은 소속 엣지 + 회사 간 거래 엣지.

    사업보고서를 못 받은 종목의 빈자리를 메운다. 소속 엣지는 **새로 만들지
    않는다** — 어느 산업에 속하는지는 밸류체인 맵이 이미 정했고, 여기서 또
    정하면 같은 종목이 두 경로로 들어와 어느 쪽이 맞는지 알 수 없게 된다.
    이 표가 하는 일은 이미 있는 소속에 '무엇을 만드는가'를 얹는 것뿐이다.
    """
    if not path.exists():
        return [], {"profiles": 0, "unresolved": []}

    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    edges: list[dict] = []
    unresolved: list[str] = []
    for ticker, prof in doc.items():
        ticker = str(ticker).zfill(6)
        if ticker not in universe:
            unresolved.append(f"{ticker}({(prof or {}).get('name') or '?'})")
            continue
        src = (prof or {}).get("source") or ""
        for key, forward in (("customers", True), ("suppliers", False)):
            for other_name in (prof or {}).get(key) or []:
                other = universe.resolve(str(other_name))
                if not other:
                    unresolved.append(f"{ticker}→{other_name}")
                    continue
                if other == ticker:
                    continue
                a, b = (ticker, other) if forward else (other, ticker)
                edges.append(G.make_edge(
                    G.ticker_node(a), G.ticker_node(b), G.REL_DOWNSTREAM, "profile",
                    origin=ticker, asof=asof, confidence=PROFILE_CONFIDENCE,
                    product=" / ".join((prof or {}).get("products") or [])[:80],
                    evidence=src))
    return edges, {"profiles": len(doc), "unresolved": unresolved}


def profile_products(path: Path = PROFILES_FILE) -> dict[str, str]:
    """티커 → 품목 문자열. 공시에 품목이 없는 소속 엣지에 얹는다."""
    if not path.exists():
        return {}
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out = {}
    for ticker, prof in doc.items():
        items = (prof or {}).get("products") or []
        if items:
            out[str(ticker).zfill(6)] = " / ".join(items)[:80]
    return out


def build_persistent(universe: Universe, asof: str) -> tuple[list[dict], dict]:
    """수직축 엣지를 만들어 graph/edges.jsonl에 저장한다.

    기존 파일과 병합하므로, 이미 쌓인 DART·LLM 엣지는 밸류체인 재빌드로 날아가지
    않는다.
    """
    vc_edges, report = from_valuechain(universe, asof)
    prof_edges, prof_report = from_profiles(universe, asof)

    # 회사 간 거래도 **여기서** 만들어 같이 저장한다. 저장된 추출 결과에서
    # 나오는 영속 자산이라 밸류체인·프로필과 성격이 같다.
    #
    # 파이프라인에서 따로 만들어 붙였더니 화면에 하나도 안 나왔다. 여기서 파일을
    # 저장한 **뒤에** 병합했고, 리포트는 메모리가 아니라 저장된 파일을 다시 읽기
    # 때문이다. 만드는 곳과 저장하는 곳이 갈리면 이런 순서 함정이 생긴다.
    from . import company_chain, dart_extract
    co_edges, unlisted = company_chain.from_extractions(
        dart_extract.load_extractions(), universe, asof)

    existing = [e for e in G.load() if e.get("source") in PERSISTENT_SOURCES]
    merged = G.merge(existing, vc_edges, prof_edges, co_edges)

    # 공시에 품목이 없는 소속 엣지에만 프로필 품목을 얹는다. 공시가 이긴다 —
    # 프로필은 기사·IR 자료라 1차 자료가 있으면 그쪽이 맞다.
    products = profile_products()
    filled = 0
    for e in merged:
        if (e["rel"] == G.REL_MEMBER and not e.get("product")
                and e["src"].startswith("T:")):
            what = products.get(e["src"][2:])
            if what:
                e["product"] = what
                filled += 1

    G.save(merged)
    trade_pairs = {(e["src"], e["dst"]) for e in merged
                   if e["src"].startswith("T:") and e["dst"].startswith("T:")}
    report.update(prof_report)
    report["company_trades"] = len(trade_pairs)
    print(f"수직축 엣지 {len(merged)}개 (밸류체인 {len(vc_edges)}개 반영, "
          f"산업 {len(report['industries'])}개)")
    print(f"  회사 간 거래 {len(trade_pairs)}건 "
          f"(공시 {len(co_edges)}엣지 + 프로필 {len(prof_edges)}엣지) / "
          f"비상장·미확인 거래처 {len({u for v in unlisted.values() for u in v})}곳")
    if prof_report["profiles"]:
        print(f"  종목 프로필 {prof_report['profiles']}건 — 품목 {filled}개 보충")
        if prof_report["unresolved"]:
            print(f"  프로필 미해석: {', '.join(prof_report['unresolved'][:8])}")
    return merged, report


def build_daily(horizontal: dict, asof: str) -> list[dict]:
    """수평축 엣지를 만들어 data/edges_{asof}.jsonl에 저장한다."""
    if not horizontal:
        return []
    edges = G.merge(
        from_groups(horizontal.get("membership", {}), asof,
                    horizontal.get("group_gaps")),
        from_factor_exposures(horizontal.get("exposures", {}), asof),
    )
    DATA_DIR.mkdir(exist_ok=True)
    G.save(edges, DATA_DIR / f"edges_{asof}.jsonl")
    print(f"수평축 엣지 {len(edges)}개")
    return edges


def assemble(persistent: list[dict], daily: list[dict]) -> G.Graph:
    """추론 시점의 통합 그래프. 두 축이 여기서 하나가 된다."""
    return G.Graph(G.merge(persistent, daily))
