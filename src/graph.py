"""Layer 1 — 관계 그래프.

수직축(밸류체인)과 수평축(공통 동인)을 **하나의 엣지 테이블**에 담는다.
이전 구조에서는 수평축이 티커 기반 정량 데이터, 수직축이 이름뿐인 자유 텍스트라
코드 상에서 만나는 지점이 없었고, 두 축의 결합이 전적으로 LLM의 즉흥 판단이었다.
재현도 채점도 되지 않는다.

## 노드는 세 종류다

    T:005490    종목
    I:시멘트·레미콘   산업·세그먼트
    D:구리 / D:ETF:TIGER 2차전지   동인(매크로 팩터, 테마·섹터 바구니)

## 엣지는 네 종류다

    (T → I, 소속)   이 종목은 이 산업에 속한다
    (I → I, 후방)   dst는 src의 후방(소재·부품·장비 공급)
    (I → I, 전방)   dst는 src의 전방(수요처)
    (T → D, 노출)   이 종목은 이 동인에 노출돼 있다. sign이 부호, weight가 강도(β)

## 왜 회사↔회사가 아니라 이분 그래프인가

회사 쌍으로 저장하면 그룹 하나가 조합 폭발을 일으킨다. 구성종목 80개짜리 ETF는
쌍으로 6,320개 엣지가 되지만, 동인 노드를 경유하면 80개다. 산업도 마찬가지여서
'건설 8개사 × 시멘트 4개사'가 32개가 아니라 12개 + 산업 간 엣지 1개로 끝난다.
DART로 자동 구축할 때 이 차이가 결정적이다.

부수 효과가 더 중요한데, **채점 단위가 엣지가 된다.** "건설 → 시멘트 후방 엣지"가
지금까지 몇 번 맞았는지 누적할 수 있고, 그러면 밸류체인이 고정 지식이 아니라
누적 학습되는 자산이 된다.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GRAPH_DIR = ROOT / "graph"
EDGES_FILE = GRAPH_DIR / "edges.jsonl"

REL_MEMBER = "소속"
REL_UPSTREAM = "후방"
REL_DOWNSTREAM = "전방"
REL_EXPOSURE = "노출"
RELATIONS = (REL_MEMBER, REL_UPSTREAM, REL_DOWNSTREAM, REL_EXPOSURE)

# 엣지 출처. 신뢰도 해석과 사후 채점 분해에 쓴다.
SOURCES = ("valuechain", "dart", "llm", "theme_index", "etf_pdf", "factor_beta", "krx_sector")

_OPPOSITE = {REL_UPSTREAM: REL_DOWNSTREAM, REL_DOWNSTREAM: REL_UPSTREAM}


def ticker_node(ticker: str) -> str:
    return f"T:{ticker}"


def industry_node(name: str) -> str:
    return f"I:{name}"


def driver_node(name: str) -> str:
    return f"D:{name}"


def split_node(node: str) -> tuple[str, str]:
    """'T:005490' → ('T', '005490'). 접두가 없으면 종목으로 본다."""
    kind, sep, value = node.partition(":")
    if not sep or kind not in ("T", "I", "D"):
        return "T", node
    return kind, value


def make_edge(src: str, dst: str, rel: str, source: str, *, sign: int = 0,
              weight: float | None = None, confidence: float = 0.5,
              evidence: str = "", asof: str = "", origin: str = "",
              product: str = "", attribution: str = "") -> dict:
    """관계 하나.

    `product`는 소속 엣지에서 **그 회사가 이 산업에서 실제로 만드는 것**이다.
    산업명만 남기면 HL만도(제동·조향)와 SNT다이내믹스(변속기·차축)와
    가온전선(자동차 전선)이 전부 '자동차 부품·모듈'로 같아 보인다. 셋은 수요
    동인도 경쟁 상대도 다르므로, 하나로 퉁치면 파급 예측이 통째로 뭉개진다.

    `attribution`은 산업 간 관계가 **어느 사업부문의 것인지 확인됐는가**다.
    '추정'은 사업부문이 여럿인 회사인데 공시가 부문을 안 밝혀 최대 매출 산업에
    붙였다는 뜻이다. 이걸 표시하지 않으면 확인된 관계와 구분할 수 없고, 실제로
    HD한국조선해양의 태양광 웨이퍼 매입이 '조선'의 후방으로 붙어 있었다.
    """
    if rel not in RELATIONS:
        raise ValueError(f"알 수 없는 관계 유형: {rel} (허용: {RELATIONS})")
    return {"src": src, "dst": dst, "rel": rel, "sign": int(sign),
            "weight": None if weight is None else round(float(weight), 4),
            "source": source, "origin": origin,
            "confidence": round(float(confidence), 3),
            "evidence": evidence, "asof": asof, "product": product,
            "attribution": attribution}


def edge_key(e: dict) -> tuple:
    """중복 판정 키. 같은 출처가 같은 관계를 다시 주장하면 갱신으로 본다.

    **origin이 키에 들어가는 이유** — source는 'valuechain'이냐 'dart'냐만
    구분한다. 그것만으로 키를 잡으면 서로 다른 회사의 사업보고서가 같은 산업
    관계를 주장했을 때 하나로 합쳐지고, 두 번째 근거가 사라진다.

    그런데 그 두 번째 근거가 바로 DART 방식을 정당화하는 것이다. 시멘트사가
    '매출처=건설'이라 적고 건설사가 '원재료=레미콘'이라 적으면, 같은 관계가
    **독립된 두 문서에서** 나온 것이고 그게 교차 검증이다. 합쳐 버리면
    "한 회사가 그렇다더라"와 구별할 수 없게 된다.
    """
    return (e["src"], e["dst"], e["rel"], e["source"], e.get("origin") or "")


def merge(*edge_lists) -> list[dict]:
    """여러 출처의 엣지를 합친다. 키가 같으면 asof가 최신인 쪽이 이긴다.

    출처가 다르면 같은 관계라도 **둘 다 남긴다.** 밸류체인 YAML과 DART가 같은
    후방 관계를 각각 주장하는 것은 교차 검증이라, 합쳐 버리면 근거가 사라진다.
    """
    best: dict[tuple, dict] = {}
    for edges in edge_lists:
        for e in edges or []:
            k = edge_key(e)
            cur = best.get(k)
            if cur is None or str(e.get("asof", "")) >= str(cur.get("asof", "")):
                best[k] = e
    return sorted(best.values(), key=lambda e: (e["src"], e["rel"], e["dst"], e["source"]))


class Graph:
    def __init__(self, edges: list[dict]):
        self.edges = edges
        self._out: dict[str, list[dict]] = defaultdict(list)
        self._in: dict[str, list[dict]] = defaultdict(list)
        for e in edges:
            self._out[e["src"]].append(e)
            self._in[e["dst"]].append(e)

    def __len__(self) -> int:
        return len(self.edges)

    def out(self, node: str, rel: str | None = None,
            min_confidence: float = 0.0) -> list[dict]:
        return [e for e in self._out.get(node, [])
                if (rel is None or e["rel"] == rel) and e["confidence"] >= min_confidence]

    def into(self, node: str, rel: str | None = None,
             min_confidence: float = 0.0) -> list[dict]:
        return [e for e in self._in.get(node, [])
                if (rel is None or e["rel"] == rel) and e["confidence"] >= min_confidence]

    # ---- 종목 기준 조회 -------------------------------------------------

    def industries_of(self, ticker: str, min_confidence: float = 0.0) -> list[dict]:
        return self.out(ticker_node(ticker), REL_MEMBER, min_confidence)

    def industry_names(self, ticker: str, min_confidence: float = 0.0) -> list[str]:
        """소속 산업명(중복 제거).

        밸류체인과 DART가 같은 소속을 각각 주장하면 엣지가 둘이라, 이름을 그대로
        나열하면 '시멘트·레미콘, 시멘트·레미콘'처럼 찍힌다.
        """
        return list(dict.fromkeys(
            split_node(e["dst"])[1] for e in self.industries_of(ticker, min_confidence)))

    def members_of(self, industry: str, min_confidence: float = 0.0) -> list[str]:
        """산업 소속 종목 티커. 시총 순 정렬은 호출부(Universe 필요)에서 한다."""
        return [split_node(e["src"])[1]
                for e in self.into(industry_node(industry), REL_MEMBER, min_confidence)]

    def drivers_of(self, ticker: str, min_confidence: float = 0.0) -> list[dict]:
        return self.out(ticker_node(ticker), REL_EXPOSURE, min_confidence)

    def exposed_to(self, driver: str, min_confidence: float = 0.0) -> list[dict]:
        """해당 동인에 노출된 엣지들(부호·강도 포함)."""
        return self.into(driver_node(driver), REL_EXPOSURE, min_confidence)

    def peers(self, ticker: str, min_confidence: float = 0.0) -> dict[str, list[str]]:
        """같은 산업에 속한 다른 종목. {산업명: [티커]}"""
        out: dict[str, list[str]] = {}
        for e in self.industries_of(ticker, min_confidence):
            ind = split_node(e["dst"])[1]
            out[ind] = [t for t in self.members_of(ind, min_confidence) if t != ticker]
        return out

    def _vertical(self, ticker: str, rel: str, min_confidence: float) -> list[dict]:
        """종목 → 소속산업 → (후방/전방) 산업 → 그 산업 소속 종목, 2홉 조회.

        경로 신뢰도는 두 엣지 신뢰도의 곱으로 둔다. 소속이 불확실하면 그 위에
        얹힌 밸류체인 추론도 같이 불확실해야 하기 때문이다.

        **같은 (기준산업, 상대산업) 쌍은 하나로 합친다.** 밸류체인 YAML과 DART가
        같은 관계를 각각 주장하면 엣지는 둘 다 남지만(교차 검증의 근거다), 조회
        결과까지 둘로 나오면 프롬프트에 같은 산업이 두 줄로 찍혀 서로 다른 관계인
        것처럼 읽힌다. 신뢰도는 가장 높은 것을 쓰고 출처는 모아서 보여준다.
        """
        merged: dict[tuple[str, str], dict] = {}
        for me in self.industries_of(ticker, min_confidence):
            my_ind = me["dst"]
            for ve in self.out(my_ind, rel, min_confidence):
                target = split_node(ve["dst"])[1]
                key = (split_node(my_ind)[1], target)
                conf = round(me["confidence"] * ve["confidence"], 3)
                cur = merged.get(key)
                if cur is None:
                    merged[key] = {
                        "from_industry": key[0], "relation": rel, "industry": target,
                        "members": self.members_of(target, min_confidence),
                        "confidence": conf, "sources": [ve["source"]],
                        "source": ve["source"], "evidence": ve.get("evidence", ""),
                    }
                    continue
                if ve["source"] not in cur["sources"]:
                    cur["sources"].append(ve["source"])
                if conf > cur["confidence"]:
                    cur.update(confidence=conf, source=ve["source"],
                               evidence=ve.get("evidence", ""))
        return sorted(merged.values(), key=lambda r: -r["confidence"])

    def upstream(self, ticker: str, min_confidence: float = 0.0) -> list[dict]:
        return self._vertical(ticker, REL_UPSTREAM, min_confidence)

    def downstream(self, ticker: str, min_confidence: float = 0.0) -> list[dict]:
        return self._vertical(ticker, REL_DOWNSTREAM, min_confidence)

    def co_exposed(self, ticker: str, min_confidence: float = 0.0) -> list[dict]:
        """같은 동인에 노출된 다른 종목들. 부호가 갈릴 수 있으므로 sign을 함께 준다."""
        out: list[dict] = []
        for me in self.drivers_of(ticker, min_confidence):
            drv = split_node(me["dst"])[1]
            others = [{"ticker": split_node(e["src"])[1], "sign": e["sign"],
                       "weight": e["weight"], "source": e["source"]}
                      for e in self.exposed_to(drv, min_confidence)
                      if split_node(e["src"])[1] != ticker]
            out.append({"driver": drv, "my_sign": me["sign"], "my_weight": me["weight"],
                        "source": me["source"], "others": others})
        return out

    def subgraph(self, ticker: str, min_confidence: float = 0.0) -> dict:
        """해당 종목 하나의 이웃만 뽑는다.

        밸류체인 맵 전체를 프롬프트에 덤프하던 이전 구조는 산업을 늘리면 프롬프트가
        선형으로 커져 확장이 불가능했다. 서브그래프만 넣으면 산업 수와 무관해진다.
        """
        return {
            "ticker": ticker,
            "industries": self.industry_names(ticker, min_confidence),
            "peers": self.peers(ticker, min_confidence),
            "upstream": self.upstream(ticker, min_confidence),
            "downstream": self.downstream(ticker, min_confidence),
            "drivers": self.co_exposed(ticker, min_confidence),
        }


# ---- 프롬프트 렌더 -------------------------------------------------------

def _fmt_members(tickers, universe, gaps=None, limit=8) -> str:
    """시총 큰 순으로 정렬해 '종목명(티커)' 나열. 갭이 있으면 함께 표기한다."""
    ranked = sorted(tickers, key=lambda t: -(universe.market_cap(t) or 0))[:limit]
    parts = []
    for t in ranked:
        label = universe.label(t)
        gap = (gaps or {}).get(t)
        parts.append(f"{label} 갭{gap:+.1%}" if gap is not None else label)
    more = len(tickers) - len(ranked)
    return ", ".join(parts) + (f" 외 {more}종목" if more > 0 else "")


def render_subgraph(graph: "Graph", ticker: str, universe, *, gaps: dict | None = None,
                    min_confidence: float = 0.0, max_members: int = 8,
                    max_drivers: int = 6) -> str:
    """해당 종목의 이웃만 프롬프트 텍스트로 만든다.

    이전 구조는 밸류체인 YAML 전체를 모든 종목 프롬프트에 똑같이 덤프해서,
    조선주를 분석할 때 이차전지 맵까지 들어갔고 산업을 늘리면 프롬프트가 터졌다.
    여기서는 산업 수와 무관하게 크기가 일정하다.
    """
    sub = graph.subgraph(ticker, min_confidence)
    lines: list[str] = []

    if sub["industries"]:
        lines.append(f"### 소속 산업: {', '.join(sub['industries'])}")
    else:
        lines.append("### 소속 산업: 미분류 (밸류체인 맵에 없는 종목)")

    for ind, members in sub["peers"].items():
        if members:
            lines.append(f"\n**동종 — {ind}**")
            lines.append(f"  {_fmt_members(members, universe, gaps, max_members)}")

    for key, title in (("upstream", "후방 — 이 산업에 납품하는 쪽 (물량 전이에 시차 있음)"),
                       ("downstream", "전방 — 이 산업의 수요처")):
        rows = sub[key]
        if not rows:
            continue
        lines.append(f"\n**{title}**")
        for r in rows:
            # 출처가 둘 이상이면 서로 다른 근거가 같은 관계를 지지한다는 뜻이다
            src = "+".join(r.get("sources") or [r["source"]])
            # 소속 종목이 없어도 관계 자체는 보여준다 — 수요·공급 구조가 정보이고,
            # 상장 종목이 없는 산업(예: 석탄)도 원인 해석에는 필요하다
            names = (_fmt_members(r["members"], universe, gaps, max_members)
                     if r["members"] else "(상장 종목 미상)")
            lines.append(f"  - {r['industry']} [신뢰 {r['confidence']:.2f}, {src}]"
                         f"\n      {names}")

    drivers = sorted(sub["drivers"], key=lambda d: -abs(d.get("my_weight") or 0))
    drivers = [d for d in drivers if d["others"]][:max_drivers]
    if drivers:
        lines.append("\n**노출 동인 — 같은 동인에 노출된 종목 (부호 주의)**")
        for d in drivers:
            w = f" β{d['my_weight']:+.2f}" if d.get("my_weight") is not None else ""
            same = [o["ticker"] for o in d["others"] if o["sign"] >= 0]
            opp = [o["ticker"] for o in d["others"] if o["sign"] < 0]
            lines.append(f"  - {d['driver']} — 이 종목 부호 {d['my_sign']:+d}{w}")
            if same:
                lines.append(f"      같은 방향: {_fmt_members(same, universe, gaps, max_members)}")
            if opp:
                lines.append(f"      반대 방향: {_fmt_members(opp, universe, gaps, max_members)}")
    return "\n".join(lines)


# ---- 저장·적재 -----------------------------------------------------------

def save(edges: list[dict], path: Path = EDGES_FILE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(e, ensure_ascii=False, sort_keys=True) for e in edges]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return path


def load(path: Path = EDGES_FILE) -> list[dict]:
    if not Path(path).exists():
        return []
    edges = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            edges.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return edges
