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

PERSISTENT_SOURCES = ("valuechain", "dart", "llm")
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
        print(f"  밸류체인 종목명 미해석 {len(flat)}건: {', '.join(flat[:15])}"
              f"{' …' if len(flat) > 15 else ''}")
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

def build_persistent(universe: Universe, asof: str) -> tuple[list[dict], dict]:
    """수직축 엣지를 만들어 graph/edges.jsonl에 저장한다.

    기존 파일과 병합하므로, 이미 쌓인 DART·LLM 엣지는 밸류체인 재빌드로 날아가지
    않는다.
    """
    vc_edges, report = from_valuechain(universe, asof)
    existing = [e for e in G.load() if e.get("source") in PERSISTENT_SOURCES]
    merged = G.merge(existing, vc_edges)
    G.save(merged)
    print(f"수직축 엣지 {len(merged)}개 (밸류체인 {len(vc_edges)}개 반영, "
          f"산업 {len(report['industries'])}개)")
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
