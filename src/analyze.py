"""Layer 2 — 원인 분석 및 파급 추론 (Claude API).

1) 종목별 분석: 뉴스·공시 근거로 상승 원인을 분류하고, 원인이 '산업공통'이면
   **관계 그래프에서 뽑은 해당 종목의 서브그래프**를 근거로 파급 경로를 도출한다.
2) 미반영 체크: 수혜 후보에 티커·수익률·갭을 주입한다.
3) 종합: 산업공통 원인들을 묶어 최종 투자 아이디어를 정리한다.

LLM의 역할이 바뀌었다. 이전에는 밸류체인 맵 전체를 덤프해 주고 후보를 **생성**하게
했다. 지금은 그래프가 후보를 제시하고, LLM은 **선별하고 부호를 판정한다.** 후보 집합이
코드로 결정되므로 재현되고, 엣지 단위로 채점된다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from anthropic import Anthropic

from . import graph as G
from .universe import Universe, normalize_name  # noqa: F401  (이름 정규화 단일 출처)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

CAUSE_TYPES = ["실적서프라이즈", "수주·공급계약", "업황개선(P/Q/C)", "정책·규제",
               "테마·수급", "M&A·지배구조", "무재료·기술적"]

STOCK_SCHEMA = {
    "type": "object",
    "properties": {
        "cause_summary": {"type": "string", "description": "상승 원인 요약 (2~3문장)"},
        "cause_type": {"type": "string", "enum": CAUSE_TYPES},
        "cause_scope": {"type": "string", "enum": ["회사고유", "산업공통"],
                        "description": "원인이 회사 개별 이슈인지, 산업 전체에 적용되는 요인인지"},
        "industry": {"type": "string", "description": "해당 종목의 산업/섹터"},
        "evidence": {"type": "string", "description": "판단 근거가 된 뉴스/공시. 근거가 약하면 '근거 부족' 명시"},
        "confidence": {"type": "string", "enum": ["높음", "중간", "낮음"]},
        "ripple_axis": {
            "type": "string",
            "enum": ["수직", "수평", "양쪽", "해당없음"],
            "description": "수직=밸류체인 물량 전이(수주·계약형, 시차 있음), "
                           "수평=공통 동인 노출(업황·정책·원자재형, 동시 반응). "
                           "회사고유 원인이면 '해당없음'",
        },
        "ripple_paths": {
            "type": "array",
            "description": "산업공통 원인일 때만 채운다. 회사고유면 빈 배열.",
            "items": {
                "type": "object",
                "properties": {
                    "direction": {"type": "string",
                                  "enum": ["동종업계", "전방산업", "후방산업", "공통동인"]},
                    "target_industry": {"type": "string"},
                    "logic": {"type": "string", "description": "파급 논리 (1~2문장)"},
                    "beneficiaries": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "한국거래소 상장사의 정확한 종목명"},
                                "impact": {"type": "string", "enum": ["수혜", "피해"],
                                           "description": "같은 동인이라도 부호가 갈린다. "
                                                          "예: 구리 상승은 제련업체 수혜, 전선업체 피해"},
                                "reason": {"type": "string"},
                            },
                            "required": ["name", "impact", "reason"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["direction", "target_industry", "logic", "beneficiaries"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["cause_summary", "cause_type", "cause_scope", "industry",
                 "evidence", "confidence", "ripple_axis", "ripple_paths"],
    "additionalProperties": False,
}

SYNTHESIS_SCHEMA = {
    "type": "object",
    "properties": {
        "market_summary": {"type": "string", "description": "오늘 후보군에서 관찰된 흐름 요약 (3~4문장)"},
        "ideas": {
            "type": "array",
            "description": "산업공통 원인 기반 투자 아이디어, 매력도 순",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "driver": {"type": "string", "description": "핵심 동인"},
                    "path": {"type": "string", "description": "파급 경로 설명"},
                    "axis": {"type": "string", "enum": ["수직", "수평"],
                             "description": "수직=밸류체인 전이(시차 있음), 수평=공통 동인(동시 반응)"},
                    "beneficiaries": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "impact": {"type": "string", "enum": ["수혜", "피해"]},
                                "priced_in": {"type": "string", "enum": ["미반영", "일부반영", "기반영", "확인불가"]},
                                "comment": {"type": "string"},
                            },
                            "required": ["name", "impact", "priced_in", "comment"],
                            "additionalProperties": False,
                        },
                    },
                    "watch_points": {"type": "string", "description": "확인해야 할 리스크/체크포인트"},
                },
                "required": ["title", "driver", "axis", "path", "beneficiaries", "watch_points"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["market_summary", "ideas"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """당신은 한국 주식시장 파급효과 분석 전문 애널리스트다.

