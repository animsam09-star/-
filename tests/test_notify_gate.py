"""분석이 없을 때 알림이 나가면 안 된다.

알림 본문은 '무엇이 왜 올랐고 어디로 파급되는가'다. 분석이 비면 남는 건
상승률 순 목록뿐인데, 그게 매일 도착하면 분석이 도는 것처럼 보인다. 실제로는
LLM 호출이 429로 전부 막혀 한 건도 못 돌고 있었고, 알림만 정상으로 나갔다.
"""

from src import pipeline


def test_no_analysis_blocks_the_notification():
    blocked = pipeline.notify_blocker({"analyses": [], "synthesis": None})
    assert blocked
    assert "원인 분석이 없어" in blocked


def test_missing_key_is_treated_as_no_analysis():
    assert pipeline.notify_blocker({})
    assert pipeline.notify_blocker(None)


def test_real_analysis_is_sent():
    assert pipeline.notify_blocker({"analyses": [{"ticker": "005930"}]}) == ""


def test_skip_notify_still_wins():
    assert pipeline.notify_blocker({"analyses": [{"ticker": "005930"}]},
                                   skip_notify=True)


def test_notification_resumes_on_its_own():
    """껐다 켜는 스위치를 따로 두면 분석이 돌아와도 알림은 꺼진 채 남는다."""
    assert pipeline.notify_blocker({"analyses": []})
    assert pipeline.notify_blocker({"analyses": [{"ticker": "005930"}]}) == ""
