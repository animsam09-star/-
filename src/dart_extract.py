"""DART 사업보고서 → 수직축 엣지 (Claude 구조화 추출).

밸류체인 YAML이 내 사전지식이라면, 이 모듈은 **1차 자료**에서 같은 관계를
뽑아낸다. 같은 엣지 테이블에 `source: "dart"`로 들어가고 YAML 엣지와 병합되지
않으므로, 둘이 같은 관계를 주장하면 교차 검증이 된다.

## 환각을 죽이는 장치: 인용 검증

사업보고서를 LLM에 넣고 "후방 산업이 뭐냐"고 물으면, 문서에 없어도 그럴듯한
답이 나온다. 그럴듯하기 때문에 사람 눈으로는 안 걸러진다.

그래서 **모든 관계에 원문 인용을 의무화하고, 코드가 그 인용이 실제로 문서에
있는지 대조한다.** 없으면 그 관계는 버린다. "LLM을 믿는다"가 "부분 문자열이
있는지 확인한다"로 바뀌는 것이 요점이다. 폐기율은 그대로 환각률의 하한이라
로그로 남긴다.

## 그래프를 조각내지 않기: 어휘 통제

산업명을 자유롭게 쓰게 두면 '시멘트', '시멘트·레미콘', '시멘트 제조업'이 서로
다른 노드가 되고 그래프가 조용히 파편화된다. 기존 산업 목록을 프롬프트에 주고,
받은 뒤에도 정규화·별칭으로 한 번 더 접는다. 그래도 새 산업이면 새로 만들되
**신규 목록을 보고해** 사람이 별칭을 정리할 수 있게 한다.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path

import yaml
from anthropic import Anthropic

from . import dart
from . import llm
from . import graph as G
from . import graph_build

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
ALIAS_FILE = ROOT / "valuechain" / "_industry_aliases.yaml"

# 근거 등급 → 신뢰도. 전부 밸류체인 YAML(0.5)보다 높다 — 1차 자료이기 때문이다.
TIER_CONFIDENCE = {"A": 0.85, "B": 0.70, "C": 0.55}

# 인용 길이 하한. 짧은 인용은 우연히 일치해 검증을 무력화하기 때문에 둔다.
# 다만 표 행("레미콘 | 38.0%")은 짧으면서도 가장 강한 근거라, 숫자를 포함하면
# 하한을 낮춘다. 산문 하한을 그대로 적용하면 등급 A 근거가 통째로 폐기된다.
MIN_QUOTE_CHARS = 10
MIN_NUMERIC_QUOTE_CHARS = 6

_RELATION = {"upstream": G.REL_UPSTREAM, "downstream": G.REL_DOWNSTREAM}

_IND_NOISE = re.compile(r"[\s·,/\-—()·]")
_IND_SUFFIX = re.compile(r"(?:제조업|사업부문|사업부|산업|부문|사업|업계)$")
_WS = re.compile(r"\s+")


def _nullable_number():
    # 구조화 출력 스키마는 타입 배열보다 anyOf가 안전하다
    return {"anyOf": [{"type": "number"}, {"type": "null"}]}


def _relation_items(extra: dict[str, dict]) -> dict:
    props = {
        "industry": {"type": "string", "description": "상대 산업명"},
        "quote": {"type": "string",
                  "description": "이 관계의 근거가 되는 원문 문장 (그대로 복사)"},
        "tier": {"type": "string", "enum": ["A", "B", "C"],
                 "description": "A=표에서 비중까지 확인, B=본문 서술, C=제품명에서 추론"},
        **extra,
    }
    return {"type": "array", "items": {
        "type": "object", "properties": props,
        "required": list(props), "additionalProperties": False}}


EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "products": _relation_items({
            "product": {"type": "string", "description": "해당 산업으로 분류한 제품·서비스명"},
            "revenue_share": {**_nullable_number(),
                              "description": "매출 비중(0~1). 문서에 없으면 null"},
        }),
        # segment — 이 관계가 **어느 사업부문의 것인가**. 없으면 회사의 최대 매출
        # 산업에 붙일 수밖에 없는데, 사업부문이 여럿인 회사에서는 그게 대개 틀린다.
        # 실제로 HD한국조선해양의 태양광 웨이퍼 매입이 '조선'의 후방으로 붙어
        # '실리콘 웨이퍼 → 조선'이라는 없는 관계가 생겼다.
        "upstream": _relation_items({
            "material": {"type": "string", "description": "매입하는 원재료·부품명"},
            "segment": {"type": "string",
                        "description": "이 원재료를 쓰는 사업부문의 산업명. "
                                       "products에 적은 industry 중 하나여야 한다. "
                                       "부문을 특정할 수 없으면 빈 문자열"},
            "cost_share": {**_nullable_number(),
                           "description": "매입액 비중(0~1). 문서에 없으면 null"},
        }),
        "downstream": _relation_items({
            "customer": {"type": "string",
                         "description": "매출처 유형. 익명이면 '건설사'처럼 업종으로 적는다"},
            "segment": {"type": "string",
                        "description": "이 매출처에 파는 사업부문의 산업명. "
                                       "products에 적은 industry 중 하나여야 한다. "
                                       "부문을 특정할 수 없으면 빈 문자열"},
        }),
        "unmapped": {"type": "string",
                     "description": "문서에 있으나 산업으로 분류하기 어려웠던 내용. 없으면 빈 문자열"},
    },
    "required": ["products", "upstream", "downstream", "unmapped"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """너는 한국 상장기업의 사업보고서에서 밸류체인 관계를 추출하는 애널리스트다.