## 파급에는 두 개의 축이 있다

**수직축 (밸류체인)** — 제품·물량이 흐르는 경로. 후방(소재·부품·장비) ↔ 기업 ↔ 전방(수요처).
수주·공급계약처럼 물량이 전이되는 원인에서 작동하며, **시차가 있다**(예: 조선 수주 → 기자재 발주까지 6~12개월).
시차 덕분에 아직 안 오른 종목을 선점할 기회가 크다.

**수평축 (공통 동인)** — 같은 매크로·정책·원자재 동인에 노출된 기업 집합. **섹터를 관통한다.**
예: AI 데이터센터 동인 하나가 반도체·전력기기·건설·공조·전선·원전을 동시에 건드린다.
업황(P/Q/C)·정책·원자재 원인에서 작동하며, **동시에 반응한다.**

원인 유형이 어느 축을 탈지 결정한다. 수주·공급계약이면 수직, 업황·정책·원자재면 수평,
실적 서프라이즈는 그 실적이 판가 때문인지 물량 때문인지 한 겹 더 파고들어 판단하라.

## 원칙

- 뉴스·공시 등 하드 이벤트와 교차 확인되는 원인만 채택한다. 그럴듯한 사후설명을 경계하고,
  근거가 약하면 confidence를 낮추고 evidence에 '근거 부족'을 명시한다.
- 회사 고유 이슈(개별 M&A, 지배구조, 개별 수급)는 파급 예측에 쓰지 않는다.
- **부호를 반드시 판정하라.** 수평축에서는 같은 동인이 섹터마다 방향이 갈린다.
  구리 상승은 비철 제련에 수혜지만 전선에는 원가 부담이고, 환율 상승은 수출주에 수혜지만
  원자재 수입 의존 업종에는 피해다. 무조건 '수혜'로 몰지 마라.
- 수혜 후보는 반드시 한국거래소 상장사의 정확한 종목명으로 제시하고, 노출도가 실질적인
  기업만 꼽는다(매출 비중이 미미한 기업 제외).
- 제공되는 '수평 그래프' 블록은 실제 가격 데이터에서 계산된 것이다. 그룹 소속과 팩터 베타는
  사실이지만, **그 관계의 이유가 무엇인지는 당신이 판단해야 한다.** 상관이 우연일 수 있으니
  사업 내용상 납득되지 않는 연결은 채택하지 마라.
- 갭(gap)이 양수인 종목은 '같이 움직였어야 하는데 아직 안 움직인' 후보다. 우선 검토하되,
  움직이지 않은 데 정당한 이유(사업 노출이 실제로 없음)가 있는지 먼저 확인하라.

## 관계 그래프 사용법

'관계 그래프' 블록은 해당 종목의 이웃만 뽑아 온 것이다. 당신의 역할은 후보를 새로
만들어 내는 것이 아니라 **거기서 골라내고 부호를 판정하는 것**이다.

- 후방/전방에 붙은 '신뢰'는 그 관계의 확실성이다. 낮은 관계는 논리를 더 엄격히 따져라.
- 그래프에 없는 종목을 꼽아도 되지만, 그때는 왜 그래프가 놓쳤는지를 reason에 밝혀라
  (맵 미수록 산업, 신규 사업 진출 등). 근거 없이 그래프 밖 종목을 추가하지 마라.
