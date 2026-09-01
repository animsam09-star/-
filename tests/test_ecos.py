"""ECOS 어댑터 검증.

여기서 고정하는 것은 계수 계산이 아니라 **모르는 것을 모른다고 말하는 동작**이다.
ECOS는 오류를 HTTP 상태가 아니라 본문의 RESULT 블록으로 돌려주므로, 그걸 안 보면
'인증키 오류'와 '결과 0건'이 호출부에서 똑같아 보인다.
"""

import pytest

from src import ecos


class TestErrorsAreNotSilent:
    def _resp(self, payload, status=200):
        class Resp:
            status_code = status
            def json(self): return payload
        return Resp()

    def test_result_block_becomes_an_exception(self, monkeypatch):
        """RESULT가 오면 정상 응답이 아니다. HTTP 200으로 온다."""
        monkeypatch.setenv(ecos.ENV_KEY, "k")
        monkeypatch.setattr(ecos.requests, "get",
                            lambda *a, **k: self._resp(
                                {"RESULT": {"CODE": "INFO-100",
                                            "MESSAGE": "인증키가 유효하지 않습니다."}}))
        with pytest.raises(ecos.EcosError) as exc:
            ecos.fetch("StatisticTableList")
        assert "INFO-100" in str(exc.value)
        assert "인증키가 유효하지 않습니다" in str(exc.value)

    def test_missing_row_reports_actual_keys(self, monkeypatch):
        monkeypatch.setenv(ecos.ENV_KEY, "k")
        monkeypatch.setattr(ecos.requests, "get",
                            lambda *a, **k: self._resp(
                                {"StatisticTableList": {"list_total_count": 0}}))
        with pytest.raises(ecos.EcosError) as exc:
            ecos.fetch("StatisticTableList")
        assert "list_total_count" in str(exc.value)

    def test_missing_key_names_the_env_var_and_where_to_get_it(self, monkeypatch):
        for name in ecos.ENV_KEYS:
            monkeypatch.delenv(name, raising=False)
        with pytest.raises(ecos.EcosError) as exc:
            ecos.fetch("StatisticTableList")
        msg = str(exc.value)
        assert all(n in msg for n in ecos.ENV_KEYS)
        assert "ecos.bok.or.kr" in msg

    @pytest.mark.parametrize("name", ecos.ENV_KEYS)
    def test_any_accepted_name_works(self, monkeypatch, name):
        """이름이 어긋나 '키 없음'으로 건너뛰면 산출물이 비고, 그건 리포트에서
        '산업 간 연관이 없다'와 구분되지 않는다."""
        for n in ecos.ENV_KEYS:
            monkeypatch.delenv(n, raising=False)
        monkeypatch.setenv(name, "k")
        assert ecos.has_key()
        assert ecos.credential_kind() == name

    def test_credential_kind_never_returns_the_value(self, monkeypatch):
        for n in ecos.ENV_KEYS:
            monkeypatch.delenv(n, raising=False)
        monkeypatch.setenv(ecos.ENV_KEYS[0], "super-secret-value")
        assert "super-secret" not in ecos.credential_kind()

    def test_top_level_key_is_not_assumed(self, monkeypatch):
        """서비스마다 최상위 키 이름이 다르다. 이름을 박으면 서비스가 바뀔 때 깨진다."""
        monkeypatch.setenv(ecos.ENV_KEY, "k")
        rows = [{"STAT_CODE": "301Y013", "STAT_NAME": "산업연관표"}]
        monkeypatch.setattr(ecos.requests, "get",
                            lambda *a, **k: self._resp(
                                {"어떤이름이든": {"list_total_count": 1, "row": rows}}))
        assert ecos.fetch("어떤서비스") == rows


class TestDiscoveryDoesNotHardcodeTableCodes:
    """통계표코드를 소스에 박지 않는다.

    박아 두면 코드가 바뀌었을 때 예외가 아니라 **빈 결과**가 나오고, 그건
    '산업 간 연관이 없다'와 구분되지 않는다. 목록을 조회해 찾는다.
    """

    ROWS = [
        {"STAT_CODE": "301Y013", "STAT_NAME": "산업연관표(2020년 기준)", "CYCLE": "A"},
        {"STAT_CODE": "999X999", "STAT_NAME": "소비자물가지수", "CYCLE": "M"},
        {"STAT_CODE": "301Y020", "STAT_NAME": "생산유발계수", "CYCLE": "A"},
    ]

    def test_matches_are_found_by_name_not_by_code(self, monkeypatch):
        monkeypatch.setattr(ecos, "fetch", lambda *a, **k: self.ROWS)
        out = ecos.discover()
        names = [m["STAT_NAME"] for m in out["matches"]]
        assert "산업연관표(2020년 기준)" in names
        assert "생산유발계수" in names
        assert "소비자물가지수" not in names

    def test_actual_fields_are_reported(self, monkeypatch):
        """첫 라이브 실행에서 이 목록을 보고 이후 코드를 확정한다."""
        monkeypatch.setattr(ecos, "fetch", lambda *a, **k: self.ROWS)
        out = ecos.discover()
        assert out["fields"] == ["CYCLE", "STAT_CODE", "STAT_NAME"]
        assert out["total"] == 3

    def test_empty_response_is_not_a_crash(self, monkeypatch):
        monkeypatch.setattr(ecos, "fetch", lambda *a, **k: [])
        assert ecos.discover()["match_count"] == 0

    def test_no_table_code_is_hardcoded_in_the_module(self):
        import inspect
        import re
        src = inspect.getsource(ecos)
        # '301Y013' 같은 통계표코드 형태가 소스에 들어오면 잡는다
        assert not re.search(r"['\"]\d{3}[A-Z]\d{3}['\"]", src), \
            "통계표코드가 소스에 박혔다 — 바뀌면 빈 결과가 되어 '연관 없음'과 구분되지 않는다"
