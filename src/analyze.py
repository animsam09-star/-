"""Claude API 기반 원인 분석 및 밸류체인 파급 추론.

1) 종목별 분석: 뉴스·공시 근거로 상승 원인을 분류하고, 원인이 '산업공통'이면
   밸류체인 맵을 참고해 동종/전방/후방 파급 경로와 수혜 후보를 도출한다.
2) 미반영 체크: 수혜 후보의 최근 수익률을 조회해 이미 주가에 반영됐는지 표시한다.
3) 종합: 산업공통 원인들을 묶어 최종 투자 아이디어를 정리한다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml
from anthropic import Anthropic

ROOT = Path(__file__).resolve().parent.parent
VALUECHAIN_DIR = ROOT / "valuechain"
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
        "ripple_paths": {
            "type": "array",
            "description": "산업공통 원인일 때만 채운다. 회사고유면 빈 배열.",
            "items": {
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "enum": ["동종업계", "전방산업", "후방산업"]},
                    "target_industry": {"type": "string"},
                    "logic": {"type": "string", "description": "파급 논리 (1~2문장)"},
                    "beneficiaries": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "한국거래소 상장사의 정확한 종목명"},
                                "reason": {"type": "string"},
                            },
                            "required": ["name", "reason"],
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
                 "evidence", "confidence", "ripple_paths"],
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
                    "beneficiaries": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "priced_in": {"type": "string", "enum": ["미반영", "일부반영", "기반영", "확인불가"]},
                                "comment": {"type": "string"},
                            },
                            "required": ["name", "priced_in", "comment"],
                            "additionalProperties": False,
                        },
                    },
                    "watch_points": {"type": "string", "description": "확인해야 할 리스크/체크포인트"},
                },
                "required": ["title", "driver", "path", "beneficiaries", "watch_points"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["market_summary", "ideas"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """당신은 한국 주식시장 밸류체인 분석 전문 애널리스트다.

원칙:
- 뉴스·공시 등 하드 이벤트와 교차 확인되는 원인만 채택한다. 그럴듯한 사후설명을 경계하고, 근거가 약하면 confidence를 낮추고 evidence에 '근거 부족'을 명시한다.
- 회사 고유 이슈(개별 M&A, 지배구조, 개별 수급)는 파급 예측에 쓰지 않는다. 산업 공통 요인(P/Q/C 개선, 정책, 전방 수요)만 밸류체인으로 확장한다.
- 수혜 후보는 반드시 한국거래소 상장사의 정확한 종목명으로 제시하고, 노출도가 실질적인 기업만 꼽는다(매출 비중이 미미한 기업 제외).
- 제공된 밸류체인 맵을 우선 참고하되, 맵에 없는 산업은 스스로 추론한다."""


def load_valuechain_maps() -> str:
    parts = []
    for f in sorted(VALUECHAIN_DIR.glob("*.yaml")):
        parts.append(f.read_text(encoding="utf-8"))
    return "\n---\n".join(parts) if parts else "(밸류체인 맵 없음)"


def _extract_json(response) -> dict | None:
    if response.stop_reason == "refusal":
        print("  분석 거부됨(refusal)")
        return None
    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        return None
    return json.loads(text)


def analyze_stock(client: Anthropic, cand: dict, evidence: dict, vc_maps: str, cfg: dict) -> dict | None:
    news = "\n".join(f"- [{n['date']}] {n['title']} — {n['description']}" for n in evidence.get("news", []))
    filings = "\n".join(f"- [{f['date']}] {f['title']}" for f in evidence.get("filings", []))
    prompt = f"""다음 종목의 최근 주가 상승 원인을 분석하고, 산업공통 원인이면 밸류체인 파급 경로를 도출하라.

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

## 밸류체인 맵 (참고자료)
{vc_maps}"""

    response = client.messages.create(
        model=cfg["model"],
        max_tokens=cfg["max_tokens"],
        system=SYSTEM_PROMPT,
        output_config={"format": {"type": "json_schema", "schema": STOCK_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    return _extract_json(response)


def check_priced_in(analyses: list[dict], returns: dict, name_to_ticker: dict) -> None:
    """수혜 후보 종목명을 티커로 매칭해 최근 수익률을 주입한다 (미반영 체크용)."""
    for a in analyses:
        for path in a.get("ripple_paths", []):
            for b in path.get("beneficiaries", []):
                ticker = name_to_ticker.get(b["name"])
                if ticker and ticker in returns:
                    r = returns[ticker]
                    b["ticker"] = ticker
                    b["ret5"] = r.get("ret5")
                    b["ret20"] = r.get("ret20")


def synthesize(client: Anthropic, analyses: list[dict], cfg: dict) -> dict | None:
    common = [a for a in analyses if a.get("cause_scope") == "산업공통"]
    if not common:
        return {"market_summary": "산업공통 요인으로 분류된 상승 종목이 없어 파급 아이디어를 도출하지 않았다.",
                "ideas": []}
    prompt = f"""아래는 오늘 상승 종목들의 개별 분석 결과다(산업공통 원인만 추린 것).
수혜 후보에는 최근 수익률(ret5/ret20)이 붙어 있다 — 이미 크게 오른 후보는 '기반영'으로 강등하고,
아직 덜 움직인 후보를 우선하라. 같은 동인은 하나의 아이디어로 묶어라.

{json.dumps(common, ensure_ascii=False, indent=1)}"""

    response = client.messages.create(
        model=cfg["model"],
        max_tokens=cfg["max_tokens"],
        system=SYSTEM_PROMPT,
        output_config={"format": {"type": "json_schema", "schema": SYNTHESIS_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    return _extract_json(response)


def analyze(candidates: list[dict], evidence_all: dict, base_date: str, cfg: dict) -> dict:
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY 미설정 — 원인 분석을 건너뜁니다.")
        return {"analyses": [], "synthesis": None}

    client = Anthropic()
    vc_maps = load_valuechain_maps()

    returns_file = DATA_DIR / f"returns_{base_date}.json"
    returns = json.loads(returns_file.read_text(encoding="utf-8")) if returns_file.exists() else {}
    name_to_ticker = {v["name"]: t for t, v in returns.items() if isinstance(v, dict) and v.get("name")}

    analyses = []
    for cand in candidates[: cfg["max_candidates"]]:
        print(f"  분석 중: {cand['name']}")
        try:
            result = analyze_stock(client, cand, evidence_all.get(cand["ticker"], {}), vc_maps, cfg)
        except Exception as e:
            print(f"  분석 실패({cand['name']}): {e}")
            continue
        if result:
            result["ticker"] = cand["ticker"]
            result["name"] = cand["name"]
            result["trigger"] = cand["trigger"]
            result["ret20"] = cand["ret20"]
            analyses.append(result)

    check_priced_in(analyses, returns, name_to_ticker)

    try:
        synthesis = synthesize(client, analyses, cfg)
    except Exception as e:
        print(f"종합 분석 실패: {e}")
        synthesis = None

    out = {"base_date": base_date, "analyses": analyses, "synthesis": synthesis}
    (DATA_DIR / f"analysis_{base_date}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"종목 분석 {len(analyses)}건 완료")
    return out