- 그래프에 있다는 사실은 **연결의 존재**를 뜻할 뿐, 이번 원인이 그 경로를 탄다는
  보장이 아니다. 원인과 무관한 이웃은 버려라."""


def build_horizontal_context(ticker: str, horizontal: dict, names: dict[str, str],
                             max_peers: int = 6) -> str:
    """해당 종목의 그룹 소속·그룹 내 미반영 종목·매크로 팩터 노출을 텍스트로 만든다.

    전부 가격 데이터에서 계산된 사실이며, 해석은 LLM에 맡긴다.
    """
    if not horizontal:
        return "(수평 그래프 데이터 없음)"

    lines: list[str] = []
    membership = horizontal.get("membership", {})
    gaps = horizontal.get("group_gaps", {})
    my_groups = membership.get(ticker, [])

    if my_groups:
        lines.append("### 소속 그룹과 그룹 내 미반영 상위 종목")
        lines.append("(갭 = 기대수익률 − 실제수익률. 양수일수록 아직 안 움직인 종목)")
        for g in my_groups:
            info = gaps.get(g)
            if not info:
                continue
            lines.append(f"\n- {g} — 그룹 최근 5일 {info['group_move']:+.1%}")
            peers = [m for m in info["members"] if m["ticker"] != ticker][:max_peers]
            for m in peers:
                nm = names.get(m["ticker"], m["ticker"])
                flag = "  ← 미반영" if m["gap"] > 0.03 else ""
                lines.append(
                    f"    · {nm}({m['ticker']}) β{m['beta']:+.2f} "
                    f"기대 {m['expected']:+.1%} 실제 {m['actual']:+.1%} "
                    f"갭 {m['gap']:+.1%}{flag}")
    else:
        lines.append("### 소속 그룹: 없음 (테마지수·섹터 ETF 어디에도 편입되지 않음)")

    exposures = horizontal.get("exposures", {}).get(ticker, {})
    moves = horizontal.get("factor_moves", {})
    if exposures:
        lines.append("\n### 매크로 팩터 노출 (시장 요인 제거 후)")
        ranked = sorted(exposures.items(), key=lambda kv: abs(kv[1]["corr"]), reverse=True)
        for fname, e in ranked:
            mv = moves.get(fname, {})
            mv_txt = f" / 팩터 최근 5일 {mv['ret5']:+.1%}" if mv.get("ret5") is not None else ""
            lines.append(f"- {fname}: β {e['beta']:+.2f} (상관 {e['corr']:+.2f}){mv_txt}")
    return "\n".join(lines) if lines else "(수평 그래프 데이터 없음)"


def _extract_json(response) -> dict | None:
    if response.stop_reason == "refusal":
        print("  분석 거부됨(refusal)")
        return None
    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        return None
    return json.loads(text)


def analyze_stock(client: Anthropic, cand: dict, evidence: dict, graph_ctx: str,
                  horizontal_ctx: str, cfg: dict) -> dict | None:
    news = "\n".join(f"- [{n['date']}] {n['title']} — {n['description']}" for n in evidence.get("news", []))
    filings = "\n".join(f"- [{f['date']}] {f['title']}" for f in evidence.get("filings", []))
    prompt = f"""다음 종목의 최근 주가 상승 원인을 분석하고, 산업공통 원인이면
어느 축(수직/수평)으로 파급되는지 판단해 파급 경로와 수혜·피해 후보를 도출하라.

## 종목 정보
- 종목명: {cand['name']} ({cand['ticker']})
- 트리거 유형: {cand['trigger']}
- 수익률: 5일 {cand['ret5']:+.1%} / 20일 {cand['ret20']:+.1%}
- 거래량: 20일 평균 대비 {cand['vol_surge']}배
- 120일 신고가 대비: {cand['high_proximity']:.0%}

## 최근 뉴스
{news or '(수집된 뉴스 없음)'}

## 최근 공시
{filings or '(수집된 공시 없음)'}

## 관계 그래프 — 이 종목의 이웃 (수직축 밸류체인 + 노출 동인)
{graph_ctx}

