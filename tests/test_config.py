"""설정·밸류체인 맵·워크플로우 정합성.

이 검증은 원래 CI 워크플로우 스크립트에만 있었다. 그래서 **로컬에서 실행할 방법이
없었고**, valuechain/에 별칭 파일을 추가했을 때 `graph_build.from_valuechain`에는
'_' 접두 제외 규칙을 넣으면서 CI 스크립트에는 넣지 않아 푸시 후에야 깨졌다.

같은 규칙이 두 곳에 따로 적혀 있으면 반드시 갈라진다. 여기로 옮겨서 로컬
`pytest`와 CI가 같은 코드를 돌게 한다.
"""

import pathlib

import yaml

from src import graph_build

ROOT = pathlib.Path(__file__).resolve().parent.parent
VALUECHAIN = ROOT / "valuechain"


def _cfg() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


class TestConfig:
    def test_required_sections_present(self):
        required = {"screener", "horizontal", "graph", "dart",
                    "collect", "analyze", "report"}
        assert not required - _cfg().keys()

    def test_factor_specs_are_complete(self):
        for spec in _cfg()["horizontal"]["factors"]:
            assert spec.get("name") and spec.get("keywords"), f"팩터 정의 오류: {spec}"

    def test_edge_confidence_floor_does_not_erase_the_vertical_axis(self):
        """밸류체인 2홉 경로의 신뢰도는 0.5×0.5=0.25다.

        하한을 그보다 높이면 수직축이 통째로 사라지는데, 예외도 경고도 나지 않고
        그냥 후방·전방이 빈 채로 리포트가 나온다.
        """
        floor = _cfg()["graph"]["min_edge_confidence"]
        two_hop = graph_build.VALUECHAIN_CONFIDENCE ** 2
        assert floor <= two_hop, (
            f"min_edge_confidence({floor})가 밸류체인 2홉 신뢰도({two_hop})보다 높습니다")

    def test_dart_model_is_pinned(self):
        assert _cfg()["dart"].get("model"), "dart.model이 없으면 추출이 실행되지 않습니다"


class TestValueChainMaps:
    def _maps(self):
        return [f for f in VALUECHAIN.glob("*.yaml") if not f.name.startswith("_")]

    def test_every_map_declares_an_industry(self):
        maps = self._maps()
        assert maps, "밸류체인 맵이 하나도 없습니다"
        for f in maps:
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
            assert data.get("industry"), f"{f.name}: industry 누락"

    def test_underscore_files_are_not_treated_as_maps(self, capsys, tmp_path):
        """'_' 접두 파일은 부속 파일이다. from_valuechain과 같은 규칙이어야 한다.

        규칙이 갈라지면 별칭 파일이 맵으로 읽혀 경고가 뜨고, CI의 맵 검증이 깨진다.
        (실제로 그렇게 깨졌다.)
        """
        from src import universe
        uni = universe.from_entries("20260101", {})

        # 부속 파일만 있는 디렉터리 — 맵으로 읽히면 경고가 찍히고 산업이 생긴다
        (tmp_path / "_aliases.yaml").write_text("레미콘: 시멘트\n", encoding="utf-8")
        edges, report = graph_build.from_valuechain(uni, "20260101", tmp_path)
        assert edges == [] and report["industries"] == []
        assert "_aliases" not in capsys.readouterr().out

        # 실제 디렉터리에서도 부속 파일 경고가 없어야 한다
        graph_build.from_valuechain(uni, "20260101", VALUECHAIN)
        out = capsys.readouterr().out
        for f in VALUECHAIN.glob("_*.yaml"):
            assert f.name not in out, f"{f.name}이 맵으로 읽혔습니다"

    def test_segments_are_well_formed(self):
        for f in self._maps():
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
            for block in ("upstream", "downstream"):
                for seg in data.get(block) or []:
                    assert seg.get("segment"), f"{f.name}: {block}에 segment 없는 항목"
                    assert isinstance(seg.get("companies") or [], list), \
                        f"{f.name}: {seg['segment']}의 companies가 목록이 아닙니다"


class TestIndustryAliases:
    ALIASES = VALUECHAIN / "_industry_aliases.yaml"

    def _data(self) -> dict:
        if not self.ALIASES.exists():
            return {}
        return yaml.safe_load(self.ALIASES.read_text(encoding="utf-8")) or {}

    def test_is_a_flat_string_mapping(self):
        data = self._data()
        assert isinstance(data, dict)
        bad = {k: v for k, v in data.items()
               if not isinstance(k, str) or not isinstance(v, str)}
        assert not bad, f"잘못된 별칭: {bad}"

    def test_no_chained_aliases(self):
        """별칭이 다른 별칭을 가리키면 한 번에 접히지 않아 산업이 갈라진다.

        해석은 1회만 수행하므로 A→B→C는 A를 B에서 멈춘다. 예외는 나지 않는다.
        """
        data = self._data()
        chained = {k: v for k, v in data.items() if v in data and data[v] != v}
        assert not chained, f"별칭이 다른 별칭을 가리킵니다: {chained}"

    def test_no_self_aliases(self):
        data = self._data()
        assert not {k for k, v in data.items() if k == v}, "자기 자신을 가리키는 별칭"

    def test_aliases_actually_fold(self):
        """별칭 표에 적었는데 정규화 단계에서 안 접히면 적은 의미가 없다."""
        from src import dart_extract as dx
        data = self._data()
        if not data:
            return
        vocab = dx.IndustryVocab(known=sorted(set(data.values())), aliases=data)
        for src, dst in data.items():
            assert vocab.resolve(src) == dst, f"별칭 미적용: {src} → {dst}"


def test_workflows_are_valid_yaml():
    files = list((ROOT / ".github" / "workflows").glob("*.yml"))
    assert files
    for f in files:
        assert yaml.safe_load(f.read_text(encoding="utf-8")), f"{f.name}: 빈 워크플로우"
