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


class TestStoredAnalysisIsValidatedBeforeUse:
    """매일 자동으로 도는 경로에는 사람 눈이 없다.

    `--analyses`로 들어오는 파일은 LLM이 쓴 것이라 스키마가 어긋날 수 있다.
    그대로 리포트에 흘리면 잘못된 항목이 조용히 섞이거나, 렌더링에서 KeyError로
    파이프라인이 통째로 죽는다. 들어오는 자리에서 거른다.

    **틀린 항목만 버리고 나머지는 살린다.** 한 종목이 어긋났다고 스무 종목을
    잃을 이유가 없다 — DART 수집에서 배운 것과 같은 규칙이다.
    """

    GOOD = {
        "cause_summary": "2분기 영업이익 흑자 전환", "cause_type": "실적서프라이즈",
        "cause_scope": "산업공통", "industry": "석유화학", "evidence": "공시",
        "confidence": "높음", "ripple_axis": "수평",
        "ripple_paths": [{"direction": "동종업계", "target_industry": "석유화학",
                          "logic": "스프레드", "beneficiaries": [
                              {"name": "대한유화", "impact": "수혜", "reason": "NCC"}]}],
    }

    def test_a_clean_file_passes_untouched(self):
        clean, problems = analyze.validate_stored({"298000": self.GOOD})
        assert problems == [] and clean == {"298000": self.GOOD}

    def test_one_bad_stock_does_not_drop_the_others(self):
        stored = {"298000": self.GOOD, "000000": {"cause_type": "없는유형"}}
        clean, problems = analyze.validate_stored(stored)
        assert "298000" in clean and "000000" not in clean
        assert problems, "문제를 보고하지 않았다"

    def test_unknown_enum_is_caught(self):
        bad = {**self.GOOD, "confidence": "보통"}
        _, problems = analyze.validate_stored({"a": bad})
        assert any("confidence" in m for m in problems)

    def test_company_specific_cause_may_not_carry_ripple_paths(self):
        """회사고유인데 파급이 붙어 있으면 둘 중 하나가 틀렸다.

        없는 파급을 그리는 것보다 안 그리는 쪽이 덜 해롭다.
        """
        bad = {**self.GOOD, "cause_scope": "회사고유"}
        clean, problems = analyze.validate_stored({"a": bad})
        assert clean == {} and any("회사고유" in m for m in problems)

    def test_beneficiary_without_a_name_is_caught(self):
        """이름이 없으면 티커 해석도, 갭 주입도, 사후 채점도 통째로 빈다."""
        bad = {**self.GOOD, "ripple_paths": [
            {"direction": "동종업계", "target_industry": "x", "logic": "y",
             "beneficiaries": [{"impact": "수혜", "reason": "z"}]}]}
        _, problems = analyze.validate_stored({"a": bad})
        assert any("name" in m for m in problems)

    def test_synthesis_is_checked_too(self):
        stored = {"_synthesis": {"market_summary": "요약", "ideas": [
            {"title": "t", "driver": "d", "axis": "옆으로", "path": "p",
             "beneficiaries": [], "watch_points": "w"}]}}
        clean, problems = analyze.validate_stored(stored)
        assert "_synthesis" not in clean and any("axis" in m for m in problems)

    def test_a_bad_synthesis_does_not_drop_the_stocks(self):
        stored = {"298000": self.GOOD, "_synthesis": {"ideas": []}}
        clean, _ = analyze.validate_stored(stored)
        assert "298000" in clean

    def test_non_object_input_is_reported_not_crashed(self):
        clean, problems = analyze.validate_stored(["말도 안 되는 형식"])
        assert clean == {} and problems
