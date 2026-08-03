"""LLM 호출이 막혔을 때 분석을 밖에서 받아 넣는 경로.

OAuth 토큰은 Messages API에서 429가 난다. 그래서 원인 분석이 한 건도 못 돌고
있었다 — 파이프라인의 마지막 빈칸이다. DART 추출에서 쓴 것과 같이, 비싸거나
막히는 한 단계만 밖으로 빼고 검증·주입·종합은 코드에 남긴다.
"""

import pytest

from src import analyze, graph as G, universe as U

UNI = U.Universe("20260803", {t: {"name": n, "market": "KOSPI", "market_cap": 1}
                              for t, n in {"005930": "삼성전자",
                                           "000660": "SK하이닉스"}.items()})
CAND = [{"ticker": "005930", "name": "삼성전자", "trigger": "강세지속",
         "ret5": 0.08, "ret20": 0.21, "ret60": 0.3, "vol_surge": 2.4,
         "high_proximity": 0.98, "close": 90000, "market_cap": 10**14}]
CFG = {"max_candidates": 5, "model": "x", "max_tokens": 100}
STORED = {"005930": {
    "cause_summary": "HBM 증설.", "cause_type": "업황개선(P/Q/C)",
    "cause_scope": "산업공통", "industry": "반도체", "evidence": "8/1 공시",
    "confidence": "높음", "ripple_axis": "수직",
    "ripple_paths": [{"direction": "후방", "target_industry": "전공정 장비",
                      "logic": "증설이면 장비 발주가 뒤따른다",
                      "beneficiaries": [{"name": "SK하이닉스", "impact": "수혜",
                                         "reason": "같은 HBM 사이클"}]}]}}


def test_prompt_is_built_without_calling_the_api():
    rows = analyze.contexts(CAND, {}, CFG, {}, UNI, G.Graph([]))
    assert len(rows) == 1
    assert rows[0]["ticker"] == "005930"
    assert "삼성전자" in rows[0]["prompt"]
    assert "20일 +21.0%" in rows[0]["prompt"]


def test_stored_analysis_skips_the_api(monkeypatch):
    """인증이 없어도 저장된 결과만 있으면 돌아야 한다."""
    monkeypatch.setattr(analyze.llm, "has_credentials", lambda: False)
    monkeypatch.setattr(analyze.llm, "build_client",
                        lambda: pytest.fail("저장된 결과가 있는데 API를 불렀다"))
    out = analyze.analyze(CAND, {}, "20260803", CFG, {}, UNI, G.Graph([]),
                          stored=STORED)
    assert len(out["analyses"]) == 1
    assert out["analyses"][0]["cause_summary"] == "HBM 증설."


def test_stored_path_still_attaches_ticker_and_trigger():
    """밖에서 받은 본문에는 종목 메타가 없다. 코드가 붙여야 리포트가 읽는다."""
    out = analyze.analyze(CAND, {}, "20260803", CFG, {}, UNI, G.Graph([]),
                          stored=STORED)
    a = out["analyses"][0]
    assert a["ticker"] == "005930" and a["trigger"] == "강세지속"
    assert a["ret20"] == pytest.approx(0.21)


def test_missing_ticker_in_stored_is_reported_not_crashed(capsys):
    out = analyze.analyze(CAND, {}, "20260803", CFG, {}, UNI, G.Graph([]), stored={})
    assert out["analyses"] == []
    assert "저장된 분석 없음" in capsys.readouterr().out


def test_stored_synthesis_is_used_when_given():
    out = analyze.analyze(CAND, {}, "20260803", CFG, {}, UNI, G.Graph([]),
                          stored={**STORED, "_synthesis": {"market_summary": "x",
                                                           "ideas": []}})
    assert out["synthesis"]["market_summary"] == "x"


def test_no_credentials_and_no_stored_still_returns_empty(monkeypatch):
    monkeypatch.setattr(analyze.llm, "has_credentials", lambda: False)
    out = analyze.analyze(CAND, {}, "20260803", CFG, {}, UNI, G.Graph([]))
    assert out == {"analyses": [], "synthesis": None}
