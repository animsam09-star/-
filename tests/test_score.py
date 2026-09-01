"""사후 채점 검증: 예측 수집과 집계가 정확한지 확인 (네트워크 조회는 제외)."""

import json
from datetime import datetime, timedelta

import pytest

from src import score


@pytest.fixture
def cases_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(score, "CASES_DIR", tmp_path)
    return tmp_path


def _write_case(cases_dir, date: str, beneficiaries: list[dict], axis: str = "수평"):
    payload = {"base_date": date, "candidates": [], "analyses": [],
               "synthesis": {"market_summary": "", "ideas": [
                   {"title": "테스트 아이디어", "axis": axis, "driver": "구리",
                    "path": "", "watch_points": "", "beneficiaries": beneficiaries}]}}
    (cases_dir / f"{date}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_collects_only_aged_predictions(cases_dir):
    """아직 보유기간이 지나지 않은 예측은 채점 대상에서 빠져야 한다."""
    old = (datetime.now() - timedelta(days=60)).strftime("%Y%m%d")
    recent = (datetime.now() - timedelta(days=2)).strftime("%Y%m%d")
    ben = [{"name": "테스트", "ticker": "000001", "priced_in": "미반영", "impact": "수혜"}]
    _write_case(cases_dir, old, ben)
    _write_case(cases_dir, recent, ben)

    preds = score.collect_predictions(min_age_days=24)
    assert [p["base_date"] for p in preds] == [old]


def test_drops_predictions_without_ticker(cases_dir):
    """티커가 붙지 않은 후보는 채점할 수 없으므로 제외된다."""
    old = (datetime.now() - timedelta(days=60)).strftime("%Y%m%d")
    _write_case(cases_dir, old, [
        {"name": "매칭됨", "ticker": "000001", "priced_in": "미반영", "impact": "수혜"},
        {"name": "미매칭", "priced_in": "미반영", "impact": "수혜"},
    ])
    preds = score.collect_predictions(min_age_days=24)
    assert len(preds) == 1
    assert preds[0]["name"] == "매칭됨"


def test_ignores_malformed_case_files(cases_dir):
    old = (datetime.now() - timedelta(days=60)).strftime("%Y%m%d")
    _write_case(cases_dir, old,
                [{"name": "정상", "ticker": "000001", "priced_in": "미반영", "impact": "수혜"}])
    (cases_dir / "20991301.json").write_text("{bad json", encoding="utf-8")
    (cases_dir / "notadate.json").write_text("{}", encoding="utf-8")

    preds = score.collect_predictions(min_age_days=24)
    assert len(preds) == 1


def test_directional_hit_counts_losses_as_correct_for_negative_calls(cases_dir, monkeypatch):
    """'피해'로 꼽은 종목은 하락해야 맞은 예측이다."""
    old = (datetime.now() - timedelta(days=60)).strftime("%Y%m%d")
    _write_case(cases_dir, old, [
        {"name": "수혜주", "ticker": "000001", "priced_in": "미반영", "impact": "수혜"},
        {"name": "피해주", "ticker": "000002", "priced_in": "확인불가", "impact": "피해"},
    ])
    # 수혜주는 상승, 피해주는 하락 → 둘 다 방향 적중
    monkeypatch.setattr(score, "_forward_return",
                        lambda t, d, h: 0.08 if t == "000001" else -0.05)

    result = score.score(horizon=10)
    assert result["n"] == 2
    assert result["directional_hit_rate"] == 1.0
    assert result["by_impact"]["피해"]["mean"] == pytest.approx(-0.05)


def test_provenance_and_path_are_scored_separately(cases_dir, monkeypatch):
    """그래프 기반 후보와 LLM 창작 후보를 갈라 봐야 밸류체인 맵의 값을 알 수 있다."""
    old = (datetime.now() - timedelta(days=60)).strftime("%Y%m%d")
    _write_case(cases_dir, old, [
        {"name": f"수혜{i}", "ticker": f"00000{i}", "priced_in": "미반영",
         "impact": "수혜", "graph_backed": True, "via": "건설·EPC→후방→시멘트"}
        for i in range(1, 4)
    ] + [{"name": "창작", "ticker": "000009", "priced_in": "미반영",
          "impact": "수혜", "graph_backed": False}])
    monkeypatch.setattr(score, "_forward_return",
                        lambda t, d, h: -0.04 if t == "000009" else 0.06)

    result = score.score(horizon=10)
    assert result["by_provenance"]["그래프기반"]["n"] == 3
    assert result["by_provenance"]["LLM창작"]["n"] == 1
    assert result["by_path"]["건설·EPC→후방→시멘트"]["n"] == 3
    assert "건설·EPC→후방→시멘트" in score.render(result)


def test_path_buckets_below_min_sample_are_dropped(cases_dir, monkeypatch):
    """1~2건짜리 경로 적중률은 잡음이라 보고하지 않는다."""
    old = (datetime.now() - timedelta(days=60)).strftime("%Y%m%d")
    _write_case(cases_dir, old, [
        {"name": "A", "ticker": "000001", "priced_in": "미반영", "impact": "수혜",
         "graph_backed": True, "via": "조선→후방→조선기자재"}])
    monkeypatch.setattr(score, "_forward_return", lambda t, d, h: 0.05)
    assert score.score(horizon=10)["by_path"] == {}


def test_render_handles_empty_result():
    assert "없습니다" in score.render({"n": 0})
