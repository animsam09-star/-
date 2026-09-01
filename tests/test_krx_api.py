"""KRX OpenAPI 어댑터 검증.

잘못 짚은 필드가 조용히 빈 컬럼이 되면 수익률이 전부 NaN이 되고, 예외 없이
틀린 결과가 나온다. 그래서 여기서 고정하는 것은 **모르는 것을 모른다고 말하는
동작**이다.

LIVE_RECORD는 2026-07-27 실제 응답에서 가져온 필드 구성이다. 가정이 아니다.
"""

import time

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

# 2026-07-27 stk_bydd_trd 라이브 응답의 실제 필드 구성.
# 수치가 쉼표 낀 문자열로 온다는 점이 핵심이다.
LIVE_RECORD = {
    "BAS_DD": "20260727", "ISU_CD": "005930", "ISU_NM": "삼성전자",
    "MKT_NM": "KOSPI", "SECT_TP_NM": "", "TDD_CLSPRC": "71,000",
    "CMPPREVDD_PRC": "1,000", "FLUC_RT": "1.43", "TDD_OPNPRC": "70,200",
    "TDD_HGPRC": "71,500", "TDD_LWPRC": "70,100", "ACC_TRDVOL": "12,000,000",
    "ACC_TRDVAL": "850,000,000,000", "MKTCAP": "423,000,000,000,000",
    "LIST_SHRS": "5,969,782,550",
}


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


class TestLiveSchema:
    """라이브 응답으로 확정한 계약. 이게 깨지면 조용히 틀리는 대신 여기서 터진다."""

    def test_live_fields_resolve(self):
        for canonical, expected in (("ticker", "ISU_CD"), ("name", "ISU_NM"),
                                    ("close", "TDD_CLSPRC"), ("volume", "ACC_TRDVOL"),
                                    ("value", "ACC_TRDVAL"), ("market_cap", "MKTCAP")):
            assert api.resolve_field([LIVE_RECORD], canonical) == expected

    def test_comma_separated_numbers_become_numeric(self):
        """'71,000'이 문자열로 남으면 종가 비교가 사전순이 되어 수익률이 엉킨다."""
        df = api.to_frame([LIVE_RECORD], ["ticker", "name", "close", "market_cap"])
        assert df.loc["005930", "close"] == 71000
        assert df.loc["005930", "market_cap"] == 423_000_000_000_000
        assert df.loc["005930", "name"] == "삼성전자"

    def test_snapshot_has_the_columns_the_universe_needs(self):
        """종목명·시총이 시세 응답에 있으므로 종목기본정보 API가 필요 없다."""
        snap = api._snapshot_frame([dict(LIVE_RECORD, _MARKET="KOSPI")])
        for col in ("name", "close", "volume", "value", "market_cap", "market"):
            assert col in snap.columns, col


class TestTickerNormalization:
    def test_short_code_keeps_leading_zeros(self):
        assert api.normalize_ticker("005930") == "005930"

    def test_numeric_type_is_zero_padded(self):
        """JSON이 숫자로 오면 5930이 되어 다른 소스와의 조인이 통째로 어긋난다."""
        assert api.normalize_ticker(5930) == "005930"

    def test_standard_code_is_reduced_to_short_code(self):
        """zfill(6)만으로는 12자리 표준코드가 그대로 남는다 — 조용히 틀린다."""
        assert api.normalize_ticker("KR7005930003") == "005930"

    def test_preferred_share_standard_code(self):
        assert api.normalize_ticker("KR7005931001") == "005931"

    def test_unexpected_code_shape_raises_with_samples(self):
        odd = [{"ISU_CD": f"XX{i}", "TDD_CLSPRC": "1"} for i in range(10)]
        with pytest.raises(api.KrxApiError, match="6자리"):
            api.to_frame(odd, ["ticker", "close"])

    def test_alphanumeric_short_codes_are_valid(self):
        """KRX는 6자리 숫자 공간이 차면서 알파벳이 낀 단축코드를 발급한다.

        라이브 ETF 응답 1,150건 중 284건이 '0184E0' 꼴이었다. 검사식이 알파벳을
        끝자리에만 허용해서 이것들이 '이상'으로 잡혔고, 5% 임계를 넘겨 **모든
        날짜의 ETF 시세가 통째로 버려졌다.** 안전장치가 데이터보다 좁으면 고장이다.
        """
        live = ["0184E0", "0182R0", "0182S0", "0103T0", "0198D0", "0131W0",
                "08104K", "005930", "132030"]
        rows = [{"ISU_CD": c, "TDD_CLSPRC": "1,000"} for c in live]
        out = api.to_frame(rows, ["ticker", "close"])
        assert list(out.index) == live

    def test_unnormalized_standard_codes_still_raise(self):
        """검사를 넓히되, 표준코드가 정규화 없이 흘러드는 것은 계속 잡아야 한다."""
        rows = [{"ISU_CD": "KR7005930003XYZ", "TDD_CLSPRC": "1"} for _ in range(10)]
        with pytest.raises(api.KrxApiError, match="6자리"):
            api.to_frame(rows, ["ticker", "close"])


