"""사슬을 한 단계 더 뒤로 늘린다 — 원료 쪽 끝을 붙인다.

## 왜

'석유화학 ← 정유'까지는 있었는데 그 정유가 원유를 어디서 받는지는 그래프에
없었다. 사슬의 값어치는 길이에 있다. 끝을 안 붙이면 '이 위로는 없다'로 읽히고,
실제로는 파급의 출발점이 거기 있다.

원유·천연가스에는 국내 상장 순수 플레이가 없다. 그래도 노드로 세우는 게 맞다 —
지도가 그 칸을 '비상장'으로 표시하므로, 없는 수혜주를 찾으러 가지 않으면서도
사슬이 어디서 시작하는지는 보인다.

## 규칙은 그대로

관계는 공시 인용에서만. segment를 명시해 엉뚱한 사업부문에 붙지 않게 한다.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 소속(products)이 없으면 관계를 붙일 기준 산업이 없다. 이번 대상은 기존 배치에
# 소속이 없는 종목이 있어 여기서 함께 넣는다.
PRODUCTS = [
    ("096770", "정유", "휘발유·경유·항공유·납사",
     "울산 및 인천 Complex에서 이를 정제하여 휘발유, 경유, 등유, 항공유, 납사 등 연료와 화학제품 원료를 생산", "B"),
    ("010950", "정유", "정유·석유화학·윤활기유",
     "하루 66만 9천배럴의 원유정제능력을 보유한 정유사로, 정유뿐 아니라 석유화학ㆍ윤활기유 등으로 다각화된 사업 포트폴리오를 보유", "B"),
]

# (티커, 방향, 상대산업, 품목/매출처, 사업부문, 인용, 등급)
ROWS = [
    # ---- 사슬의 시작: 원유 → 정유 -----------------------------------------
    ("096770", "upstream", "원유·천연가스", "원유(중동 등 해외 조달)", "정유",
     "석유사업은 석유제품 생산을 위해 원유를 중동 등 전 세계 다양한 공급망을 통해 안정적으로 조달하며", "B"),
    ("010950", "upstream", "원유·천연가스", "원유(정제 투입)", "정유",
     "하루 66만 9천배럴의 원유정제능력을 보유한 정유사", "B"),
    ("010950", "downstream", "석유화학", "석유화학·윤활기유 사업 연계", "정유",
     "정유뿐 아니라 석유화학ㆍ윤활기유 등으로 다각화된 사업 포트폴리오를 보유", "B"),

    # ---- 타이어의 후방을 원료까지: 천연고무 -------------------------------
    ("073240", "upstream", "천연고무·원자재", "천연고무", "타이어",
     "타이어 원재료 천연고무 타이어제조용원재료 461,697", "A"),

    # ---- 화장품 ODM의 후방: 화학 원료 --------------------------------------
    ("192820", "upstream", "석유화학", "1,2-Hexanediol 등 화장품 원료", "화장품 ODM",
     "화장품 원재료 1,2-Hexanediol 방부제", "A"),

    # ---- 기판·패키징의 후방: CCL·페이스트 ----------------------------------
    ("009150", "upstream", "소재(전구체·특수가스·CMP)", "PASTE/POWDER, CCL/PPG, 센서 IC", "기판·패키징",
     "주요 원재료는 PASTE/POWDER로 SHOEI, GUANGBO 등에서 매입하고 있으며", "B"),
]

_KEY = {"upstream": ("material", "cost_share"), "downstream": ("customer", None)}


def main() -> None:
    prior = {}
    for f in ("membership_20260803.json", "membership_20260804.json",
              "membership_20260804b.json"):
        p = ROOT / "data" / f
        if p.exists():
            prior.update(json.loads(p.read_text(encoding="utf-8")))

    out: dict[str, dict] = {}
    for ticker, industry, product, quote, tier in PRODUCTS:
        out.setdefault(ticker, {"products": [], "upstream": [],
                                "downstream": [], "unmapped": ""})
        out[ticker]["products"].append({
            "industry": industry, "product": product, "quote": quote,
            "tier": tier, "revenue_share": None})

    for ticker, direction, industry, what, segment, quote, tier in ROWS:
        e = out.setdefault(ticker, {
            "products": list((prior.get(ticker) or {}).get("products") or []),
            "upstream": [], "downstream": [], "unmapped": ""})
        name_key, share_key = _KEY[direction]
        item = {"industry": industry, "quote": quote, "tier": tier,
                name_key: what, "segment": segment}
        if share_key:
            item[share_key] = None
        e[direction].append(item)

    missing = [t for t, v in out.items() if not v["products"]]
    if missing:
        print(f"  ::warning:: 소속이 없는 종목 {missing} — 관계가 만들어지지 않습니다")
    path = ROOT / "data" / "chain_20260804.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    ups = sum(len(v["upstream"]) for v in out.values())
    downs = sum(len(v["downstream"]) for v in out.values())
    print(f"{path} — 종목 {len(out)}건, 후방 {ups}개 / 전방 {downs}개")


if __name__ == "__main__":
    main()
