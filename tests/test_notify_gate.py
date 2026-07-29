"""알림은 보내라고 할 때만 나간다.

자동 발송이 기본이면 보낼 내용이 준비됐는지와 무관하게 매일 나간다. 실제로
원인 분석이 429로 한 건도 못 도는 동안 알림만 정상으로 나갔고, 받는 쪽에서는
'분석이 돌고 있다'와 '멈춰 있다'를 구분할 방법이 없었다.
"""

from src import pipeline


def test_nothing_is_sent_by_default():
    """기본값이 발송이면, 준비되지 않은 것도 매일 나간다."""
    send, _ = pipeline.notify_decision({"analyses": [{"ticker": "005930"}]})
    assert send is False


def test_default_path_says_nothing():
    """안 보내는 게 정상 동작이라 사유를 찍을 것도 없다."""
    _, why = pipeline.notify_decision({"analyses": [{"ticker": "005930"}]})
    assert why == ""


def test_explicit_request_sends():
    send, why = pipeline.notify_decision({"analyses": [{"ticker": "005930"}]},
                                         notify=True)
    assert send is True and why == ""


def test_request_without_analysis_is_refused_with_a_reason():
    """요청은 있었으니 왜 안 나갔는지는 말해 줘야 한다."""
    send, why = pipeline.notify_decision({"analyses": []}, notify=True)
    assert send is False
    assert "원인 분석이 없어" in why


def test_missing_or_none_analysis_is_treated_as_empty():
    assert pipeline.notify_decision({}, notify=True)[0] is False
    assert pipeline.notify_decision(None, notify=True)[0] is False


def test_send_and_reason_are_separate_values():
    """한 값에 섞으면 '요청 없음'과 '보내도 됨'이 같은 빈 문자열이 된다.

    처음에 '안 보낼 이유' 문자열 하나로 뒀다가 정상 경로가 그대로 발송으로
    떨어졌다 — 끄려고 넣은 코드가 켜는 코드가 될 뻔했다.
    """
    quiet = pipeline.notify_decision({"analyses": [{"t": 1}]})          # 요청 없음
    ready = pipeline.notify_decision({"analyses": [{"t": 1}]}, notify=True)
    assert quiet[1] == ready[1] == ""     # 사유는 둘 다 비었는데
    assert quiet[0] != ready[0]           # 판정은 반대여야 한다