class TestTradingDayWalk:
    """거래일 달력 API가 없으므로 '빈 응답 = 휴장일'로 판정한다."""

    def _stub(self, monkeypatch, trading: set[str]):
        def fake(bas_dd, use_cache=True, markets=("KOSPI", "KOSDAQ")):
            if bas_dd not in trading:
                return pd.DataFrame(columns=api.SNAPSHOT_COLUMNS)
            return pd.DataFrame({"close": [100.0], "volume": [1.0], "value": [1.0]},
                                index=["005930"])
        monkeypatch.setattr(api, "daily_snapshot", fake)

    def test_skips_holidays_and_collects_requested_count(self, monkeypatch):
        trading = {"20260727", "20260724", "20260723"}   # 25·26은 주말
        self._stub(monkeypatch, trading)
        days = [d for d, _ in api.iter_trading_days("20260727", 3, progress_every=0)]
        assert days == ["20260727", "20260724", "20260723"]

    def test_weekends_are_never_requested(self, monkeypatch):
        asked = []

        def fake(bas_dd, use_cache=True, markets=("KOSPI", "KOSDAQ")):
            asked.append(bas_dd)
            return pd.DataFrame({"close": [100.0], "volume": [1.0], "value": [1.0]},
                                index=["005930"])
        monkeypatch.setattr(api, "daily_snapshot", fake)
        list(api.iter_trading_days("20260727", 3, progress_every=0))
        assert "20260725" not in asked and "20260726" not in asked

    def test_running_out_of_days_raises_instead_of_returning_short(self, monkeypatch):
        self._stub(monkeypatch, set())
        with pytest.raises(api.KrxApiError, match="거래일"):
            list(api.iter_trading_days("20260727", 3, max_lookback=10, progress_every=0))

    def test_panel_is_ascending_by_date(self, monkeypatch):
        self._stub(monkeypatch, {"20260727", "20260724", "20260723"})
        dates, close, _, _, latest = api.fetch_panel("20260727", 3)
        assert dates == ["20260723", "20260724", "20260727"]
        assert list(close.index) == dates, "패널이 오름차순이 아니면 수익률 부호가 뒤집힌다"
        assert not latest.empty, "기준일 스냅샷이 있어야 종목 마스터를 만든다"


