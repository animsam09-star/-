"""KRX OpenAPI 어댑터 검증.

실제 응답 필드명을 문서 없이 단정할 수 없으므로, 여기서 고정하는 것은
**모르는 것을 모른다고 말하는 동작**이다. 잘못 짚은 필드가 조용히 빈 컬럼이
되면 수익률이 전부 NaN이 되고, 예외 없이 틀린 결과가 나온다.
"""

import pandas as pd
import pytest

from src import krx_api as api

# 실제 응답을 본떠 만든 레코드(필드명은 확정 전 가정값)
RECORDS = [
    {"BAS_DD": "20260724", "ISU_SRT_CD": "005930", "ISU_ABBRV": "삼성전자",
     "TDD_CLSPRC": 71000.0, "ACC_TRDVOL": 12000000, "ACC_TRDVAL": 850000000000.0,
     "MKTCAP": 423000000000000.0},
    {"BAS_DD": "20260724", "ISU_SRT_CD": "000660", "ISU_ABBRV": "SK하이닉스",
     "TDD_CLSPRC": 180000.0, "ACC_TRDVOL": 3000000, "ACC_TRDVAL": 540000000000.0,
     "MKTCAP": 131000000000000.0},
]


class TestFieldResolution:
    def test_resolves_known_candidate(self):
        assert api.resolve_field(RECORDS, "close") == "TDD_CLSPRC"
        assert api.resolve_field(RECORDS, "ticker") == "ISU_SRT_CD"

    def test_prefers_earlier_candidate(self):
        both = [{"ISU_SRT_CD": "005930", "ISU_CD": "KR7005930003"}]
        assert api.resolve_field(both, "ticker") == "ISU_SRT_CD"

    def test_unknown_field_raises_with_actual_keys(self):
        """조용히 None을 돌려주면 빈 컬럼이 되고 결과가 예외 없이 틀린다.

        예외 메시지에 **실제 필드 목록**이 있어야 첫 실행에서 스키마를 확정할 수 있다.
        """
        records = [{"SOME_OTHER_CD": "005930", "WEIRD_PRC": 100}]
        with pytest.raises(api.KrxApiError) as exc:
            api.resolve_field(records, "close")
        msg = str(exc.value)
        assert "WEIRD_PRC" in msg, "실제 응답 필드가 메시지에 없다"
        assert "FIELD_CANDIDATES" in msg, "고치는 방법이 안내되지 않았다"

    def test_optional_field_returns_none(self):
        assert api.resolve_field(RECORDS, "sector", required=False) is None


class TestToFrame:
    def test_maps_to_canonical_columns(self):
        df = api.to_frame(RECORDS, ["ticker", "name", "close", "volume", "value"])
        assert list(df.columns) == ["name", "close", "volume", "value"]
        assert df.loc["005930", "close"] == 71000.0

    def test_ticker_keeps_leading_zeros(self):
        """티커가 숫자로 변환되면 '005930'이 5930이 되어 다른 소스와 조인이 어긋난다.

        예외는 나지 않고, 조인 결과만 조용히 비어 버린다.
        """
        records = [{"ISU_SRT_CD": 5930, "TDD_CLSPRC": 71000.0}]
        df = api.to_frame(records, ["ticker", "close"])
        assert list(df.index) == ["005930"]

    def test_missing_optional_column_is_omitted_not_blank(self):
        records = [{"ISU_SRT_CD": "005930", "TDD_CLSPRC": 71000.0}]
        df = api.to_frame(records, ["ticker", "close", "market_cap"],
                          optional=("market_cap",))
        assert "market_cap" not in df.columns

    def test_empty_records_give_empty_frame(self):
        df = api.to_frame([], ["ticker", "close"])
        assert df.empty


class TestAuth:
    def test_missing_key_names_the_env_var_and_where_to_get_it(self, monkeypatch):
        monkeypatch.delenv(api.ENV_KEY, raising=False)
        with pytest.raises(api.KrxApiError) as exc:
            api.fetch_raw("sto", "stk_bydd_trd", "20260724")
        msg = str(exc.value)
        assert api.ENV_KEY in msg
        assert "openapi.krx.co.kr" in msg


class TestRequestShape:
    def test_sends_auth_key_and_basdd(self, monkeypatch):
        monkeypatch.setenv(api.ENV_KEY, "test-key")
        seen = {}

        class Resp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return {"OutBlock_1": RECORDS}

        def fake_get(url, params=None, timeout=None):
            seen.update(url=url, params=params)
            return Resp()

        monkeypatch.setattr(api.requests, "get", fake_get)
        out = api.fetch_raw("sto", "stk_bydd_trd", "20260724")
        assert seen["url"] == f"{api.BASE_URL}/sto/stk_bydd_trd"
        assert seen["params"] == {"AUTH_KEY": "test-key", "basDd": "20260724"}
        assert len(out) == 2

    @pytest.mark.parametrize("code", [400, 401, 403, 429, 500])
    def test_error_body_is_always_surfaced(self, monkeypatch, code):
        """KRX가 적어 보낸 사유를 버리면 상태 코드만 보고 추측하게 된다.

        실제로 403이 났을 때 본문을 안 보여줘서 원인을 좁히지 못했다.
        """
        monkeypatch.setenv(api.ENV_KEY, "test-key")

        class Resp:
            status_code = code
            text = '{"errMsg":"서비스 승인 대기중","errCode":"E403"}'
            def json(self): return {}

        monkeypatch.setattr(api.requests, "get", lambda *a, **k: Resp())
        with pytest.raises(api.KrxApiError) as exc:
            api.fetch_raw("sto", "stk_bydd_trd", "20260724", retries=1)
        assert "서비스 승인 대기중" in str(exc.value), f"{code}에서 본문이 누락됐다"

    def test_403_lists_the_plausible_causes(self, monkeypatch):
        """403은 키가 인식된 상태라 401과 조치가 다르다.
        승인 대기 / IP 제한 / 한도 소진을 구분해 안내해야 한다."""
        monkeypatch.setenv(api.ENV_KEY, "test-key")

        class Resp:
            status_code = 403
            text = "forbidden"
            def json(self): return {}

        monkeypatch.setattr(api.requests, "get", lambda *a, **k: Resp())
        with pytest.raises(api.KrxApiError) as exc:
            api.fetch_raw("sto", "stk_bydd_trd", "20260724", retries=1)
        msg = str(exc.value)
        assert "승인 대기" in msg and "IP" in msg

    def test_missing_outblock_reports_actual_keys(self, monkeypatch):
        monkeypatch.setenv(api.ENV_KEY, "test-key")

        class Resp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return {"errMsg": "no service", "errCode": "E001"}

        monkeypatch.setattr(api.requests, "get", lambda *a, **k: Resp())
        with pytest.raises(api.KrxApiError) as exc:
            api.fetch_raw("sto", "stk_bydd_trd", "20260724")
        assert "errMsg" in str(exc.value)


def test_market_ohlcv_returns_canonical_frame(monkeypatch):
    monkeypatch.setenv(api.ENV_KEY, "test-key")
    monkeypatch.setattr(api, "fetch_raw", lambda *a, **k: RECORDS)
    df = api.get_market_ohlcv("20260724", market="KOSPI")
    assert isinstance(df, pd.DataFrame)
    assert {"close", "volume", "value"} <= set(df.columns)
    assert not isinstance(df.index, pd.DatetimeIndex), "티커 인덱스여야 한다"
    assert df.index.tolist() == ["005930", "000660"]