목적은 '이 기업이 속한 산업'과 '그 산업의 후방·전방 산업'을 1차 자료로 확정하는 것이다.

## 절대 규칙: 인용

모든 항목에 quote를 채워라. quote는 **제공된 문서에서 그대로 복사한 연속된 문장**이어야
한다. 요약하거나 다시 쓰지 마라. 시스템이 원문과 대조해 일치하지 않는 항목을 전부
폐기하므로, 지어내면 그 항목은 버려진다. 근거를 못 찾으면 그 항목을 아예 넣지 마라.
적게 넣고 정확한 편이 많이 넣고 틀린 것보다 낫다.

표에서 인용할 때는 **항목명과 수치를 한 줄로 함께** 복사하라(예: `레미콘 | 38.0%`).
수치만 떼어 오면 근거로 인정되지 않는다.

## 무엇을 뽑는가

- **products**: 이 회사가 파는 것 → 이 회사가 속한 산업. '주요 제품 및 서비스',
  '매출실적', 사업부문별 매출에서 찾는다. 매출 비중이 표에 있으면 revenue_share에 적는다.
- **upstream**: 이 회사가 사는 것 → 후방 산업. '주요 원재료', '원재료 매입 현황'에서
  찾는다. 원재료명을 그 원재료를 만드는 **산업**으로 옮겨라
  (예: 원재료 '열연강판' → 산업 '철강').
- **downstream**: 이 회사가 파는 상대 → 전방 산업. '매출 및 수주상황', '주요 매출처'.

## 매출처가 익명이어도 괜찮다

사업보고서의 거래처는 대개 익명이다("A사", "국내 대형 건설사"). **회사 이름은 필요 없다.**
필요한 것은 업종이다. "국내 건설사에 납품"이면 downstream 산업은 '건설'이다.
익명이라는 이유로 항목을 버리지 마라 — 업종만 식별되면 충분하다.

## 산업명은 주어진 목록에서 고른다

아래 '기존 산업 목록'에 맞는 이름이 있으면 **글자 그대로** 재사용하라. 목록에 없는
산업일 때만 새 이름을 쓰되, 가장 일반적인 표기를 택하라. 같은 산업을 다른 이름으로
부르면 그래프가 조각난다.

## 등급

- A: 표에서 비중 수치까지 확인됨
- B: 본문 서술에서 명확히 확인됨(수치 없음)
- C: 제품·원재료명에서 산업을 추론함

## 절 구성은 산업마다 다르다

건설사에는 '원재료 및 생산설비'가 아예 없고, 제조업에는 '수주상황'이 없다.
**없는 항목은 빈 배열로 두어라.** 스키마에 자리가 있다고 채우려 하지 마라.
한쪽이 비어도 상대 산업의 보고서에서 같은 관계가 나오므로 손실이 아니다
(시멘트사의 '매출처=건설업체'가 건설의 후방 관계를 만든다).

## 하지 말 것

- 문서에 없는 일반 상식으로 관계를 채우지 마라. 사전지식으로 아는 관계라도
  이 문서에 근거가 없으면 넣지 마라.