class TestParallelFetch:
    """순차 조회는 130거래일에 40분이 넘는다(라이브에서 확인). 병렬로 받되
    초당 제한과 최신순 정렬은 유지돼야 한다."""

    def test_weekday_window_covers_holidays(self):
        """거래일 n일을 담으려면 평일을 그보다 넉넉히 잡아야 한다."""
        days = api.weekdays_back("20260728", 130)
        assert len(days) > 130, "여유가 없으면 휴장이 낀 구간에서 모자란다"
        assert days[0] == "20260728", "최신순이어야 한다"
        assert all(pd.Timestamp(d).weekday() < 5 for d in days), "주말이 섞였다"

    def test_results_stay_newest_first_despite_parallelism(self, monkeypatch):
        """병렬 실행이 순서를 흐트러뜨리면 패널 정렬이 깨지고 수익률이 뒤집힌다."""
        import random

        def fake(bas_dd, use_cache=True, markets=("KOSPI", "KOSDAQ")):
            time.sleep(random.random() * 0.01)      # 완료 순서를 일부러 흔든다
            return pd.DataFrame({"close": [100.0], "volume": [1.0], "value": [1.0]},
                                index=["005930"])
        monkeypatch.setattr(api, "daily_snapshot", fake)
        days = [d for d, _ in api.iter_trading_days("20260728", 10, progress_every=0)]
        assert days == sorted(days, reverse=True)
        assert len(days) == 10

    def test_one_bad_date_does_not_sink_the_panel(self, monkeypatch):
        """하루가 실패했다고 130일치를 버리면 안 된다."""
        def fake(bas_dd, use_cache=True, markets=("KOSPI", "KOSDAQ")):
            if bas_dd == "20260724":
                raise api.KrxApiError("일시 오류")
            return pd.DataFrame({"close": [100.0], "volume": [1.0], "value": [1.0]},
                                index=["005930"])
        monkeypatch.setattr(api, "daily_snapshot", fake)
        days = [d for d, _ in api.iter_trading_days("20260728", 5, progress_every=0)]
        assert "20260724" not in days
        assert len(days) == 5, "실패한 하루를 건너뛰고 5일을 채워야 한다"

    def test_rate_limiter_is_held_under_a_lock(self, monkeypatch):
        """락 없이 간격을 계산하면 모든 스레드가 같은 값을 보고 동시에 나간다."""
        monkeypatch.setenv(api.ENV_KEY, "k")
        stamps = []

        class Resp:
            status_code = 200
            def json(self): return {"OutBlock_1": []}

        def fake_get(url, params=None, timeout=None):
            stamps.append(time.monotonic())
            return Resp()

        monkeypatch.setattr(api.requests, "get", fake_get)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda i: api.fetch_raw("sto", "stk_bydd_trd", "2026072%d" % (i % 10)),
                          range(12)))
        gaps = [b - a for a, b in zip(sorted(stamps), sorted(stamps)[1:])]
        assert min(gaps) >= api._RATE_MIN_INTERVAL * 0.8, f"간격 {min(gaps):.3f}s가 너무 촘촘하다"


class TestLongPanelIsNotTruncatedByTheLookbackCap:
    """상한이 고정값이면 긴 패널을 조용히 잘라 낸다.

    실제로 그렇게 실패했다. max_lookback이 500으로 고정돼 있어 924거래일을
    요청했는데 평일 후보를 500개만 훑고 '거래일이 460일뿐'이라며 멈췄다.
    130일 패널에서는 절대 안 걸리다가, 사이클 시차용 긴 패널에서 처음 드러났다.
    상한의 목적은 장기 휴장 때 폭주를 막는 것이지 요청을 잘라내는 게 아니다.
    """

    def _all_weekdays(self, monkeypatch):
        """모든 평일이 거래일인 세계 — 상한 말고는 막을 게 없다."""
        def fake(bas_dd, use_cache=True, markets=("KOSPI", "KOSDAQ")):
            return pd.DataFrame({"close": [100.0], "volume": [1.0], "value": [1.0]},
                                index=["005930"])
        monkeypatch.setattr(api, "daily_snapshot", fake)

    def test_long_request_is_fully_served(self, monkeypatch):
        self._all_weekdays(monkeypatch)
        got = list(api.iter_trading_days("20260727", 924, progress_every=0))
        assert len(got) == 924, f"{len(got)}일에서 잘렸다"

    def test_cap_still_scales_with_the_request(self):
        """상한이 요청량에 비례해야 한다 — 없애 버리면 폭주 방지가 사라진다."""
        assert api.weekdays_back("20260727", 924)[:int(924 * 1.5) + 40] != []
        # 짧은 요청에서는 후보가 상한보다 적어 상한이 무의미해야 한다
        assert len(api.weekdays_back("20260727", 130)) < int(130 * 1.5) + 40

    def test_explicit_cap_is_respected(self, monkeypatch):
        """명시적으로 넘긴 상한은 그대로 지킨다."""
        self._all_weekdays(monkeypatch)
        with pytest.raises(api.KrxApiError, match="거래일이"):
            list(api.iter_trading_days("20260727", 924, max_lookback=100,
                                       progress_every=0))