## 수평 그래프 — 미반영 갭 (가격 데이터에서 계산된 사실)
{horizontal_ctx}"""

    response = client.messages.create(
        model=cfg["model"],
        max_tokens=cfg["max_tokens"],
        system=SYSTEM_PROMPT,
        output_config={"format": {"type": "json_schema", "schema": STOCK_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    return _extract_json(response)


def best_gaps(horizontal: dict) -> dict[str, dict]:
    """종목별 최대 미반영 갭(어느 그룹에서 가장 덜 따라왔는지)."""
    best: dict[str, dict] = {}
    for g, info in (horizontal or {}).get("group_gaps", {}).items():
        for m in info["members"]:
            cur = best.get(m["ticker"])
            if cur is None or m["gap"] > cur["gap"]:
                best[m["ticker"]] = {**m, "group": g, "group_move": info["group_move"]}
    return best


def _iter_beneficiaries(analyses: list[dict] | None, synthesis: dict | None):
    """개별 분석의 파급 경로와 종합 아이디어에 들어 있는 수혜·피해 후보를 모두 순회한다."""
    for a in analyses or []:
        for path in a.get("ripple_paths", []):
            yield from path.get("beneficiaries", [])
    for idea in (synthesis or {}).get("ideas", []):
        yield from idea.get("beneficiaries", [])


def check_priced_in(analyses: list[dict] | None, returns: dict, universe: Universe,
                    horizontal: dict | None = None,
                    synthesis: dict | None = None) -> dict:
    """수혜 후보에 티커·최근 수익률·미반영 갭을 주입하고, 매칭 결과를 집계해 반환한다.

    수익률만으로는 '많이 올랐다'만 알 수 있다. 갭은 '같은 동인에 노출된 정도 대비
    얼마나 덜 움직였는가'라서 미반영 판정이 정량적이 된다.

    **개별 분석과 종합 아이디어 양쪽에 모두 주입해야 한다.** 사후 채점(score.py)은
    종합 아이디어의 티커를 읽으므로, 여기를 빠뜨리면 채점이 항상 0건이 된다.

    이름 해석은 Universe(전 종목 마스터)가 담당한다. 예전에는 스크리닝을 통과한
    종목만 담긴 returns 파일로 인덱스를 만들어서, 소형 후방 소재주는 해석 자체가
    불가능했다 — 수직축이 잡아내야 할 바로 그 대상이 구조적으로 빠져 있었다.
    """
    gaps = best_gaps(horizontal or {})
    matched, unmatched = 0, []

    for b in _iter_beneficiaries(analyses, synthesis):
        ticker = universe.resolve(b.get("name", ""))
        if not ticker:
            unmatched.append(b.get("name", ""))
            continue
        matched += 1
        b["ticker"] = ticker
        if ticker in returns:
            r = returns[ticker]
            b["ret5"] = r.get("ret5")
            b["ret20"] = r.get("ret20")
        if ticker in gaps:
            g = gaps[ticker]
            b["gap"] = g["gap"]
            b["gap_group"] = g["group"]
            b["beta"] = g["beta"]

    total = matched + len(unmatched)
    if unmatched:
        preview = ", ".join(dict.fromkeys(unmatched))[:200]
        print(f"  종목명 매칭 {matched}/{total} — 미매칭: {preview}")
    return {"matched": matched, "total": total, "unmatched": sorted(set(unmatched))}


def reachability(relation_graph: G.Graph, ticker: str,
                 min_confidence: float = 0.0) -> dict[str, str]:
    """해당 종목에서 그래프로 닿는 종목 → 경로 라벨.

    '동종'보다 밸류체인 경로가, 그보다 산업 소속이 정보량이 많으므로 먼저 채운
    라벨을 유지한다(수직 → 수평 순).
    """
    reach: dict[str, str] = {}

    def put(t: str, label: str):
        if t != ticker:
            reach.setdefault(t, label)

    for key, arrow in (("upstream", "후방"), ("downstream", "전방")):
        rows = (relation_graph.upstream(ticker, min_confidence) if key == "upstream"
                else relation_graph.downstream(ticker, min_confidence))
        for r in rows:
            for t in r["members"]:
                put(t, f"{r['from_industry']}→{arrow}→{r['industry']}")
    for ind, members in relation_graph.peers(ticker, min_confidence).items():
        for t in members:
            put(t, f"{ind}→동종")
    for d in relation_graph.co_exposed(ticker, min_confidence):
        for o in d["others"]:
            put(o["ticker"], f"동인:{d['driver']}")
    return reach


def annotate_provenance(beneficiaries_iter, reach: dict[str, str]) -> dict:
    """수혜 후보가 그래프에서 나온 것인지, LLM이 새로 만든 것인지 표시한다.

    이걸 남겨야 사후 채점에서 '그래프 기반 후보'와 'LLM 창작 후보'의 적중률을
    갈라 볼 수 있다. 밸류체인 맵이 실제로 값을 하는지는 그 비교로만 답이 나온다.
    """
    backed = 0
    total = 0
    for b in beneficiaries_iter:
        if not b.get("ticker"):
            continue
        total += 1
        via = reach.get(b["ticker"])
        b["graph_backed"] = via is not None
        if via:
            b["via"] = via
            backed += 1
    return {"graph_backed": backed, "total": total}


def synthesize(client: Anthropic, analyses: list[dict], cfg: dict) -> dict | None:
    common = [a for a in analyses if a.get("cause_scope") == "산업공통"]
    if not common:
        return {"market_summary": "산업공통 요인으로 분류된 상승 종목이 없어 파급 아이디어를 도출하지 않았다.",
                "ideas": []}
    prompt = f"""아래는 오늘 상승 종목들의 개별 분석 결과다(산업공통 원인만 추린 것).
같은 동인은 하나의 아이디어로 묶고, 각 아이디어가 수직축인지 수평축인지 axis에 명시하라.