- 단순 소모품·용역(전기료, 운반비, 사무용품)은 upstream에 넣지 마라.
- 지주회사의 단순 지분 관계를 밸류체인으로 취급하지 마라."""


# ---- 어휘 통제 -----------------------------------------------------------

def normalize_industry(name: str) -> str:
    """대조용 정규화. 접미를 떼되 2글자 미만으로 줄지 않게 한다."""
    s = _IND_NOISE.sub("", str(name))
    prev = None
    while prev != s:
        prev = s
        m = _IND_SUFFIX.search(s)
        if m and len(s) - len(m.group()) >= 2:
            s = s[:m.start()]
    return s.upper()


def known_industries(existing_edges: list[dict]) -> list[str]:
    """추출 프롬프트에 줄 산업 어휘.

    엣지 파일에서만 읽으면 안 된다 — graph/edges.jsonl은 파이프라인이 한 번
    돌아야 생기는 산출물이라, 새 클론이나 첫 실행에서는 비어 있다. 그러면
    어휘 통제가 조용히 꺼진 채로 추출이 돌아 '시멘트'와 '시멘트 제조업'이
    각각 노드가 된다. 원천인 밸류체인 YAML을 함께 읽어 그 구멍을 막는다.
    """
    names = {G.split_node(e["dst"])[1] for e in existing_edges
             if e["rel"] in (G.REL_MEMBER, G.REL_UPSTREAM, G.REL_DOWNSTREAM)}
    for f in sorted(p for p in graph_build.VALUECHAIN_DIR.glob("*.yaml")
                    if not p.name.startswith("_")):
        try:
            doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        if doc.get("industry"):
            names.add(str(doc["industry"]))
        for block in (doc.get("upstream"), doc.get("downstream")):
            for seg in graph_build._segments(block):
                names.add(seg["segment"])
    return sorted(n for n in names if n)


def load_aliases() -> dict[str, str]:
    if not ALIAS_FILE.exists():
        return {}
    try:
        data = yaml.safe_load(ALIAS_FILE.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    return {str(k): str(v) for k, v in data.items() if k and v}


class IndustryVocab:
    """산업명을 기존 어휘로 접는다. 접히지 않은 것은 신규로 보고한다."""

    def __init__(self, known: list[str], aliases: dict[str, str] | None = None):
        self.known = list(known)
        self.aliases = aliases or {}
        self._by_norm = {normalize_industry(k): k for k in reversed(self.known)}
        self.new: dict[str, int] = {}

    def resolve(self, name: str) -> str:
        raw = str(name).strip()
        if not raw:
            return raw
        target = self.aliases.get(raw, raw)
        if target in self._by_norm.values() or target in self.known:
            return target
        hit = self._by_norm.get(normalize_industry(target))
        if hit:
            return hit
        self.new[target] = self.new.get(target, 0) + 1
        self._by_norm[normalize_industry(target)] = target
        self.known.append(target)
        return target

    def alias_suggestions(self, min_len: int = 2) -> dict[str, list[str]]:
        """신규 산업 → 같은 것일 수 있는 기존 산업 후보.

        '건설'과 '건설·EPC'처럼 한쪽이 다른 쪽의 접두인 경우를 잡는다.
        **자동으로 병합하지는 않는다** — '반도체'와 '반도체 장비'는 접두 관계지만
        엄연히 다른 산업이라, 자동 병합은 조용한 오류를 만든다. 사람이 고르도록
        후보만 내놓는다.
        """
        established = [k for k in self.known if k not in self.new]
        out: dict[str, list[str]] = {}
        for fresh in self.new:
            nf = normalize_industry(fresh)
            if len(nf) < min_len:
                continue
            hits = [k for k in established
                    if (nk := normalize_industry(k)) != nf
                    and (nk.startswith(nf) or nf.startswith(nk))]
            if hits:
                out[fresh] = hits
        return out


# ---- 인용 검증 -----------------------------------------------------------

def _squash(s: str) -> str:
    return _WS.sub("", str(s))


def verify_quote(quote: str, source: str, min_chars: int = MIN_QUOTE_CHARS) -> bool:
    """인용이 원문에 실제로 있는가.

    공백은 무시한다 — 표를 읽으면서 줄바꿈이 달라질 수 있고, 그건 환각이 아니다.
    너무 짧은 인용은 우연히 일치하므로 하한을 두되, 숫자를 포함한 인용은
    우연 일치 가능성이 훨씬 낮으므로 하한을 낮춘다(표 행 근거를 살리기 위함).
    """
    q = _squash(quote)
    if not q:
        return False
    floor = MIN_NUMERIC_QUOTE_CHARS if any(c.isdigit() for c in q) else min_chars
    if len(q) < floor:
        return False
    return q in _squash(source)


def filter_by_quote(extraction: dict, source: str) -> tuple[dict, dict]:
    """인용이 검증되지 않은 항목을 걷어낸다. (남은 것, 폐기 통계)"""
    kept: dict[str, list] = {}
    stats = {"kept": 0, "dropped": 0, "dropped_samples": []}
    for key in ("products", "upstream", "downstream"):
        rows = []
        for item in extraction.get(key) or []:
            if verify_quote(item.get("quote", ""), source):
                rows.append(item)
                stats["kept"] += 1
            else:
                stats["dropped"] += 1
                if len(stats["dropped_samples"]) < 5:
                    stats["dropped_samples"].append(
                        f"{key}:{item.get('industry')} — {str(item.get('quote'))[:60]}")
        kept[key] = rows
    kept["unmapped"] = extraction.get("unmapped", "")
    return kept, stats


# ---- 엣지 변환 -----------------------------------------------------------

def _tier_conf(item: dict) -> float:
    return TIER_CONFIDENCE.get(item.get("tier", "C"), 0.55)


def _fold(items: list[dict], vocab: IndustryVocab, share_key: str | None) -> dict[str, dict]:
    """산업명을 어휘로 접은 뒤 같은 산업끼리 합친다.

    별칭 때문에 '레미콘'과 '시멘트·레미콘'이 같은 산업이 되는 일이 흔한데,
    합치지 않으면 같은 엣지가 두 번 생기고 비중은 둘 중 하나만 임의로 남는다.
    한 산업 안의 서로 다른 제품이므로 **비중은 더한다.**
    신뢰도와 인용은 근거가 가장 강한(등급이 높은) 항목의 것을 쓴다.

    제품명은 **합치지 않고 모은다.** 같은 산업으로 접혔다는 건 분류가 같다는
    뜻일 뿐, 만드는 물건이 같다는 뜻이 아니다. 한 회사가 '자동차 부품·모듈'
    안에서 변속기와 차축을 따로 적었다면 둘 다 남아야 한다.
    """
    folded: dict[str, dict] = {}
    for item in items or []:
        industry = vocab.resolve(item.get("industry", ""))
        if not industry:
            continue
        share = item.get(share_key) if share_key else None
        share = float(share) if isinstance(share, (int, float)) else None
        conf = _tier_conf(item)
        # 후방은 'material', 전방은 'customer'로 들어온다. 셋 다 '이 관계에서
        # 실제로 오가는 물건'이라 같은 자리에 담는다.
        what = str(item.get("product") or item.get("material")
                   or item.get("customer") or "").strip()
        seg = str(item.get("segment") or "").strip()
        cur = folded.get(industry)
        if cur is None:
            folded[industry] = {"industry": industry, "share": share,
                                "confidence": conf, "quote": item.get("quote", ""),
                                "products": [what] if what else [],
                                "segments": {seg} if seg else set()}
            continue
        if share is not None:
            cur["share"] = share if cur["share"] is None else cur["share"] + share
        if conf > cur["confidence"]:
            cur["confidence"], cur["quote"] = conf, item.get("quote", "")
        if what and what not in cur["products"]:
            cur["products"].append(what)
        if seg:
            cur["segments"].add(seg)
    return folded


def to_edges(ticker: str, extraction: dict, vocab: IndustryVocab,
             asof: str, evidence_prefix: str = "") -> list[dict]:
    """검증을 통과한 추출 결과를 엣지로 바꾼다.

    **종목명→티커 해석이 필요 없다.** 공시는 그 회사 자신의 것이라 티커를 이미
    알고, 상대는 회사가 아니라 산업으로만 표현된다. 익명화가 문제가 되지 않는
    이유이자, YAML 경로보다 이쪽이 구조적으로 견고한 이유다.
    """
    edges: list[dict] = []
    products = _fold(extraction.get("products") or [], vocab, "revenue_share")

    for p in products.values():
        edges.append(G.make_edge(
            G.ticker_node(ticker), G.industry_node(p["industry"]), G.REL_MEMBER, "dart",
            weight=p["share"], confidence=p["confidence"], origin=ticker,
            product=" / ".join(p["products"])[:80],
            evidence=f"{evidence_prefix}{str(p['quote'])[:150]}", asof=asof))

    if not products:
        return edges
    # 기준 산업 = 매출 비중이 가장 큰 산업. 비중이 없으면 첫 항목.
    with_share = [p for p in products.values() if p["share"] is not None]
    primary = (max(with_share, key=lambda p: p["share"])["industry"] if with_share
               else next(iter(products)))

    for key, rel in _RELATION.items():
        share_key = "cost_share" if key == "upstream" else None
        for item in _fold(extraction.get(key) or [], vocab, share_key).values():
            target = item["industry"]
            # 관계의 **출발 산업**은 그 관계가 속한 사업부문이다. 공시가 부문을
            # 밝혔고 그게 이 회사의 제품 산업 중 하나면 그걸 쓴다. 없으면 최대
            # 매출 산업으로 떨어지는데, 사업부문이 여럿이면 그건 추정일 뿐이라
            # 표시를 남긴다 — 표시가 없으면 추정과 확인을 구분할 수 없다.
            segs = [s for s in (vocab.resolve(s) for s in item["segments"]) if s in products]
            if len(segs) == 1:
                anchor, attribution = segs[0], "부문확인"
            else:
                anchor = primary
                attribution = "추정" if len(products) > 1 else ""
            if target == anchor:
                continue
            ev = f"{evidence_prefix}{str(item['quote'])[:150]}"
            what = " / ".join(item["products"])[:80]
            # 산업 간 관계는 양방향 — 소형 소재주에서 전방을 찾을 수 있어야 한다
            # origin=ticker — 어느 회사 공시에서 나왔는지가 키에 들어가야
            # 같은 관계를 두 회사가 각각 주장한 것이 교차 검증으로 남는다.
            edges.append(G.make_edge(G.industry_node(anchor), G.industry_node(target),
                                     rel, "dart", weight=item["share"], origin=ticker,
                                     product=what, attribution=attribution,
                                     confidence=item["confidence"], evidence=ev, asof=asof))
            edges.append(G.make_edge(G.industry_node(target), G.industry_node(anchor),
                                     G._OPPOSITE[rel], "dart", origin=ticker,
                                     product=what, attribution=attribution,
                                     confidence=item["confidence"], evidence=ev, asof=asof))
    return edges


# ---- LLM 호출 ------------------------------------------------------------

def build_prompt(doc: dict, vocab: IndustryVocab) -> str:
    return (f"## 기존 산업 목록 (해당하면 글자 그대로 재사용)\n"
            f"{', '.join(vocab.known)}\n\n"
            f"## 대상 기업\n{doc.get('corp_name') or doc['ticker']} ({doc['ticker']})\n"
            f"출처: {doc['report_nm']} (접수 {doc['rcept_dt']})"
            f"{' — 분량 초과로 앞부분만' if doc.get('truncated') else ''}\n\n"
            f"## 사업의 내용\n{doc['section']}")


def _request_params(doc: dict, vocab: IndustryVocab, cfg: dict) -> dict:
    return {
        "model": cfg["model"],
        "max_tokens": cfg.get("max_tokens", 8000),
        # 시스템 프롬프트는 전 기업 공통이라 캐시하면 그대로 절약된다
        "system": [{"type": "text", "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"}}],
        "output_config": {"format": {"type": "json_schema", "schema": EXTRACT_SCHEMA}},
        "messages": [{"role": "user", "content": build_prompt(doc, vocab)}],
    }


def _parse(response) -> dict | None:
    if getattr(response, "stop_reason", None) == "refusal":
        print("  모델이 응답을 거부했습니다.")
        return None
    for block in response.content:
        if block.type == "text":
            try:
                return json.loads(block.text)
            except json.JSONDecodeError:
                return None
    return None


RETRY_STATUS = (429, 500, 502, 503, 529)
MAX_LLM_RETRIES = 5


def _headers(exc) -> dict:
    resp = getattr(exc, "response", None)
    return getattr(resp, "headers", None) or {}


def _retry_after(exc) -> float | None:
    """서버가 알려 준 대기 시간. 추측보다 이게 항상 낫다."""
    for key in ("retry-after", "anthropic-ratelimit-input-tokens-reset"):
        raw = _headers(exc).get(key)
        if not raw:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def rate_limit_detail(exc) -> str:
    """429가 '어느' 한도인지 헤더에서 읽는다.

    응답 본문은 {'type':'rate_limit_error','message':'Error'}뿐이라 아무것도
    알려주지 않는다. 한도 종류를 모르면 조치가 갈린다 — 요청 **간격**을 늘릴지,
    요청 **크기**를 줄일지가 정반대다. 한 요청이 이미 분당 토큰 한도를 넘으면
    아무리 물러서도 통과하지 못한다(실제로 5종목이 전부 그렇게 실패했다).
    """
    h = _headers(exc)
    parts = []
    for kind in ("requests", "input-tokens", "output-tokens", "tokens"):
        limit = h.get(f"anthropic-ratelimit-{kind}-limit")
        remaining = h.get(f"anthropic-ratelimit-{kind}-remaining")
        if limit or remaining:
            parts.append(f"{kind}={remaining}/{limit}")
    reset = h.get("retry-after") or h.get("anthropic-ratelimit-input-tokens-reset")
    if reset:
        parts.append(f"reset={reset}")
    return " ".join(parts) or "한도 헤더 없음"


def extract_one(client: Anthropic, doc: dict, vocab: IndustryVocab, cfg: dict) -> dict | None:
    """한 건 추출. 429/일시 오류는 물러섰다가 다시 시도한다.

    한 요청이 입력 6만 자(≈2.5만 토큰)라, 연속으로 쏘면 분당 입력 토큰 한도에
    바로 걸린다. 실제로 8종목을 한 번에 돌렸을 때 **전부 429**로 실패했다.
    SDK 기본 재시도(2회)는 간격이 짧아 이 한도에는 소용이 없다.
    """
    params = _request_params(doc, vocab, cfg)
    delay = 8.0
    for attempt in range(1, MAX_LLM_RETRIES + 1):
        try:
            return _parse(client.messages.create(**params))
        except Exception as e:
            status = getattr(e, "status_code", None)
            if status not in RETRY_STATUS or attempt == MAX_LLM_RETRIES:
                raise
            wait = _retry_after(e) or delay
            detail = f" [{rate_limit_detail(e)}]" if status == 429 else ""
            print(f"    {doc['ticker']} {status} — {wait:.0f}초 후 재시도 "
                  f"({attempt}/{MAX_LLM_RETRIES - 1}){detail}")
            time.sleep(min(wait, 120))
            delay *= 2
    return None


def extract_batch(client: Anthropic, docs: list[dict], vocab: IndustryVocab,
                  cfg: dict, poll_seconds: int = 30) -> dict[str, dict]:
    """Batch API로 일괄 추출. 표준 요금의 50%이고 최대 10만 건까지 들어간다.

    결과는 **순서가 보장되지 않으므로** custom_id(티커)로 대조한다.
    """
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    batch = client.messages.batches.create(requests=[
        Request(custom_id=d["ticker"],
                params=MessageCreateParamsNonStreaming(**_request_params(d, vocab, cfg)))
        for d in docs])
    print(f"배치 제출: {batch.id} ({len(docs)}건) — 완료까지 최대 24시간")

    while True:
        batch = client.messages.batches.retrieve(batch.id)
        if batch.processing_status == "ended":
            break
        counts = batch.request_counts
        print(f"  진행 중: 처리 {counts.processing} / 성공 {counts.succeeded} / 실패 {counts.errored}")
        time.sleep(poll_seconds)

    out: dict[str, dict] = {}
    for result in client.messages.batches.results(batch.id):
        if result.result.type != "succeeded":
            print(f"  {result.custom_id}: {result.result.type}")
            continue
        parsed = _parse(result.result.message)
        if parsed:
            out[result.custom_id] = parsed
    return out


# ---- 파이프라인 ----------------------------------------------------------

def collect_documents(tickers: list[str], cfg: dict | None = None
                      ) -> tuple[list[dict], list[str]]:
    cfg = cfg or {}
    max_chars = cfg.get("max_section_chars", 60000)
    slim = cfg.get("slim_sections", True)
    corp_codes = dart.load_corp_codes()
    docs, missing = [], []
    started = time.monotonic()
    for i, t in enumerate(tickers, 1):
        # 종목마다 한 줄씩 찍는다. 20건마다 찍던 때는 25종목 수집이 70분을 넘겨도
        # 로그가 비어 있어서 '멈춤'과 '느림'을 구분할 수 없었다. 한 건이 최악
        # 3분(타임아웃 60초 × 재시도 3회)까지 걸릴 수 있으니, 어디서 걸리는지가
        # 보여야 한다.
        t0 = time.monotonic()
        try:
            doc = dart.retry(dart.fetch_business_section, t, corp_codes,
                             max_chars=max_chars, slim=slim)
        except dart.DartError as e:
            print(f"  [{i}/{len(tickers)}] {t} 실패 ({time.monotonic() - t0:.0f}s): {e}",
                  flush=True)
            missing.append(t)
            continue
        if doc:
            docs.append(doc)
            print(f"  [{i}/{len(tickers)}] {t} {doc.get('corp_name') or '':12}"
                  f" {len(doc['section']):>7,}자 ({time.monotonic() - t0:.0f}s)", flush=True)
        else:
            missing.append(t)
            print(f"  [{i}/{len(tickers)}] {t} '사업의 내용' 없음"
                  f" ({time.monotonic() - t0:.0f}s)", flush=True)
        time.sleep(0.15)   # DART 분당 호출 제한 배려
    print(f"  수집 {len(docs)}건 / 미확보 {len(missing)}건, "
          f"총 {time.monotonic() - started:.0f}초", flush=True)
    return docs, missing


BATCH_SCOPE_HINT = (
    "Batch API는 OAuth 토큰(CLAUDE_CODE_OAUTH_TOKEN)으로 호출할 수 없습니다.\n"
    "  서버 응답: 'OAuth token does not meet scope requirement\n"
    "             any_of(user:batch, user:developer, workspace:developer,\n"
    "                    workspace:inference)'\n"
    "  선택지:\n"
    "   1. --no-batch로 순차 실행 (요금 2배지만 소량이면 무시할 만하다)\n"
    "   2. ANTHROPIC_API_KEY를 따로 발급해 등록 (Batch는 요금 50%)")


def preflight(model: str) -> bool:
    """가장 작은 요청 하나로 '호출 자체가 되는가'를 판별한다.

    429가 났을 때 원인이 둘로 갈리는데, 조치가 정반대다.
      (가) 요청이 커서 분당 토큰 한도를 넘는다 → 문서를 더 줄인다
      (나) 이 자격 증명으로는 Messages API를 못 쓴다 → 줄여도 소용없다

    입력 10토큰짜리 요청이 통과하면 (가), 이것도 429면 (나)다. 사업보고서
    5건을 12분간 태워 가며 알아낼 일이 아니다.
    """
    client = llm.build_client()
    print(f"사전 점검: 최소 요청 1건 ({model}, 인증={llm.credential_kind()})")
    try:
        resp = client.messages.create(
            model=model, max_tokens=8,
            messages=[{"role": "user", "content": "ping"}])
    except Exception as e:
        status = getattr(e, "status_code", None)
        print(f"  실패 — HTTP {status}: {str(e)[:300]}")
        if status == 429:
            print("  최소 요청조차 429입니다. 요청 크기 문제가 아닙니다.\n"
                  "  이 자격 증명으로는 Messages API 호출이 불가하거나, 구독\n"
                  "  사용량이 소진된 상태입니다. 문서를 더 줄여도 통과하지 않습니다.")
        return False
    text = "".join(b.text for b in resp.content if b.type == "text")[:60]
    print(f"  통과 — 응답 {resp.usage.input_tokens}입력/"
          f"{resp.usage.output_tokens}출력 토큰: {text!r}")
    return True


SECTIONS_DIR = DATA_DIR / "dart_sections"


def fetch_only(tickers: list[str], cfg: dict) -> dict:
    """공시를 받아 절 선별까지만 하고 텍스트로 남긴다. LLM을 호출하지 않는다.

    추출을 **세션 안의 Claude가 직접** 수행하는 경로를 위한 것이다. API 키 없이
    구독만 있는 환경에서는 Messages API 호출이 막히지만, 사람이 쓰는 Claude는
    같은 문서를 읽고 같은 스키마로 추출할 수 있다. 인용 검증은 그대로 돌므로
    추출자가 API든 세션이든 품질 장치는 동일하게 작동한다.
    """
    docs, missing = collect_documents(tickers, cfg)
    if not docs:
        raise SystemExit(f"'사업의 내용'을 하나도 확보하지 못했습니다. 미확보: {missing}")

    SECTIONS_DIR.mkdir(parents=True, exist_ok=True)
    for d in docs:
        (SECTIONS_DIR / f"{d['ticker']}.json").write_text(
            json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")

    full = sum(d.get("full_chars") or len(d["section"]) for d in docs)
    kept = sum(len(d["section"]) for d in docs)
    print(f"절 선별: {full:,}자 → {kept:,}자 ({kept / max(1, full):.0%})")
    for d in docs:
        print(f"  {d['ticker']} {d.get('corp_name') or '':10} {len(d['section']):>7,}자  "
              f"{', '.join(d.get('kept_sections') or ['(원문)'])[:70]}")
    print(f"\n{SECTIONS_DIR}에 {len(docs)}건 저장. 미확보 {len(missing)}건: {missing}")
    return {"documents": len(docs), "missing": missing}


_MEMBERSHIP_KEEP = re.compile(r"사업의\s*개요|사업\s*개요|주요\s*제품|제품\s*및\s*서비스|매출실적")
_MEMBERSHIP_DROP = re.compile(r"생산설비|생산\s*및\s*설비|유형자산|무형자산|가격변동추이")


def membership_digest(section: str, budget: int = 1200) -> str:
    """'이 회사가 무엇을 파는가'만 남긴 요약.

    소형주에는 **소속 산업만 붙으면 된다.** 후방·전방은 대형주 보고서에서
    산업 대 산업으로 이미 확정되므로, 소형주가 그 산업에 속하기만 하면 파급
    대상이 된다. 대형주가 오른 뒤 아직 안 움직인 소형 공급사를 찾는 것이
    이 파이프라인의 목적이라, 여기가 비면 파급 대상 자체가 없다.

    그래서 원재료·매출처까지 읽을 필요가 없고, 분량을 1/10로 줄이면 한 번에
    수백 종목을 다룰 수 있다. 인용 대조는 원문 전체를 상대로 하므로 여기서
    잘려 나간 부분을 인용해도 검증은 정상 동작한다.
    """
    marks = [(m.start(), m.group(1).strip())
             for m in dart._SUBSECTION_LINE.finditer(section)]
    marks = [(p, t) for p, t in marks if t]
    if len(marks) < 2:
        return section[:budget]
    out = []
    for i, (pos, title) in enumerate(marks):
        if _MEMBERSHIP_DROP.search(title) or not _MEMBERSHIP_KEEP.search(title):
            continue
        end = marks[i + 1][0] if i + 1 < len(marks) else len(section)
        out.append(section[pos:end].strip()[:900])
    text = "\n\n".join(out).strip()
    return (text or section)[:budget]


def load_sections() -> dict[str, dict]:
    if not SECTIONS_DIR.exists():
        raise SystemExit(f"{SECTIONS_DIR}가 없습니다. 먼저 --fetch-only로 공시를 받으세요.")
    return {p.stem: json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(SECTIONS_DIR.glob("*.json"))}


def run(tickers: list[str], cfg: dict, asof: str, use_batch: bool = True,
        extractions: dict | None = None) -> dict:
    if extractions is None:
        if not llm.has_credentials():
            raise SystemExit(llm.MISSING_HINT)

        # 공시 수집에 몇 분이 걸린 뒤에야 403으로 죽으면 그 시간이 통째로 낭비다.
        # 알 수 있는 실패는 시작 전에 말한다.
        if use_batch and llm.credential_kind() != llm.API_KEY_ENV:
            raise SystemExit(BATCH_SCOPE_HINT)

    if extractions is not None:
        # 추출은 이미 끝났고, 남은 일은 검증과 엣지 생성이다. 공시는 저장된
        # 절 텍스트에서 읽는다 — 인용 대조는 반드시 **추출자가 본 그 텍스트**와
        # 해야 한다. 다시 받아 오면 미묘하게 달라져 멀쩡한 인용이 폐기된다.
        stored = load_sections()
        unknown = [t for t in extractions if t not in stored]
        if unknown:
            raise SystemExit(
                f"추출에는 있으나 저장된 절이 없는 종목: {unknown}\n"
                "  인용을 대조할 원문이 없으면 검증이 불가능합니다.")
        docs, missing = [stored[t] for t in extractions], []
        print(f"== 저장된 절 사용 ({len(docs)}종목) ==")
    else:
        print(f"== 사업보고서 수집 ({len(tickers)}종목) ==")
        docs, missing = collect_documents(tickers, cfg)
    print(f"'사업의 내용' 확보 {len(docs)}건 / 미확보 {len(missing)}건")
    if not docs:
        return {"edges": 0, "documents": 0}

    # 절 선별이 실제로 얼마나 줄였는지 드러낸다. 요청 크기가 곧 429의 원인이라
    # 이 숫자가 다음 실행의 성패를 가른다.
    full = sum(d.get("full_chars") or len(d["section"]) for d in docs)
    kept = sum(len(d["section"]) for d in docs)
    print(f"절 선별: {full:,}자 → {kept:,}자 ({kept / max(1, full):.0%}), "
          f"평균 {kept // max(1, len(docs)):,}자/건")
    sample = next((d for d in docs if d.get("kept_sections")), None)
    if sample:
        print(f"  예시({sample['ticker']}): {', '.join(sample['kept_sections'][:8])}")
    no_slim = [d["ticker"] for d in docs if not d.get("kept_sections")]
    if no_slim:
        print(f"  ::warning:: 하위 절을 못 찾아 원문을 그대로 쓴 종목: {no_slim}")

    existing = G.load()
    vocab = IndustryVocab(known=known_industries(existing), aliases=load_aliases())
    print(f"기존 산업 어휘 {len(vocab.known)}개, 별칭 {len(vocab.aliases)}개")
    if not vocab.known:
        print("  ::warning:: 산업 어휘가 비었습니다. 어휘 통제 없이 추출하면 "
              "'시멘트'와 '시멘트 제조업'이 다른 노드가 되어 그래프가 조각납니다.")

    if extractions is not None:
        raw = extractions
        print(f"== 추출 (세션 제공, {len(raw)}건) ==")
    elif use_batch:
        client = llm.build_client()
        print(f"Claude 인증: {llm.credential_kind()}")
        print(f"== 추출 (배치, {cfg['model']}) ==")
        raw = extract_batch(client, docs, vocab, cfg)
    else:
        raw = {}
        failures: list[str] = []
        for i, d in enumerate(docs, 1):
            if i > 1:
                # 요청 간 간격. 한 건이 입력 6만 자라 붙여 쏘면 분당 토큰 한도에 걸린다.
                time.sleep(cfg.get("request_interval", 5))
            try:
                r = extract_one(client, d, vocab, cfg)
            except Exception as e:
                print(f"  {d['ticker']} 추출 실패: {e}")
                failures.append(d["ticker"])
                continue
            if r:
                raw[d["ticker"]] = r
            print(f"  {i}/{len(docs)} {d['ticker']}")
        if failures:
            print(f"  ::warning:: 추출 실패 {len(failures)}건 — {failures}")

    # 공시는 다 받았는데 추출이 0건이면 그건 '결과 없음'이 아니라 고장이다.
    # 여기서 멈추지 않으면 빈 리포트가 커밋되고 워크플로는 초록불로 끝난다 —
    # 실제로 429로 8건이 전부 실패했을 때 그렇게 됐다.
    if not raw:
        raise RuntimeError(
            f"공시 {len(docs)}건을 확보했으나 추출에 **전부 실패**했습니다.\n"
            "  위의 실패 사유를 보세요. 429가 전부라면 조치는 둘 중 하나입니다.\n"
            "  - 남은 한도가 0이 아닌데 걸린다면: 요청 간격(dart.request_interval)을 늘린다\n"
            "  - 한 요청만으로 한도를 넘는다면: 간격을 늘려도 소용없다.\n"
            "    dart.max_section_chars를 줄이거나 Batch API(--use-batch)를 쓴다.\n"
            "    Batch는 한도 체계가 별도라 대량 추출에는 그쪽이 정석이다.")

    by_ticker = {d["ticker"]: d for d in docs}
    edges: list[dict] = []
    total = {"kept": 0, "dropped": 0}
    samples: list[str] = []

    for ticker, extraction in raw.items():
        doc = by_ticker[ticker]
        clean, stats = filter_by_quote(extraction, doc["section"])
        total["kept"] += stats["kept"]
        total["dropped"] += stats["dropped"]
        samples.extend(stats["dropped_samples"][:2])
        edges += to_edges(ticker, clean, vocab, asof,
                          evidence_prefix=f"{doc['report_nm']}({doc['rcept_dt']}): ")

    checked = total["kept"] + total["dropped"]
    rate = total["dropped"] / checked if checked else 0.0
    print(f"\n인용 검증: {total['kept']}/{checked} 통과 (폐기율 {rate:.1%})")
    if rate > 0.3:
        print("  ::warning:: 폐기율이 높습니다. 절 추출이 잘못됐거나 프롬프트를 점검하세요.")
    for s in samples[:5]:
        print(f"  폐기: {s}")

    suggestions = vocab.alias_suggestions()
    if vocab.new:
        top = sorted(vocab.new.items(), key=lambda kv: -kv[1])[:20]
        print(f"\n신규 산업 {len(vocab.new)}개 (별칭 정리 대상):")
        print("  " + ", ".join(f"{k}({v})" for k, v in top))
    if suggestions:
        print(f"\n별칭 후보 — 같은 산업이면 {ALIAS_FILE.name}에 추가하세요:")
        for fresh, hits in list(suggestions.items())[:15]:
            print(f"  {fresh}: {hits[0]}" + (f"   (그 외 {hits[1:]})" if len(hits) > 1 else ""))

    persistent = [e for e in existing if e.get("source") in graph_build.PERSISTENT_SOURCES]
    merged = G.merge(persistent, edges)
    G.save(merged)
    print(f"\nDART 엣지 {len(edges)}개 반영 → 수직축 엣지 총 {len(merged)}개")

    report = {"asof": asof, "documents": len(docs), "missing": missing,
              "extracted": len(raw), "quote_kept": total["kept"],
              "quote_dropped": total["dropped"], "drop_rate": round(rate, 3),
              "new_industries": vocab.new, "alias_suggestions": suggestions,
              "edges": len(edges), "total_edges": len(merged)}
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / f"dart_extract_{asof}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _default_tickers(top_n: int, base_date: str | None) -> list[str]:
    """시가총액 상위 종목. 커버리지를 넓히는 가장 단순한 우선순위다."""
    from . import universe as universe_mod
    from pykrx import stock

    date = base_date or stock.get_nearest_business_day_in_a_week()
    uni = universe_mod.build(date)
    ranked = sorted(uni.entries.items(), key=lambda kv: -(kv[1].get("market_cap") or 0))
    return [t for t, _ in ranked[:top_n]]


def main():
    p = argparse.ArgumentParser(description="DART 사업보고서에서 수직축 엣지를 추출한다.")
    p.add_argument("--tickers", help="쉼표로 구분한 종목코드. 없으면 시총 상위 --top-n")
    p.add_argument("--top-n", type=int, default=100, help="대상 종목 수 (기본 100)")
    p.add_argument("--date", help="종목 마스터 기준일 YYYYMMDD")
    p.add_argument("--model", help="config.yaml의 dart.model 덮어쓰기")
    p.add_argument("--no-batch", action="store_true",
                   help="Batch API 대신 순차 호출 (소량·즉시 확인용, 비용 2배)")
    p.add_argument("--preflight", action="store_true",
                   help="최소 요청 1건만 보내 호출 가능 여부를 확인하고 끝낸다")
    p.add_argument("--fetch-only", action="store_true",
                   help="공시 수집·절 선별까지만 하고 data/dart_sections/에 저장 (LLM 미호출)")
    p.add_argument("--extractions",
                   help="{티커: 추출결과} JSON 파일. 저장된 절과 대조해 검증하고 엣지를 만든다 "
                        "(LLM 미호출). --fetch-only로 받아 둔 절이 있어야 한다")
    p.add_argument("--digest", action="store_true",
                   help="저장된 절에서 '무엇을 파는가'만 요약해 출력한다. 소속 매핑용")
    args = p.parse_args()

    cfg_all = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    cfg = dict(cfg_all.get("dart") or {})
    cfg.setdefault("model", "claude-opus-5")
    cfg.setdefault("max_tokens", 8000)
    if args.model:
        cfg["model"] = args.model

    tickers = ([t.strip() for t in args.tickers.split(",") if t.strip()]
               if args.tickers else None)
    asof = args.date or __import__("datetime").datetime.now().strftime("%Y%m%d")

    # LLM을 쓰지 않는 모드를 먼저 처리한다. 자격 증명 검사에 걸리면 안 된다.
    if args.digest:
        stored = load_sections()
        want = tickers or sorted(stored)
        for t in want:
            d = stored.get(t)
            if not d:
                print(f"### {t} — 절 없음\n")
                continue
            print(f"### {t} {d.get('corp_name') or ''} ({len(d['section']):,}자)")
            print(membership_digest(d["section"]))
            print()
        return
    if args.fetch_only:
        print(json.dumps(fetch_only(tickers or _default_tickers(args.top_n, args.date), cfg),
                         ensure_ascii=False, indent=2))
        return
    if args.extractions:
        extractions = json.loads(Path(args.extractions).read_text(encoding="utf-8"))
        report = run([], cfg, asof, extractions=extractions)
        print(json.dumps(report, ensure_ascii=False, indent=2)[:1500])
        return

    if not llm.has_credentials():
        raise SystemExit(llm.MISSING_HINT)
    if args.preflight:
        raise SystemExit(0 if preflight(cfg["model"]) else 1)

    tickers = tickers or _default_tickers(args.top_n, args.date)
    report = run(tickers, cfg, asof, use_batch=not args.no_batch)
    print(json.dumps(report, ensure_ascii=False, indent=2)[:1500])


if __name__ == "__main__":
    main()
