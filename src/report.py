"""리포트 생성: 일자별 HTML 대시보드(GitHub Pages용)와 텔레그램 요약 텍스트.

## 왜 라이브러리를 안 쓰는가

차트를 CDN(Chart.js 등)으로 불러오면 그 호스트가 죽거나 정책이 바뀐 날 리포트가
조용히 빈 화면이 된다. 자동 생성물이라 그날 아무도 안 볼 수도 있다. 그래서
막대·경로 다이어그램을 전부 **인라인 SVG**로 그린다. 의존성이 0이면 깨질 곳도 없다.

## 무엇을 보여 주는가

이 파이프라인의 산출물은 '오른 종목 목록'이 아니라 **파급 경로**다. 그래서 화면의
중심도 표가 아니라 경로 다이어그램이다 — 어떤 종목이 왜 올랐고, 그 영향이 어느
산업을 거쳐 어느 종목까지 가는지, 그중 아직 안 움직인 게 무엇인지.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = ROOT / "reports"

CSS = """
:root { --bg:#f6f6f9; --card:#fff; --fg:#16161f; --muted:#6a6a80; --line:#e4e4ee;
        --up:#c0392b; --down:#2471a3; --accent:#5b4b8a; --chip:#efeaf9;
        --good:#1e8449; --warn:#b7791f; --grid:#f0f0f6; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#101018; --card:#1b1b26; --fg:#e9e9f2; --muted:#9494ab; --line:#2f2f43;
          --up:#ff7f6d; --down:#69b4ff; --accent:#ad9bea; --chip:#2a2340;
          --good:#4ade80; --warn:#fbbf24; --grid:#22222f; } }
* { box-sizing:border-box; margin:0; }
body { background:var(--bg); color:var(--fg);
       font-family:'Apple SD Gothic Neo','Malgun Gothic',-apple-system,sans-serif;
       line-height:1.6; padding:0 0 48px; }
main { max-width:1080px; margin:0 auto; padding:0 16px; }
header { background:var(--card); border-bottom:1px solid var(--line); padding:22px 16px 16px;
         margin-bottom:22px; }
header .inner { max-width:1080px; margin:0 auto; }
h1 { font-size:1.45rem; letter-spacing:-.01em; }
h2 { font-size:1.1rem; margin:34px 0 12px; color:var(--accent);
     display:flex; align-items:center; gap:8px; }
h2::after { content:''; flex:1; height:1px; background:var(--line); }
h3 { font-size:1.02rem; margin-bottom:6px; }
.sub { color:var(--muted); font-size:.88rem; }
a { color:var(--accent); }

/* KPI 스트립 — 오늘 무슨 일이 있었는지 한 줄로 */
.kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(132px,1fr)); gap:10px;
        margin-top:14px; }
.kpi { background:var(--bg); border:1px solid var(--line); border-radius:10px; padding:10px 12px; }
.kpi .v { font-size:1.35rem; font-weight:700; line-height:1.2; }
.kpi .l { font-size:.74rem; color:var(--muted); }

nav.jump { display:flex; flex-wrap:wrap; gap:6px; margin-top:14px; }
nav.jump a { font-size:.78rem; background:var(--chip); color:var(--accent);
             border-radius:99px; padding:3px 11px; text-decoration:none; }

.card { background:var(--card); border:1px solid var(--line); border-radius:12px;
        padding:16px 18px; margin-bottom:12px; }
.grid2 { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:12px; }
.chip { display:inline-block; background:var(--chip); color:var(--accent); border-radius:99px;
        padding:1px 10px; font-size:.76rem; margin-left:6px; vertical-align:middle; }
.up { color:var(--up); font-weight:600; } .down { color:var(--down); font-weight:600; }
.muted { color:var(--muted); font-size:.85rem; }
table { width:100%; border-collapse:collapse; font-size:.86rem; }
th,td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); white-space:nowrap; }
th { color:var(--muted); font-weight:600; }
td.wide { white-space:normal; }
.scroll { overflow-x:auto; }
.path { border-left:3px solid var(--accent); padding:6px 12px; margin:8px 0; }
ul.dates { list-style:none; } ul.dates li { padding:6px 0; border-bottom:1px solid var(--line); }
.badge-mi { color:var(--good); font-weight:600; } .badge-gi { color:var(--muted); }
svg { display:block; max-width:100%; height:auto; }
.legend { display:flex; gap:14px; flex-wrap:wrap; font-size:.76rem; color:var(--muted);
          margin:6px 0 2px; }