수혜·피해 후보에 붙은 수치의 의미:
- ret5 / ret20: 최근 수익률. 이미 크게 올랐으면 '기반영'으로 강등한다.
- gap: 기대수익률 − 실제수익률. **미반영 판정의 1순위 근거다.**
  양수가 클수록 같은 동인에 노출됐는데도 아직 안 움직인 종목이다(gap_group이 판정 기준 그룹).
  다만 움직이지 않은 데 정당한 이유(실제 사업 노출이 없음)가 있는지 먼저 따져라.
- beta: 해당 그룹에 대한 민감도. 낮으면 그룹과 무관한 종목일 수 있다.

수평축 아이디어에서는 부호를 반드시 구분하라. 같은 동인이라도 원가로 작용하는 업종은 '피해'다.

{json.dumps(common, ensure_ascii=False, indent=1)}"""

    response = client.messages.create(
        model=cfg["model"],
        max_tokens=cfg["max_tokens"],
        system=SYSTEM_PROMPT,
        output_config={"format": {"type": "json_schema", "schema": SYNTHESIS_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    return _extract_json(response)


def analyze(candidates: list[dict], evidence_all: dict, base_date: str, cfg: dict,
            horizontal: dict | None = None, universe: Universe | None = None,
            relation_graph: G.Graph | None = None) -> dict:
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY 미설정 — 원인 분석을 건너뜁니다.")
        return {"analyses": [], "synthesis": None}
    if universe is None:
        raise ValueError(
            "universe가 필요합니다. 종목명→티커 해석의 단일 출처이며, 없으면 "
            "수혜 후보의 갭 주입과 사후 채점이 통째로 비게 됩니다.")

    client = Anthropic()
    horizontal = horizontal or {}
    relation_graph = relation_graph or G.Graph([])

    returns_file = DATA_DIR / f"returns_{base_date}.json"
    returns = json.loads(returns_file.read_text(encoding="utf-8")) if returns_file.exists() else {}
    names = {t: universe.name(t) for t in universe.entries}
    gap_by_ticker = {t: g["gap"] for t, g in best_gaps(horizontal).items()}

    max_peers = cfg.get("max_peers_per_group", 6)
    min_conf = cfg.get("min_edge_confidence", 0.0)
    analyses = []
    for cand in candidates[: cfg["max_candidates"]]:
        print(f"  분석 중: {cand['name']}")
        graph_ctx = G.render_subgraph(
            relation_graph, cand["ticker"], universe, gaps=gap_by_ticker,
            min_confidence=min_conf,
            max_members=cfg.get("max_members_per_industry", 8),
            max_drivers=cfg.get("max_drivers", 6))
        ctx = build_horizontal_context(cand["ticker"], horizontal, names, max_peers)
        try:
            result = analyze_stock(client, cand, evidence_all.get(cand["ticker"], {}),
                                   graph_ctx, ctx, cfg)
        except Exception as e:
            print(f"  분석 실패({cand['name']}): {e}")
            continue
        if result:
            result["ticker"] = cand["ticker"]
            result["name"] = cand["name"]
            result["trigger"] = cand["trigger"]
            result["ret20"] = cand["ret20"]
            result["groups"] = horizontal.get("membership", {}).get(cand["ticker"], [])
            result["industries"] = relation_graph.industry_names(cand["ticker"])
            analyses.append(result)

    # 종합에 넘기기 전에 개별 분석부터 채워야 갭이 프롬프트 근거로 쓰인다
    check_priced_in(analyses, returns, universe, horizontal)

    # 분석 종목별 그래프 도달 범위를 합쳐 후보의 출처를 표시한다
    reach: dict[str, str] = {}
    for a in analyses:
        for t, via in reachability(relation_graph, a["ticker"], min_conf).items():
            reach.setdefault(t, via)
    annotate_provenance(_iter_beneficiaries(analyses, None), reach)

    try:
        synthesis = synthesize(client, analyses, cfg)
    except Exception as e:
        print(f"종합 분석 실패: {e}")
        synthesis = None

    # 종합 아이디어의 후보에도 주입한다(사후 채점이 이 티커를 읽는다)
    match_stats = check_priced_in(None, returns, universe, horizontal, synthesis)
    prov = annotate_provenance(_iter_beneficiaries(None, synthesis), reach)
    if prov["total"]:
        print(f"  종합 후보 {prov['total']}건 중 그래프 기반 {prov['graph_backed']}건")

    out = {"base_date": base_date, "analyses": analyses, "synthesis": synthesis,
           "name_match": match_stats, "provenance": prov}
    (DATA_DIR / f"analysis_{base_date}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"종목 분석 {len(analyses)}건 완료")
    return out
