"""리포트·프롬프트 렌더링 검증: 부호·갭·축 표기가 출력에 실제로 반영되는지 확인."""

import pytest

from src import analyze, report

NAMES = {"000001": "테스트제련", "000002": "테스트전선", "000003": "테스트리더",
         "000004": "테스트라거드"}

HORIZONTAL = {
    "exposures": {"000003": {"구리": {"beta": 0.85, "corr": 0.41}}},
    "factor_moves": {"구리": {"ret5": 0.107, "ret20": 0.091}},
    "group_gaps": {
        "ETF:테스트그룹": {
            "group_move": 0.045,
            "members": [
                {"ticker": "000004", "beta": 0.98, "corr": 0.8,
                 "expected": 0.069, "actual": 0.029, "gap": 0.040},
                {"ticker": "000003", "beta": 1.38, "corr": 0.9,
                 "expected": 0.090, "actual": 0.159, "gap": -0.069},
            ],
        }
    },
    "membership": {"000003": ["ETF:테스트그룹"]},
    "groups": {"ETF:테스트그룹": ["000003", "000004"]},
}

ANALYSES = [{
    "ticker": "000003", "name": "테스트리더", "trigger": "강세지속", "ret20": 0.30,
    "cause_summary": "구리 가격 급등으로 판가 상승.", "cause_type": "업황개선(P/Q/C)",
    "cause_scope": "산업공통", "industry": "비철금속", "evidence": "7/22 업황 뉴스",
    "confidence": "높음", "ripple_axis": "수평", "groups": ["ETF:테스트그룹"],
    "ripple_paths": [{
        "direction": "공통동인", "target_industry": "전선",
        "logic": "구리는 전선의 주원재료라 원가로 작용한다.",
        "beneficiaries": [
            {"name": "테스트라거드", "impact": "수혜", "reason": "같은 그룹인데 미반응"},
            {"name": "테스트전선", "impact": "피해", "reason": "구리 원가 상승"},
        ],
    }],
}]

SYNTHESIS = {"market_summary": "구리 급등이 공통 동인.", "ideas": [{
    "title": "구리 급등 파급", "driver": "구리 가격", "axis": "수평",
    "path": "구리 상승 → 제련 수혜 / 전선 원가 부담",
    "beneficiaries": [
        {"name": "테스트라거드", "impact": "수혜", "priced_in": "미반영",
         "comment": "그룹 대비 뒤처짐", "gap": 0.040},
        {"name": "테스트전선", "impact": "피해", "priced_in": "확인불가",
         "comment": "원가 부담"},
    ],
    "watch_points": "구리 반락 시 역방향",
}]}

CANDIDATES = [{"ticker": "000003", "name": "테스트리더", "trigger": "강세지속",
               "close": 13000, "ret5": 0.13, "ret20": 0.30, "ret60": 0.40,
               "vol_surge": 3.1, "high_proximity": 1.0, "market_cap": 10 ** 12}]


@pytest.fixture
def analysis():
    return {"analyses": ANALYSES, "synthesis": SYNTHESIS}


def test_prompt_context_surfaces_unmoved_peer():
    ctx = analyze.build_horizontal_context("000003", HORIZONTAL, NAMES)
    assert "테스트라거드" in ctx
    assert "미반영" in ctx
    assert "구리" in ctx


def test_prompt_context_handles_stock_with_no_group():
    ctx = analyze.build_horizontal_context("999999", HORIZONTAL, NAMES)
    assert "없음" in ctx


def test_gap_is_injected_into_beneficiaries():
    analyses = [dict(a, ripple_paths=[dict(p, beneficiaries=[dict(b) for b in p["beneficiaries"]])
                                      for p in a["ripple_paths"]]) for a in ANALYSES]
    returns = {t: {"name": n, "ret5": 0.01, "ret20": 0.03} for t, n in NAMES.items()}
    analyze.check_priced_in(analyses, returns, {n: t for t, n in NAMES.items()}, HORIZONTAL)
    laggard = analyses[0]["ripple_paths"][0]["beneficiaries"][0]
    assert laggard["gap"] == pytest.approx(0.040)
    assert laggard["gap_group"] == "ETF:테스트그룹"


def test_html_shows_axis_impact_and_gap(analysis):
    html = report.render_html("20260724", CANDIDATES, analysis, "테스트", HORIZONTAL, NAMES)
    assert "수평 파급" in html      # 수평 섹션
    assert "[피해]" in html         # 부호 구분
    assert "수평축" in html         # 축 표기
    assert "테스트라거드" in html


def test_html_survives_missing_horizontal(analysis):
    """수평 그래프 생성이 실패해도 리포트는 정상 렌더링돼야 한다."""
    html = report.render_html("20260724", CANDIDATES, analysis, "테스트", None, None)
    assert "테스트리더" in html
    assert "수평 파급" not in html


def test_telegram_marks_negative_impact(analysis):
    tg = report.render_telegram("20260724", CANDIDATES, analysis, None)
    assert "🔻" in tg          # 피해 종목
    assert "🟢" in tg          # 미반영 수혜 종목
    assert "수평축" in tg


def test_telegram_falls_back_without_analysis():
    tg = report.render_telegram("20260724", CANDIDATES,
                                {"analyses": [], "synthesis": None}, None)
    assert "테스트리더" in tg