.legend i { display:inline-block; width:9px; height:9px; border-radius:2px; margin-right:4px; }
"""


def _pct(v) -> str:
    if v is None:
        return "-"
    cls = "up" if v > 0 else "down"
    return f'<span class="{cls}">{v:+.1%}</span>'


def _esc(s) -> str:
    return html.escape(str(s or ""))


# ---- SVG 조각 ------------------------------------------------------------

def _diverging_bar(value: float | None, max_abs: float,
                   width: int = 130, height: int = 13) -> str:
    """0을 가운데 두고 좌우로 뻗는 막대.

    부호가 이 프로젝트의 핵심이라(구리 상승 = 제련 수혜 / 전선 피해) 길이만 있는
    막대는 쓰지 않는다. 오른쪽이 양, 왼쪽이 음이다.
    """
    if value is None or not max_abs:
        return '<span class="muted">-</span>'
    half = width / 2
    frac = max(-1.0, min(1.0, value / max_abs))
    w = abs(frac) * half
    x = half if frac >= 0 else half - w
    color = "var(--up)" if frac >= 0 else "var(--down)"
    return (f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
            f'role="img" aria-label="{value:+.1%}">'
            f'<rect x="0" y="0" width="{width}" height="{height}" fill="var(--grid)" rx="3"/>'
            f'<rect x="{x:.1f}" y="0" width="{w:.1f}" height="{height}" fill="{color}" rx="2"/>'
            f'<line x1="{half}" y1="0" x2="{half}" y2="{height}" '
            f'stroke="var(--line)" stroke-width="1"/></svg>')


_ROW_H = 26
_PAD = 12


def _ripple_svg(source: str, paths: list[dict], max_bens: int = 4) -> str:
    """파급 경로 다이어그램: 종목 → 산업 → 종목.

    이 파이프라인의 산출물이 '오른 종목 목록'이 아니라 경로이므로, 화면의 중심도
    표가 아니라 이 그림이어야 한다. 수혜(빨강)와 피해(파랑)를 색으로 가른다 —
    같은 동인이라도 위치에 따라 부호가 갈리는 것이 이 분석의 요점이다.
    """
    rows: list[tuple[str, str, dict]] = []
    for p in paths or []:
        bens = (p.get("beneficiaries") or [])[:max_bens] or [{}]
        for b in bens:
            rows.append((p.get("direction") or "", p.get("target_industry") or "", b))
    if not rows:
        return ""

    h = _PAD * 2 + _ROW_H * len(rows)
    w = 640
    x_src, x_ind, x_ben = 8, 210, 400
    mid = h / 2

    out = [f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" '
           f'aria-label="{_esc(source)} 파급 경로">']

    # 왼쪽: 출발 종목
    out.append(f'<rect x="{x_src}" y="{mid - 15:.0f}" width="180" height="30" rx="7" '
               f'fill="var(--chip)" stroke="var(--accent)"/>'
               f'<text x="{x_src + 90}" y="{mid + 5:.0f}" text-anchor="middle" '
               f'font-size="13" font-weight="600" fill="var(--accent)">{_esc(source[:14])}</text>')

    seen_ind: dict[str, float] = {}
    for i, (direction, industry, b) in enumerate(rows):
        y = _PAD + _ROW_H * i + _ROW_H / 2
        impact = b.get("impact")
        color = "var(--down)" if impact == "피해" else "var(--up)"

        # 종목 → 산업 (같은 산업이 여러 줄이면 첫 줄에만 상자를 그린다)
        if industry not in seen_ind:
            seen_ind[industry] = y
            out.append(
                f'<path d="M188 {mid:.0f} C 200 {mid:.0f}, 200 {y:.0f}, {x_ind} {y:.0f}" '
                f'fill="none" stroke="var(--line)" stroke-width="1.5"/>')
            out.append(
                f'<text x="{x_ind - 6}" y="{y - 3:.0f}" text-anchor="end" font-size="9.5" '
                f'fill="var(--muted)">{_esc(direction)}</text>')
            out.append(
                f'<rect x="{x_ind}" y="{y - 11:.0f}" width="170" height="22" rx="5" '
                f'fill="none" stroke="var(--accent)" stroke-dasharray="3 2"/>'
                f'<text x="{x_ind + 85}" y="{y + 4:.0f}" text-anchor="middle" font-size="11.5" '
                f'fill="var(--fg)">{_esc(industry[:13])}</text>')
        y_ind = seen_ind[industry]

        name = b.get("name")
        if not name:
            continue
        gap = b.get("gap")
        tag = ""
        if b.get("priced_in") == "미반영":
            tag = " ●"
        label = f'{name[:12]}{tag}'
        sub = f'{gap:+.0%}' if gap is not None else ""

        out.append(
            f'<path d="M{x_ind + 170} {y_ind:.0f} C {x_ind + 190} {y_ind:.0f}, '
            f'{x_ben - 20} {y:.0f}, {x_ben} {y:.0f}" '
            f'fill="none" stroke="{color}" stroke-width="1.5" opacity=".65"/>')
        out.append(f'<circle cx="{x_ben}" cy="{y:.0f}" r="3.5" fill="{color}"/>')
        out.append(f'<text x="{x_ben + 10}" y="{y + 4:.0f}" font-size="11.5" '
                   f'fill="var(--fg)">{_esc(label)}</text>')
        if sub:
            out.append(f'<text x="{w - 8}" y="{y + 4:.0f}" text-anchor="end" font-size="10.5" '
                       f'fill="var(--muted)">갭 {sub}</text>')

    out.append("</svg>")
    legend = ('<div class="legend">'
              '<span><i style="background:var(--up)"></i>수혜</span>'
              '<span><i style="background:var(--down)"></i>피해</span>'
              '<span>● 미반영</span></div>')
    return "".join(out) + legend


# ---- 조각 렌더 -----------------------------------------------------------

def _impact_tag(b: dict) -> str:
    if b.get("impact") == "피해":
        return ' <span class="down">[피해]</span>'
    return ' <span class="up">[수혜]</span>' if b.get("impact") else ""


def _provenance_tag(b: dict) -> str:
    """후보가 관계 그래프에서 나왔는지, LLM이 새로 만든 건지 드러낸다.

    둘을 구분해 보여 줘야 밸류체인 맵이 값을 하는지 사람이 눈으로도 판단할 수 있다.
    """
    if b.get("via"):
        return f' <span class="muted">[{_esc(b["via"])}]</span>'
    if b.get("graph_backed") is False:
        return ' <span class="muted">[그래프 밖]</span>'
    return ""


def _beneficiary_row(b: dict) -> str:
    bits = []
    if b.get("ret20") is not None:
        bits.append(f'20일 {b["ret20"]:+.1%}')
    if b.get("gap") is not None:
        bits.append(f'갭 {b["gap"]:+.1%}')
    meta = f' <span class="muted">({" · ".join(bits)})</span>' if bits else ""
    return (f'<li><b>{_esc(b["name"])}</b>{_impact_tag(b)}{_provenance_tag(b)}{meta} — '
            f'{_esc(b.get("reason") or b.get("comment", ""))}</li>')


def render_kpis(candidates: list[dict], analysis: dict, horizontal: dict) -> str:
    """오늘 무슨 일이 있었는지 숫자 몇 개로. 스크롤하기 전에 보이는 것들."""
    gaps = (horizontal or {}).get("group_gaps") or {}
    best_gap = 0.0
    for info in gaps.values():
        for m in info.get("members", []):
            best_gap = max(best_gap, m.get("gap") or 0.0)

    vc = (analysis or {}).get("valuechain_coverage") or {}
    prov = (analysis or {}).get("provenance") or {}
    ideas = ((analysis or {}).get("synthesis") or {}).get("ideas") or []

    cells = [("후보 종목", f"{len(candidates)}", "스크리닝 통과"),
             ("파급 아이디어", f"{len(ideas)}", "종합 분석")]
    if gaps:
        cells.append(("최대 미반영 갭", f"{best_gap:+.1%}", "아직 안 따라온 종목"))
        cells.append(("그룹", f"{len(gaps)}", "갭 산출 대상"))
    if vc.get("edges"):
        cells.append(("밸류체인 엣지", f"{vc['edges']}", f"산업 {len(vc.get('industries', []))}개"))
    if prov.get("total"):
        cells.append(("그래프 기반", f"{prov['graph_backed']}/{prov['total']}",
                      "근거 있는 후보 비율"))

    items = "".join(f'<div class="kpi"><div class="v">{_esc(v)}</div>'
                    f'<div class="l">{_esc(l)}</div>'
                    f'<div class="l">{_esc(s)}</div></div>' for l, v, s in cells)
    return f'<div class="kpis">{items}</div>'


def render_horizontal(horizontal: dict, names: dict[str, str], top_groups: int = 8,
                      top_members: int = 6) -> str:
    """가장 크게 움직인 그룹과 그 안에서 아직 안 따라온 종목."""
    gaps = (horizontal or {}).get("group_gaps") or {}
    if not gaps:
        return ""

    ranked = sorted(gaps.items(), key=lambda kv: abs(kv[1]["group_move"]), reverse=True)[:top_groups]
    blocks = []
    for g, info in ranked:
        members = info["members"][:top_members]
        scale = max([abs(m.get("gap") or 0) for m in members] + [0.02])
        rows = ""
        for m in members:
            nm = names.get(m["ticker"], m["ticker"])
            cls = "badge-mi" if m["gap"] > 0.03 else "badge-gi"
            rows += (f"<tr><td class='wide'>{_esc(nm)}</td><td class='muted'>{m['ticker']}</td>"
                     f"<td>{m['beta']:+.2f}</td><td>{m['expected']:+.1%}</td>"
                     f"<td>{m['actual']:+.1%}</td>"
                     f"<td class='{cls}'>{m['gap']:+.1%}</td>"
                     f"<td>{_diverging_bar(m['gap'], scale)}</td></tr>")
        blocks.append(
            f"<div class='card'><h3>{_esc(g)} "
            f"<span class='chip'>그룹 5일 {info['group_move']:+.1%}</span></h3>"
            f"<div class='scroll'><table>"
            f"<tr><th>종목</th><th>코드</th><th>β</th><th>기대</th><th>실제</th>"
            f"<th>갭</th><th>　</th></tr>"
            f"{rows}</table></div></div>")

    moves = (horizontal or {}).get("factor_moves") or {}
    fac = ""
    if moves:
        scale = max([abs(v.get("ret5") or 0) for v in moves.values()] + [0.01])
        rows = "".join(
            f"<tr><td>{_esc(k)}</td><td>{v['ret5']:+.1%}</td>"
            f"<td>{_diverging_bar(v['ret5'], scale)}</td>"
            f"<td class='muted'>20일 {v.get('ret20', 0):+.1%}</td></tr>"
            for k, v in moves.items() if v.get("ret5") is not None)
        fac = ("<div class='card'><h3>매크로 팩터 최근 5일</h3>"
               "<div class='scroll'><table>" + rows + "</table></div></div>")

    return ("<h2 id='horizontal'>수평 파급 — 같은 동인, 아직 안 움직인 종목</h2>"
            "<p class='muted'>갭 = 기대수익률(β × 그룹 수익률) − 실제수익률. "
            "양수가 클수록 함께 움직였어야 하는데 뒤처진 종목입니다.</p>"
            + fac + "<div class='grid2'>" + "".join(blocks) + "</div>")


def render_price_review(analysis: dict, limit: int = 12) -> str:
    """가격이 밸류체인을 뒷받침하는가 — 사이클 시차, 근거 약한 엣지, 발굴 후보.

    상관은 엣지를 **만들지 않는다**. 그 원칙이 화면에도 드러나야 한다 —
    발굴 후보는 '추가된 관계'가 아니라 '공시로 확인할 목록'으로 표시한다.
    """
    pr = (analysis or {}).get("price_review") or {}
    lags = pr.get("cycle_lags") or {}
    weak = pr.get("weak") or []
    cand = pr.get("candidates") or []
    if not (lags or weak or cand):
        return ""

    parts = ["<h2 id='price'>가격이 뒷받침하는가</h2>",
             "<p class='muted'>상관은 관계를 <b>만들지 않습니다</b>. 이미 공시로 확인된 "
             "관계가 실제로 가격에 나타나는지, 몇 달 시차로 전달되는지를 잽니다.</p>",
             "<div class='grid2'>"]

    if lags:
        top = sorted(lags.items(), key=lambda kv: -abs(kv[1]["corr"]))[:limit]
        rows = "".join(
            f"<tr><td class='wide'>{_esc(k.replace('→', ' → '))}</td>"
            f"<td>{(str(v['lag_months']) + '개월') if v['lag_months'] else '동행'}</td>"
            f"<td>{v['corr']:+.2f}</td>"
            f"<td>{_diverging_bar(v['corr'], 1.0, width=90)}</td></tr>" for k, v in top)
        parts.append("<div class='card'><h3>사이클 시차 (월 단위 실측)</h3>"
                     "<p class='muted'>+N개월 = 왼쪽이 오른쪽보다 N개월 먼저 움직였다</p>"
                     "<div class='scroll'><table><tr><th>관계</th><th>시차</th>"
                     f"<th>상관</th><th>　</th></tr>{rows}</table></div></div>")

    if weak:
        rows = "".join(
            f"<tr><td class='wide'>{_esc(w['src'][2:])} → {_esc(w['dst'][2:])}</td>"
            f"<td>{w['price_corr']:+.2f}</td>"
            f"<td class='muted'>{_esc(w.get('origin') or w.get('source'))}</td></tr>"
            for w in weak[:limit])
        parts.append("<div class='card'><h3>가격 근거가 약한 관계</h3>"
                     "<p class='muted'>삭제 목록이 아니라 검토 대기열입니다. 신생 관계는 "
                     "아직 가격에 안 실렸을 수 있습니다.</p>"
                     "<div class='scroll'><table><tr><th>관계</th><th>상관</th>"
                     f"<th>출처</th></tr>{rows}</table></div></div>")

    if cand:
        rows = "".join(
            f"<tr><td class='wide'>{_esc(c['a'])} ↔ {_esc(c['b'])}</td>"
            f"<td>{c['corr']:+.2f}</td><td>{c['lag']}</td></tr>" for c in cand[:limit])
        parts.append("<div class='card'><h3>발굴 후보 <span class='chip'>공시 확인 필요</span></h3>"
                     "<p class='muted'>엣지가 없는데 잔차 상관이 높은 쌍입니다. "
                     "<b>자동으로 관계를 만들지 않습니다</b> — 사업보고서에서 근거 문장을 "
                     "찾은 뒤에만 그래프에 들어갑니다.</p>"
                     "<div class='scroll'><table><tr><th>산업 쌍</th><th>상관</th>"
                     f"<th>시차(일)</th></tr>{rows}</table></div></div>")

    parts.append("</div>")
    return "".join(parts)


def render_coverage(analysis: dict) -> str:
    """그래프 커버리지를 리포트 하단에 드러낸다.

    맵에 적어 놓고 티커로 해석되지 않은 회사, LLM이 그래프 밖에서 꺼내 온 후보 비율은
    전부 '맵이 어디서 비어 있는가'를 가리킨다. 로그에만 남기면 아무도 안 본다.
    """
    vc = analysis.get("valuechain_coverage") or {}
    prov = analysis.get("provenance") or {}
    match = analysis.get("name_match") or {}
    bits = []

    if vc:
        bits.append(f"밸류체인 산업 {len(vc.get('industries', []))}개 · "
                    f"엣지 {vc.get('edges', 0)}개")
        unresolved = sorted({n for v in (vc.get("unresolved") or {}).values() for n in v})
        if unresolved:
            bits.append("<b>맵에 있으나 티커 미해석</b>: "
                        + _esc(", ".join(unresolved[:20]))
                        + (f" 외 {len(unresolved) - 20}건" if len(unresolved) > 20 else ""))
    if prov.get("total"):
        bits.append(f"종합 후보 {prov['total']}건 중 그래프 기반 {prov['graph_backed']}건")
    if match.get("unmatched"):
        bits.append("<b>LLM 종목명 미매칭</b>: " + _esc(", ".join(match["unmatched"][:15])))

    if not bits:
        return ""
    return ("<h2 id='coverage'>그래프 커버리지</h2><div class='card'><p class='muted'>"
            + "<br>".join(bits) + "</p></div>")


def render_html(base_date: str, candidates: list[dict], analysis: dict, site_title: str,
                horizontal: dict | None = None, names: dict[str, str] | None = None) -> str:
    date_fmt = f"{base_date[:4]}-{base_date[4:6]}-{base_date[6:]}"
    analysis = analysis or {}
    horizontal = horizontal or {}

    # 섹션 본문을 먼저 만든다. 내비게이션은 **실제로 렌더된 섹션만** 걸어야 한다 —
    # 없는 곳으로 가는 링크는 눌러도 아무 일이 없고, 그 자체가 '데이터가 있는데
    # 화면이 비었나?'라는 오해를 만든다.
    sec_horizontal = render_horizontal(horizontal, names or {})
    sec_price = render_price_review(analysis)
    sec_coverage = render_coverage(analysis)

    parts = ["<main>"]

    synthesis = analysis.get("synthesis")
    if synthesis:
        parts.append("<h2 id='synthesis'>오늘의 종합</h2><div class='card'>")
        parts.append(f"<p>{_esc(synthesis.get('market_summary'))}</p></div>")
        for idea in synthesis.get("ideas", []):
            bens = ""
            for b in idea.get("beneficiaries", []):
                gap = f' <span class="muted">갭 {b["gap"]:+.1%}</span>' if b.get("gap") is not None else ""
                bens += (
                    f'<li><b>{_esc(b["name"])}</b>{_impact_tag(b)} '
                    f'<span class="{"badge-mi" if b["priced_in"] == "미반영" else "badge-gi"}">'
                    f'[{_esc(b["priced_in"])}]</span>{gap}{_provenance_tag(b)} '
                    f'— {_esc(b["comment"])}</li>')
            axis = f"<span class='chip'>{_esc(idea['axis'])}축</span>" if idea.get("axis") else ""
            diagram = _ripple_svg(idea.get("driver") or idea.get("title", ""),
                                  [{"direction": idea.get("axis") or "파급",
                                    "target_industry": idea.get("path", "")[:30],
                                    "beneficiaries": idea.get("beneficiaries", [])}])
            parts.append(
                f"<div class='card'><h3>💡 {_esc(idea['title'])}{axis}</h3>"
                f"<p><b>동인:</b> {_esc(idea['driver'])}</p>"
                f"<p><b>경로:</b> {_esc(idea['path'])}</p>"
                f"<div class='scroll'>{diagram}</div>"
                f"<ul>{bens}</ul>"
                f"<p class='muted'>체크포인트: {_esc(idea['watch_points'])}</p></div>")

    analyses = analysis.get("analyses", [])
    if analyses:
        parts.append("<h2 id='stocks'>종목별 분석</h2>")
        for a in analyses:
            paths = ""
            for p in a.get("ripple_paths", []):
                bens = "".join(_beneficiary_row(b) for b in p.get("beneficiaries", []))
                paths += (f"<div class='path'><b>{_esc(p['direction'])} → {_esc(p['target_industry'])}</b>"
                          f"<br>{_esc(p['logic'])}<ul>{bens}</ul></div>")
            axis = f"<span class='chip'>{_esc(a['ripple_axis'])}축</span>" if a.get("ripple_axis") else ""
            grp = ""
            if a.get("groups"):
                grp = (f"<p class='muted'>소속 그룹: "
                       f"{_esc(' / '.join(a['groups'][:5]))}</p>")
            diagram = _ripple_svg(a["name"], a.get("ripple_paths", []))
            parts.append(
                f"<div class='card'><h3>{_esc(a['name'])} <span class='muted'>{a['ticker']}</span>"
                f"<span class='chip'>{_esc(a['trigger'])}</span>"
                f"<span class='chip'>{_esc(a['cause_type'])}</span>"
                f"<span class='chip'>{_esc(a['cause_scope'])}</span>{axis}</h3>"
                f"<p>{_esc(a['cause_summary'])}</p>"
                f"<p class='muted'>근거: {_esc(a['evidence'])} · 확신도 {_esc(a['confidence'])} · "
                f"20일 수익률 {a['ret20']:+.1%}</p>{grp}"
                f"<div class='scroll'>{diagram}</div>{paths}</div>")

    parts.append(sec_horizontal)
    parts.append(sec_price)

    if candidates:
        scale = max([abs(c.get("ret20") or 0) for c in candidates] + [0.05])
        rows = "".join(
            f"<tr><td class='wide'>{_esc(c['name'])}</td><td class='muted'>{c['ticker']}</td>"
            f"<td>{_esc(c['trigger'])}</td>"
            f"<td>{_pct(c['ret5'])}</td><td>{_pct(c['ret20'])}</td>"
            f"<td>{_diverging_bar(c['ret20'], scale)}</td>"
            f"<td>{c['vol_surge']}x</td><td>{c['high_proximity']:.0%}</td></tr>"
            for c in candidates)
        parts.append(
            "<h2 id='screen'>스크리닝 전체 후보</h2><div class='card scroll'><table>"
            "<tr><th>종목</th><th>코드</th><th>유형</th><th>5일</th><th>20일</th><th>　</th>"
            "<th>거래량</th><th>신고가대비</th></tr>" + rows + "</table></div>")

    parts.append(sec_coverage)
    parts.append("<p class='muted'>본 자료는 자동 생성된 참고 자료이며 투자 권유가 아닙니다.</p></main>")
    body = "".join(parts)

    jump = [("#synthesis", "종합", bool(analysis.get("synthesis"))),
            ("#stocks", "종목별", bool(analysis.get("analyses"))),
            ("#horizontal", "수평 파급", bool(sec_horizontal)),
            ("#price", "가격 검증", bool(sec_price)),
            ("#screen", "스크리닝", bool(candidates)),
            ("#coverage", "커버리지", bool(sec_coverage))]
    nav = "".join(f'<a href="{h}">{t}</a>' for h, t, on in jump if on)
    head = (f'<header><div class="inner"><h1>{_esc(site_title)}</h1>'
            f'<p class="sub">기준일 {date_fmt} · <a href="index.html">지난 리포트</a>'
            f' · <a href="valuechain.html">밸류체인</a></p>'
            f'{render_kpis(candidates, analysis, horizontal)}'
            f'<nav class="jump">{nav}</nav></div></header>')

    return (f"<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{_esc(site_title)} {date_fmt}</title><style>{CSS}</style></head>"
            f"<body>{head}{body}</body></html>")


def render_index(site_title: str) -> str:
    dates = sorted((p.stem for p in REPORTS_DIR.glob("2*.html")), reverse=True)
    items = "".join(f'<li><a href="{d}.html">{d[:4]}-{d[4:6]}-{d[6:]}</a></li>' for d in dates)
    latest = f'<meta http-equiv="refresh" content="0; url={dates[0]}.html">' if dates else ""
    return (f"<!doctype html><html lang='ko'><head><meta charset='utf-8'>{latest}"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{_esc(site_title)}</title><style>{CSS}</style></head><body><main>"
            f"<h1>{_esc(site_title)}</h1><p class='sub'>일자별 리포트 · "
            f"<a href='valuechain.html'>밸류체인</a></p>"
            f"<ul class='dates'>{items}</ul></main></body></html>")


def render_telegram(base_date: str, candidates: list[dict], analysis: dict,
                    pages_url: str | None) -> str:
    """텔레그램용 요약 (HTML parse mode). **선택 사항**이다.

    봇 토큰이 없으면 notify가 조용히 건너뛴다. 주 산출물은 웹 대시보드이고,
    이건 '오늘 뭔가 나왔다'는 알림 역할이다.
    """
    date_fmt = f"{base_date[:4]}-{base_date[4:6]}-{base_date[6:]}"
    lines = [f"📈 <b>상승 종목 원인·파급 분석</b> ({date_fmt})", ""]

    synthesis = (analysis or {}).get("synthesis")
    if synthesis and synthesis.get("ideas"):
        lines.append(f"<i>{_esc(synthesis['market_summary'])}</i>")
        lines.append("")
        for idea in synthesis["ideas"][:5]:
            axis = f" [{idea['axis']}축]" if idea.get("axis") else ""
            lines.append(f"💡 <b>{_esc(idea['title'])}</b>{axis}")
            lines.append(f"  경로: {_esc(idea['path'])}")
            for b in idea.get("beneficiaries", [])[:4]:
                mark = "🟢" if b["priced_in"] == "미반영" else ("🟡" if b["priced_in"] == "일부반영" else "⚪")
                if b.get("impact") == "피해":
                    mark = "🔻"
                gap = f" 갭{b['gap']:+.0%}" if b.get("gap") is not None else ""
                lines.append(f"  {mark} {_esc(b['name'])} [{_esc(b['priced_in'])}]{gap}")
            lines.append("")
    elif candidates:
        lines.append("원인 분석 없이 스크리닝만 수행됨. 상위 후보:")
        for c in candidates[:10]:
            lines.append(f"· {_esc(c['name'])} ({c['trigger']}, 20일 {c['ret20']:+.1%})")
        lines.append("")

    if pages_url:
        lines.append(f'👉 <a href="{pages_url}">상세 리포트 보기</a>')
    return "\n".join(lines)


def build(base_date: str, candidates: list[dict], analysis: dict, cfg: dict,
          horizontal: dict | None = None) -> Path:
    REPORTS_DIR.mkdir(exist_ok=True)

    names: dict[str, str] = {}
    returns_file = ROOT / "data" / f"returns_{base_date}.json"
    if returns_file.exists():
        raw = json.loads(returns_file.read_text(encoding="utf-8"))
        names = {t: v.get("name") for t, v in raw.items() if isinstance(v, dict) and v.get("name")}

    out = REPORTS_DIR / f"{base_date}.html"
    out.write_text(
        render_html(base_date, candidates, analysis, cfg["site_title"], horizontal, names),
        encoding="utf-8")
    (REPORTS_DIR / "index.html").write_text(render_index(cfg["site_title"]), encoding="utf-8")

    # 밸류체인 탭 — 날짜별이 아니라 항상 최신 그래프 한 장이다. 관계는 공시에서
    # 나오므로 일자별로 바뀌지 않고, 여러 장으로 나누면 오히려 찾기 어려워진다.
    from . import graph as G
    (REPORTS_DIR / "valuechain.html").write_text(
        render_valuechain(G.load(), names, cfg["site_title"]), encoding="utf-8")

    print(f"리포트 생성: {out}, valuechain.html")
    return out


# ---- 밸류체인 탭 ---------------------------------------------------------

def _member_chip(ticker: str, names: dict, sourced: bool) -> str:
    """소속 종목 칩. 공시 근거가 있는 것과 맵에만 있는 것을 시각적으로 가른다."""
    nm = names.get(ticker) or ticker
    cls = "m-dart" if sourced else "m-map"
    title = "사업보고서 인용 근거 있음" if sourced else "밸류체인 맵(사람이 작성)"
    return f'<span class="mchip {cls}" title="{title}">{_esc(nm)}</span>'


def render_valuechain(edges: list[dict], names: dict[str, str],
                      site_title: str = "밸류체인") -> str:
    """구축된 밸류체인 전체를 훑어보는 페이지.

    이 그래프의 값어치는 관계의 개수가 아니라 **근거**다. 그래서 화면의 중심에
    인용문을 둔다 — 관계를 클릭하면 그 관계를 주장한 회사와 사업보고서 원문
    문장이 그대로 나온다. 눈으로 검증할 수 없는 관계는 없는 것과 같다.
    """
    from . import graph as G

    members: dict[str, set] = {}
    sourced: set[tuple[str, str]] = set()      # (산업, 티커) 중 공시 근거가 있는 것
    rel_rows: dict[tuple[str, str, str], list[dict]] = {}

    for e in edges:
        sk, sn = G.split_node(e["src"])
        dk, dn = G.split_node(e["dst"])
        if e["rel"] == G.REL_MEMBER and sk == "T" and dk == "I":
            members.setdefault(dn, set()).add(sn)
            if e.get("source") == "dart":
                sourced.add((dn, sn))
        elif sk == "I" and dk == "I" and e["rel"] in (G.REL_UPSTREAM, G.REL_DOWNSTREAM):
            rel_rows.setdefault((sn, e["rel"], dn), []).append(e)

    industries = sorted(members, key=lambda i: (-len(members[i]), i))
    for (a, _r, b) in rel_rows:
        for n in (a, b):
            if n not in industries:
                industries.append(n)

    cross = sum(1 for v in rel_rows.values()
                if len({e.get("origin") for e in v if e.get("origin")}) >= 2)
    all_tickers = {t for v in members.values() for t in v}
    quoted = sum(1 for v in rel_rows.values() if any(e.get("source") == "dart" for e in v))

    kpis = "".join(
        f'<div class="kpi"><div class="v">{v}</div><div class="l">{l}</div></div>'
        for l, v in [("산업 노드", len(industries)), ("소속 종목", len(all_tickers)),
                     ("산업 간 관계", len(rel_rows)), ("공시 인용 관계", quoted),
                     ("교차 검증 관계", cross)])

    cards = []
    for ind in industries:
        mem = sorted(members.get(ind, ()), key=lambda t: (names.get(t) or t))
        chips = "".join(_member_chip(t, names, (ind, t) in sourced) for t in mem) or \
            '<span class="muted">소속 종목 없음 — 상장된 순수 사업자가 없거나 아직 매핑되지 않음</span>'

        def _rels(rel: str, label: str) -> str:
            items = []
            for (a, r, b), es in sorted(rel_rows.items()):
                if a != ind or r != rel:
                    continue
                origins = sorted({e["origin"] for e in es if e.get("origin")})
                badge = (f'<span class="xv">×{len(origins)}</span>' if len(origins) >= 2 else "")
                lag = next((e.get("cycle_lag_months") for e in es
                            if e.get("cycle_lag_months") is not None), None)
                lagtxt = f'<span class="chip">{lag:+d}개월</span>' if lag else ""
                quotes = "".join(
                    f'<li><b>{_esc(names.get(e.get("origin")) or e.get("origin") or e.get("source"))}</b>'
                    f' — <span class="q">{_esc(e.get("evidence"))}</span></li>'
                    for e in es[:6] if e.get("evidence"))
                body = (f'<details><summary>{_esc(b)} {badge}{lagtxt}</summary>'
                        f'<ul class="quotes">{quotes}</ul></details>') if quotes else \
                       f'<div class="norel">{_esc(b)} {badge}{lagtxt}</div>'
                items.append(body)
            if not items:
                return ""
            return f'<div class="rel"><span class="rl">{label}</span>{"".join(items)}</div>'

        up = _rels(G.REL_UPSTREAM, "후방(공급)")
        down = _rels(G.REL_DOWNSTREAM, "전방(수요)")
        cards.append(
            f'<div class="card vc" data-k="{_esc(ind)} {_esc(" ".join(names.get(t) or t for t in mem))}">'
            f'<h3>{_esc(ind)} <span class="chip">{len(mem)}종목</span></h3>'
            f'<div class="mchips">{chips}</div>{up}{down}</div>')

    css_extra = """
