"""뉴스가 0건일 때 왜 0건인지 말해야 한다.

원인 분석 후보 20건이 전부 '뉴스 없음'이었는데, 키 미등록 때문인지 정말 기사가
없어서인지 로그만 봐서는 알 수 없었다. 원인 분석이 가장 크게 기대는 입력이라
그 구분이 안 되면 진단이 막힌다.
"""

import requests

from src import collect


def _reset():
    collect._NEWS_NOTICE_SHOWN = False


def test_missing_key_is_named(monkeypatch, capsys):
    _reset()
    monkeypatch.delenv("NAVER_CLIENT_ID", raising=False)
    monkeypatch.delenv("NAVER_CLIENT_SECRET", raising=False)
    assert collect.fetch_news("삼성전자", {"news_days": 3}) == []
    out = capsys.readouterr().out
    assert "NAVER_CLIENT_ID" in out and "NAVER_CLIENT_SECRET" in out


def test_only_one_missing_key_is_named(monkeypatch, capsys):
    _reset()
    monkeypatch.setenv("NAVER_CLIENT_ID", "x")
    monkeypatch.delenv("NAVER_CLIENT_SECRET", raising=False)
    collect.fetch_news("삼성전자", {"news_days": 3})
    out = capsys.readouterr().out
    assert "NAVER_CLIENT_SECRET" in out
    assert "NAVER_CLIENT_ID," not in out, "설정된 키를 미설정으로 적었다"


def test_notice_is_printed_once_not_per_stock(monkeypatch, capsys):
    """종목마다 찍으면 30줄이 된다."""
    _reset()
    monkeypatch.delenv("NAVER_CLIENT_ID", raising=False)
    monkeypatch.delenv("NAVER_CLIENT_SECRET", raising=False)
    for _ in range(5):
        collect.fetch_news("삼성전자", {"news_days": 3})
    assert capsys.readouterr().out.count("NAVER_CLIENT_ID") == 1


def test_auth_failure_says_it_is_auth_not_the_stock(monkeypatch, capsys):
    """401/403은 종목 이름 문제가 아니라 키·API 설정 문제다."""
    _reset()
    monkeypatch.setenv("NAVER_CLIENT_ID", "x")
    monkeypatch.setenv("NAVER_CLIENT_SECRET", "y")

    class Resp:
        status_code = 401
        def raise_for_status(self):
            raise requests.HTTPError("401", response=self)

    monkeypatch.setattr(collect.requests, "get", lambda *a, **k: Resp())
    assert collect.fetch_news("삼성전자", {"news_days": 3}) == []
    out = capsys.readouterr().out
    assert "인증 실패" in out and "검색" in out