.mchips { margin:6px 0 10px; }
.mchip { display:inline-block; border-radius:6px; padding:1px 8px; margin:2px 4px 2px 0;
         font-size:.82rem; border:1px solid var(--line); }
.mchip.m-dart { background:var(--chip); color:var(--accent); border-color:var(--accent); }
.mchip.m-map { color:var(--muted); }
.rel { margin-top:8px; padding-left:10px; border-left:2px solid var(--line); }
.rl { display:block; font-size:.76rem; color:var(--muted); margin-bottom:3px; }
details { margin:2px 0; } summary { cursor:pointer; font-size:.9rem; }
.norel { font-size:.9rem; color:var(--muted); }
.quotes { list-style:none; margin:4px 0 8px 4px; font-size:.8rem; }
.quotes li { padding:3px 0; border-bottom:1px dashed var(--line); }
.q { color:var(--muted); }
.xv { background:var(--good); color:#fff; border-radius:99px; padding:0 6px;
      font-size:.7rem; margin-left:4px; }
#f { width:100%; padding:9px 12px; border-radius:9px; border:1px solid var(--line);
     background:var(--card); color:var(--fg); font-size:.95rem; margin:12px 0 4px; }
"""
    js = ("<script>const f=document.getElementById('f');"
          "f.addEventListener('input',()=>{const q=f.value.trim().toLowerCase();"
          "document.querySelectorAll('.vc').forEach(c=>{"
          "c.style.display=!q||c.dataset.k.toLowerCase().includes(q)?'':'none';});});</script>")

    return (f"<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{_esc(site_title)} — 밸류체인</title>"
            f"<style>{CSS}{css_extra}</style></head><body>"
            f'<header><div class="inner"><h1>밸류체인</h1>'
            f'<p class="sub">사업보고서에서 인용과 함께 추출한 산업 관계 · '
            f'<a href="index.html">리포트로</a></p>'
            f'<div class="kpis">{kpis}</div></div></header><main>'
            f'<input id="f" placeholder="산업명·종목명으로 거르기 (예: 조선, 시멘트, 포스코)">'
            f'<p class="muted">관계를 펼치면 <b>그 관계를 주장한 회사와 사업보고서 원문 '
            f'문장</b>이 나옵니다. <span class="xv">×2</span>는 서로 다른 회사가 독립적으로 '
            f'같은 관계를 말했다는 뜻입니다. 진한 칩은 공시 근거가 있는 소속, '
            f'흐린 칩은 사람이 작성한 맵입니다.</p>'
            f'{"".join(cards)}'
            f'<p class="muted">본 자료는 자동 생성된 참고 자료이며 투자 권유가 아닙니다.</p>'
            f"</main>{js}</body></html>")
