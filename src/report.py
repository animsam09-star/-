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
          horizontal: dict | None = None, universe=None) -> Path:
    REPORTS_DIR.mkdir(exist_ok=True)

    # 종목명은 **전 종목 마스터**에서 가져온다. returns_*.json은 시총·거래대금
    # 필터를 통과한 종목만 담아서, 밸류체인의 소형 후방 소재주는 이름이 없다.
    # 이름이 없으면 상자에 티커가 그대로 찍히는데, '104700'을 보고 한국철강임을
    # 아는 사람은 없다. 실제로 20종목이 그렇게 나가고 있었다.
    names: dict[str, str] = {}
    if universe is not None:
        names = {t: n for t in universe.entries if (n := universe.name(t))}
    if not names:
        returns_file = ROOT / "data" / f"returns_{base_date}.json"
        if returns_file.exists():
            raw = json.loads(returns_file.read_text(encoding="utf-8"))
            names = {t: v.get("name") for t, v in raw.items()
                     if isinstance(v, dict) and v.get("name")}

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
#
# 그림 우선이다. 표와 인용문으로 된 이전 판은 정보는 다 있었지만 '무엇이 무엇으로
# 흐르는가'가 한눈에 안 들어왔다. 밸류체인은 본질적으로 방향이 있는 흐름이라
# 왼쪽(공급) → 가운데(산업) → 오른쪽(수요) 배치가 글보다 빠르게 읽힌다.
#
# 인용문은 화면에서 뺐다. 근거는 그래프 파일에 그대로 남아 있고 필요할 때
# 꺼내 볼 수 있다 — 매일 보는 화면에서는 그게 오히려 시야를 가린다.

_VC_BOX_W = 168
_VC_ROW_H = 62
_VC_GAP = 232

# 전체 지도용 치수. 산업 카드보다 작게 잡는다 — 62개 산업을 한 장에 올리려면
# 상자마다 종목명을 다 적을 수 없고, 종목은 아래 카드에서 보면 된다.
_MAP_BOX_W = 128
_MAP_BOX_H = 30
_MAP_ROW_H = 42
_MAP_COL_W = 196


def _vc_flow(edges: list[dict]) -> tuple[dict, dict]:
    """공급 방향 간선 {(공급, 수요): 교차검증 횟수} 와 산업별 소속 종목.

    그래프에는 같은 관계가 후방·전방 두 방향으로 들어 있다(A의 후방이 B면
    B의 전방이 A). 지도는 **흐름 방향 하나**로만 그려야 하므로 후방 엣지만
    읽어 '공급 → 수요'로 뒤집는다. 둘 다 읽으면 모든 선이 두 번 그려진다.
    """
    from . import graph as G

    flow: dict[tuple[str, str], set] = {}
    members: dict[str, set] = {}
    for e in edges:
        sk, sn = G.split_node(e["src"])
        dk, dn = G.split_node(e["dst"])
        if e["rel"] == G.REL_MEMBER and sk == "T" and dk == "I":
            members.setdefault(dn, set()).add(sn)
        elif sk == "I" and dk == "I" and e["rel"] == G.REL_UPSTREAM:
            flow.setdefault((dn, sn), set()).add(
                e.get("origin") or e.get("source") or "?")
    return flow, members


def _vc_sequence(nodes: list[str], flow) -> dict[str, int]:
    """사이클을 무시할 순서를 정한다 (Eades–Lin–Smyth 그리디).

    산업 연관은 실제로 순환한다 — 철강이 건설기계를 먹이고 건설기계가 광산을,
    광산이 다시 철강을 먹인다. 위상정렬을 그냥 돌리면 62개 중 41개가 사이클에
    걸려 층이 안 나온다. 그래서 '되돌아가는 간선'을 최소로 만드는 순서를 먼저
    잡고, 그 순서를 거스르는 간선만 층 계산에서 뺀다. 실제 데이터에서 142개 중
    7개만 빠진다 — 나머지 135개는 그대로 흐름을 이룬다.
    """
    succ: dict[str, set] = {n: set() for n in nodes}
    pred: dict[str, set] = {n: set() for n in nodes}
    for u, d in flow:
        succ[u].add(d)
        pred[d].add(u)

    left, right, rest = [], [], set(nodes)
    while rest:
        moved = True
        while moved:
            moved = False
            for n in sorted(rest):
                if n in rest and not (succ[n] & rest):     # 더 팔 곳이 없다 = 끝단
                    right.append(n); rest.discard(n); moved = True
            for n in sorted(rest):
                if n in rest and not (pred[n] & rest):     # 받을 곳이 없다 = 원류
                    left.append(n); rest.discard(n); moved = True
        if rest:
            # 남은 건 전부 사이클이다. 나가는 쪽이 가장 많은 노드를 앞으로 빼면
            # 끊어야 하는 간선이 가장 적어진다.
            n = max(sorted(rest), key=lambda x: len(succ[x] & rest) - len(pred[x] & rest))
            left.append(n); rest.discard(n)
    return {n: i for i, n in enumerate(left + right[::-1])}


def _vc_layout(flow) -> tuple[dict[str, tuple[int, int]], int, int, set]:
    """산업 → (열, 행). 열은 공급 깊이, 행은 선이 덜 꼬이는 자리.

    열은 원류로부터의 **최장 경로**다. 최단으로 잡으면 소재가 완성품 옆에 붙어
    중간 단계가 사라진다.

    전력처럼 거의 모든 산업이 받아 쓰는 투입은 층을 길게 늘인다(유틸리티가
    오른쪽으로 밀리면 그걸 받는 시멘트가 더 오른쪽으로 간다). 절대 위치보다
    **선을 따라가는 것**이 이 그림의 용도라 그대로 둔다.
    """
    nodes = sorted({x for pair in flow for x in pair})
    if not nodes:
        return {}, 0, 0, set()
    order = _vc_sequence(nodes, flow)
    fwd = [(u, d) for u, d in flow if order[u] < order[d]]
    dropped = {(u, d) for u, d in flow if order[u] >= order[d]}

    incoming: dict[str, list] = {n: [] for n in nodes}
    for u, d in fwd:
        incoming[d].append(u)
    col: dict[str, int] = {}
    for n in sorted(nodes, key=lambda x: order[x]):
        col[n] = max([col[u] + 1 for u in incoming[n] if u in col], default=0)

    cols: dict[int, list] = {}
    for n in nodes:
        cols.setdefault(col[n], []).append(n)
    for c in cols:
        cols[c].sort()

    # 무게중심 정렬 — 이웃의 평균 높이로 자리를 옮긴다. 안 하면 선이 통째로
    # 교차해서, 연결은 돼 있는데 눈으로는 못 따라간다.
    nbr_in: dict[str, list] = {n: [] for n in nodes}
    nbr_out: dict[str, list] = {n: [] for n in nodes}
    for u, d in fwd:
        nbr_in[d].append(u)
        nbr_out[u].append(d)
    row = {n: i for c in cols for i, n in enumerate(cols[c])}
    for sweep in range(4):
        keys = sorted(cols) if sweep % 2 == 0 else sorted(cols, reverse=True)
        for c in keys:
            side = nbr_in if sweep % 2 == 0 else nbr_out
            cols[c].sort(key=lambda n: (
                sum(row[x] for x in side[n]) / len(side[n]) if side[n] else row[n], n))
            for i, n in enumerate(cols[c]):
                row[n] = i

    pos = {n: (col[n], row[n]) for n in nodes}
    return pos, max(col.values()) + 1, max(len(v) for v in cols.values()), dropped


def _vc_reach(flow) -> tuple[dict[str, set], dict[str, set]]:
    """각 산업의 전체 후방(조상)·전방(자손). 지도에서 사슬을 따라가는 데 쓴다.

    **반드시 사이클을 걷어낸 간선만 넣어야 한다.** 원본 간선으로 돌리면 62개 중
    41개가 한 덩어리로 순환하고 있어서 모든 산업이 모든 산업에 닿는다 — 철강도
    시멘트도 조선도 '후방 38 / 전방 41'로 똑같이 나와 아무것도 구분되지 않는다.
    """
    succ: dict[str, set] = {}
    pred: dict[str, set] = {}
    for u, d in flow:
        succ.setdefault(u, set()).add(d)
        pred.setdefault(d, set()).add(u)

    def close(adj):
        out: dict[str, set] = {}
        for start in set(adj) | {x for v in adj.values() for x in v}:
            seen, stack = set(), [start]
            while stack:
                x = stack.pop()
                for y in adj.get(x, ()):
                    if y not in seen:
                        seen.add(y); stack.append(y)
            out[start] = seen
        return out

    return close(pred), close(succ)


def _vc_box(x: float, y: float, name: str, members: list[str], *,
            accent: bool = False) -> str:
    """산업 상자 하나. 이름 아래에 소속 종목을 적는다.

    종목명이 없으면 산업만 보이는데, 그러면 '그래서 뭘 사야 하나'에 답이 안 된다.
    이 그래프의 쓸모는 산업이 아니라 종목까지 내려가는 데 있다.
    """
    shown = members[:3]
    more = len(members) - len(shown)
    label = ", ".join(shown) + (f" 외 {more}" if more > 0 else "")
    fill = "var(--chip)" if accent else "var(--card)"
    stroke = "var(--accent)" if accent else "var(--line)"
    weight = "700" if accent else "500"
    out = [f'<rect x="{x}" y="{y}" width="{_VC_BOX_W}" height="46" rx="8" '
           f'fill="{fill}" stroke="{stroke}" stroke-width="{2 if accent else 1}"/>',
           f'<text x="{x + _VC_BOX_W / 2}" y="{y + 19}" text-anchor="middle" '
           f'font-size="12.5" font-weight="{weight}" fill="var(--fg)">'
           f'{_esc(name[:13])}</text>']
    if label:
        out.append(f'<text x="{x + _VC_BOX_W / 2}" y="{y + 35}" text-anchor="middle" '
                   f'font-size="9.5" fill="var(--muted)">{_esc(label[:24])}</text>')
    elif not accent:
        out.append(f'<text x="{x + _VC_BOX_W / 2}" y="{y + 35}" text-anchor="middle" '
                   f'font-size="9.5" fill="var(--muted)">상장 종목 없음</text>')
    return "".join(out)


def _vc_link(x1: float, y1: float, x2: float, y2: float, strength: int) -> str:
    """관계 선. 굵기가 교차 검증 횟수다.

    몇 개 회사가 독립적으로 같은 말을 했는지가 이 그래프에서 가장 믿을 만한
    품질 신호라, 그걸 굵기로 드러낸다. 숫자를 읽지 않아도 눈에 들어온다.
    """
    w = min(4.0, 1.0 + strength * 0.7)
    op = min(0.85, 0.35 + strength * 0.15)
    mx = (x1 + x2) / 2
    return (f'<path d="M{x1} {y1} C {mx} {y1}, {mx} {y2}, {x2} {y2}" fill="none" '
            f'stroke="var(--accent)" stroke-width="{w:.1f}" opacity="{op:.2f}"/>')


def _vc_map(flow, members: dict) -> tuple[str, int]:
    """전 산업을 한 장에 이은 흐름 지도. (SVG, 그려진 산업 수).

    산업별 카드만 있던 이전 판은 각 산업의 **이웃 한 칸**까지만 보여 줬다.
    그래서 철광석 → 철강 → 후판 → 조선 → 해운처럼 사슬을 따라가는, 이 도구의
    본래 용도가 화면에서 불가능했다. 카드 61장이 서로 이어지지 않은 채 흩어져
    있었던 셈이다.

    왼쪽이 원류, 오른쪽이 최종 수요다. 산업을 누르면 그 산업의 사슬 전체가
    남고 나머지는 흐려진다 — 62개 노드를 한꺼번에 눈으로 좇을 수는 없다.
    """
    pos, ncols, nrows, dropped = _vc_layout(flow)
    if not pos:
        return "", 0

    idx = {n: i for i, n in enumerate(sorted(pos))}
    fwd = {k: v for k, v in flow.items() if k not in dropped}
    up_all, down_all = _vc_reach(fwd)
    near: dict[str, set] = {}
    for u, d in fwd:
        near.setdefault(u, set()).add(d)
        near.setdefault(d, set()).add(u)

    w = (ncols - 1) * _MAP_COL_W + _MAP_BOX_W + 8
    h = nrows * _MAP_ROW_H + 16

    def xy(n):
        c, r = pos[n]
        return 4 + c * _MAP_COL_W, 8 + r * _MAP_ROW_H

    out = [f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" '
           f'aria-label="산업 밸류체인 전체 흐름도" class="map">']

    # 선을 먼저 깔아야 상자가 그 위에 온다.
    for (u, d), who in sorted(flow.items()):
        if (u, d) in dropped:
            continue          # 사슬을 거스르는 간선. 그리면 흐름이 뒤엉킨다.
        x1, y1 = xy(u); x2, y2 = xy(d)
        x1 += _MAP_BOX_W
        y1 += _MAP_BOX_H / 2; y2 += _MAP_BOX_H / 2
        mx = (x1 + x2) / 2
        sw = min(3.2, 0.9 + len(who) * 0.6)
        out.append(f'<path class="lk" data-a="{idx[u]}" data-b="{idx[d]}" '
                   f'd="M{x1} {y1} C {mx} {y1}, {mx} {y2}, {x2} {y2}" fill="none" '
                   f'stroke="var(--accent)" stroke-width="{sw:.1f}" opacity=".28"/>')

    for n in sorted(pos):
        x, y = xy(n)
        chain = sorted({idx[m] for m in (up_all.get(n, set()) | down_all.get(n, set())
                                         | {n}) if m in idx})
        adj = sorted({idx[m] for m in near.get(n, ()) if m in idx})
        cnt = len(members.get(n, ()))
        out.append(
            f'<g class="nd" data-i="{idx[n]}" data-chain="{",".join(map(str, chain))}" '
            f'data-near="{",".join(map(str, adj))}" '
            f'tabindex="0" role="button" aria-label="{_esc(n)} 사슬 보기">'
            f'<title>{_esc(n)} · 소속 {cnt}종목 · '
            f'후방 {len(up_all.get(n, ()))} / 전방 {len(down_all.get(n, ()))}</title>'
            f'<rect x="{x}" y="{y}" width="{_MAP_BOX_W}" height="{_MAP_BOX_H}" rx="7" '
            f'fill="var(--card)" stroke="var(--line)"/>'
            f'<text x="{x + _MAP_BOX_W / 2}" y="{y + 19}" text-anchor="middle" '
            f'font-size="11" fill="var(--fg)">{_esc(n[:11])}</text></g>')

    out.append("</svg>")
    return "".join(out), len(pos)


def _vc_diagram(industry: str, members: dict, ups: list, downs: list) -> str:
    """한 산업의 후방 → 산업 → 전방 흐름 그림."""
    rows = max(len(ups), len(downs), 1)
    h = rows * _VC_ROW_H + 24
    w = _VC_GAP * 2 + _VC_BOX_W
    mid = h / 2 - 23

    out = [f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" '
           f'aria-label="{_esc(industry)} 밸류체인">']

    def col(items, x, anchor_x):
        for i, (name, strength) in enumerate(items):
            y = 12 + i * _VC_ROW_H + (rows - len(items)) * _VC_ROW_H / 2
            out.append(_vc_link(anchor_x, mid + 23,
                                x + (_VC_BOX_W if x < _VC_GAP else 0), y + 23, strength))
            out.append(_vc_box(x, y, name, sorted(members.get(name, ()))))

    col(ups, 0, _VC_GAP)                       # 왼쪽: 공급(후방)
    col(downs, _VC_GAP * 2, _VC_GAP + _VC_BOX_W)   # 오른쪽: 수요(전방)
    out.append(_vc_box(_VC_GAP, mid, industry, sorted(members.get(industry, ())),
                       accent=True))
    out.append("</svg>")
    return "".join(out)


def _vc_makes(industry: str, members: dict, makes: dict) -> str:
    """이 산업의 종목이 각각 **무엇을 만드는지**.

    상자 안에는 이름밖에 못 넣는다. 그런데 같은 산업 안에서도 만드는 물건은
    제각각이라, 이름만 보면 '자동차 부품·모듈' 12종목이 서로 대체재처럼 보인다.
    실제로는 제동장치(HL만도)·변속기(SNT다이내믹스)·자동차 전선(가온전선)이고
    수요가 움직이는 이유가 다르다.

    제품이 안 적힌 종목은 **적힌 종목 뒤로 보낸다.** 근거가 있는 쪽이 먼저
    보여야 하고, 빈 자리는 공시에서 아직 못 뽑았다는 뜻이라 숨기지 않는다.
    """
    who = sorted(members.get(industry, ()))
    if not who:
        return ""
    m = makes.get(industry, {})
    rows = []
    for name in sorted(who, key=lambda n: (not m.get(n), n)):
        prods = " · ".join(sorted(m.get(name, ())))
        rows.append(
            f'<li><b>{_esc(name)}</b>'
            + (f'<span>{_esc(prods[:70])}</span>' if prods
               else '<span class="none">공시에서 품목 미추출</span>')
            + '</li>')
    return f'<ul class="mk">{"".join(rows)}</ul>'


def render_valuechain(edges: list[dict], names: dict[str, str],
                      site_title: str = "밸류체인") -> str:
    """구축된 밸류체인을 그림으로 훑어보는 페이지."""
    from . import graph as G

    members: dict[str, set] = {}
    # 산업 → 종목 → 그 종목이 이 산업에서 만드는 것. 산업명만 남기면 HL만도와
    # SNT다이내믹스와 가온전선이 전부 '자동차 부품·모듈'로 같아 보인다.
    # 제동장치·변속기·자동차 전선은 수요 동인이 다르므로 파급도 다르게 간다.
    makes: dict[str, dict[str, set]] = {}
    ups: dict[str, dict[str, set]] = {}
    downs: dict[str, dict[str, set]] = {}

    for e in edges:
        sk, sn = G.split_node(e["src"])
        dk, dn = G.split_node(e["dst"])
        if e["rel"] == G.REL_MEMBER and sk == "T" and dk == "I":
            members.setdefault(dn, set()).add(names.get(sn) or sn)
            if e.get("product"):
                makes.setdefault(dn, {}).setdefault(
                    names.get(sn) or sn, set()).add(e["product"])
        elif sk == "I" and dk == "I":
            bucket = ups if e["rel"] == G.REL_UPSTREAM else (
                downs if e["rel"] == G.REL_DOWNSTREAM else None)
            if bucket is None:
                continue
            who = e.get("origin") or e.get("source") or "?"
            bucket.setdefault(sn, {}).setdefault(dn, set()).add(who)

    industries = sorted(set(members) | set(ups) | set(downs),
                        key=lambda i: (-(len(members.get(i, ())) * 2
                                         + len(ups.get(i, {})) + len(downs.get(i, {}))), i))

    all_stocks = {s for v in members.values() for s in v}
    rel_count = sum(len(v) for v in ups.values()) + sum(len(v) for v in downs.values())
    cross = sum(1 for v in ups.values() for w in v.values() if len(w) >= 2)

    flow, _ = _vc_flow(edges)
    chain_map, mapped = _vc_map(flow, members)
    # 흐름에 아직 못 붙은 산업은 지도에서 그냥 사라진다. 조용히 빠지면 '없는'
    # 건지 '안 이어진' 건지 알 수 없으므로 이름을 적어 드러낸다.
    orphans = sorted(i for i in industries
                     if not any(i in pair for pair in flow) and members.get(i))

    kpis = "".join(
        f'<div class="kpi"><div class="v">{v}</div><div class="l">{l}</div></div>'
        for l, v in [("산업", len(industries)), ("소속 종목", len(all_stocks)),
                     ("산업 간 관계", rel_count), ("교차 검증", cross)])

    cards = []
    for ind in industries:
        u = sorted(((k, len(w)) for k, w in ups.get(ind, {}).items()),
                   key=lambda kv: -kv[1])[:4]
        d = sorted(((k, len(w)) for k, w in downs.get(ind, {}).items()),
                   key=lambda kv: -kv[1])[:4]
        if not u and not d and len(members.get(ind, ())) < 2:
            continue          # 연결도 소속도 없는 노드는 그림이 될 게 없다
        key = " ".join([ind] + [x for x, _ in u + d] + sorted(members.get(ind, ())))
        cards.append(
            f'<div class="card vc" data-k="{_esc(key)}">'
            f'<h3>{_esc(ind)} <span class="chip">{len(members.get(ind, ()))}종목</span></h3>'
            f'<div class="scroll">{_vc_diagram(ind, members, u, d)}</div>'
            f'{_vc_makes(ind, members, makes)}</div>')

    css_extra = """
.vc h3 { margin-bottom:2px; }
.legend2 { display:flex; gap:18px; flex-wrap:wrap; align-items:center;
           font-size:.78rem; color:var(--muted); margin:10px 0 4px; }
#f { width:100%; padding:10px 13px; border-radius:9px; border:1px solid var(--line);
     background:var(--card); color:var(--fg); font-size:.95rem; margin:14px 0 2px; }
.map .nd { cursor:pointer; }
.map .nd:focus { outline:none; }
.map .nd rect { transition:opacity .12s, stroke .12s; }
/* 고른 산업 > 바로 붙은 산업 > 사슬의 나머지 > 사슬 밖. 사슬을 통째로 같은
   밝기로 켜면 62개 중 28개가 켜져 여전히 못 읽는다. 단계를 줘야 눈이 따라간다. */
.map.sel .nd { opacity:.12; }
.map.sel .nd.ch { opacity:.5; }
.map.sel .nd.on { opacity:1; }
.map.sel .lk { opacity:.04 !important; }
.map.sel .lk.ch { opacity:.3 !important; }
.map.sel .lk.on { opacity:.85 !important; }
.map .nd.pick { opacity:1 !important; }
.map .nd.pick rect { stroke:var(--accent); stroke-width:2.4; fill:var(--chip); }
.axis { display:flex; justify-content:space-between; font-size:.74rem;
        color:var(--muted); margin:2px 0 6px; }
ul.mk { list-style:none; margin:10px 0 0; border-top:1px solid var(--line);
        padding-top:8px; }
ul.mk li { display:flex; gap:10px; padding:3px 0; font-size:.82rem;
           border-bottom:1px solid var(--line); }
ul.mk li b { flex:0 0 8.5em; font-weight:600; }
ul.mk li span { color:var(--muted); }
ul.mk .none { opacity:.55; font-style:italic; }
@media (max-width:560px){ ul.mk li { flex-direction:column; gap:1px; }
                          ul.mk li b { flex:none; } }
"""
    # 사슬 추적. 62개 노드를 한꺼번에 눈으로 좇을 수 없으니, 하나를 누르면
    # 그 산업이 닿는 후방·전방만 남기고 나머지를 흐린다. 다시 누르면 원래대로.
    js = ("<script>"
          "const f=document.getElementById('f');"
          "f.addEventListener('input',()=>{const q=f.value.trim().toLowerCase();"
          "document.querySelectorAll('.vc').forEach(c=>{"
          "c.style.display=!q||c.dataset.k.toLowerCase().includes(q)?'':'none';});});"
          "const m=document.querySelector('.map');"
          "if(m){let cur=null;const clear=()=>{m.classList.remove('sel');"
          "m.querySelectorAll('.on,.ch,.pick').forEach(e=>"
          "e.classList.remove('on','ch','pick'));};"
          "const pick=g=>{const i=g.dataset.i;"
          "if(cur===i){cur=null;clear();return;}"
          "cur=i;clear();m.classList.add('sel');"
          "const s=new Set(g.dataset.chain.split(','));"
          "const nr=new Set(g.dataset.near?g.dataset.near.split(','):[]);"
          "m.querySelectorAll('.nd').forEach(n=>{const j=n.dataset.i;"
          "if(nr.has(j))n.classList.add('on');else if(s.has(j))n.classList.add('ch');});"
          "g.classList.add('pick');"
          "m.querySelectorAll('.lk').forEach(l=>{const a=l.dataset.a,b=l.dataset.b;"
          "if(a===i||b===i)l.classList.add('on');"
          "else if(s.has(a)&&s.has(b))l.classList.add('ch');});};"
          "m.querySelectorAll('.nd').forEach(g=>{"
          "g.addEventListener('click',()=>pick(g));"
          "g.addEventListener('keydown',e=>{"
          "if(e.key==='Enter'||e.key===' '){e.preventDefault();pick(g);}});});}"
          "</script>")

    legend = (
        '<div class="legend2">'
        '<span>← 왼쪽 = 공급(후방)</span>'
        '<span>가운데 = 해당 산업</span>'
        '<span>오른쪽 = 수요(전방) →</span>'
        '<span>선이 굵을수록 여러 회사가 같은 관계를 말함</span></div>')

    map_block = ""
    if chain_map:
        note = ""
        if orphans:
            note = ('<p class="muted">아직 어느 사슬에도 안 붙은 산업: '
                    + _esc(", ".join(orphans))
                    + ' — 공시에서 전·후방이 아직 안 잡힌 곳입니다.</p>')
        map_block = (
            '<h2>전체 흐름도</h2>'
            f'<div class="card"><p class="muted">산업 {mapped}개가 한 사슬로 이어져 '
            '있습니다. 산업을 누르면 그 산업이 닿는 후방·전방 전체만 남습니다.</p>'
            '<div class="axis"><span>← 원류(원료·부품)</span>'
            '<span>최종 수요 →</span></div>'
            f'<div class="scroll">{chain_map}</div>{note}</div>')

    return (f"<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{_esc(site_title)} — 밸류체인</title>"
            f"<style>{CSS}{css_extra}</style></head><body>"
            f'<header><div class="inner"><h1>밸류체인</h1>'
            f'<p class="sub">사업보고서에서 추출한 산업 간 흐름 · '
            f'<a href="index.html">리포트로</a></p>'
            f'<div class="kpis">{kpis}</div></div></header><main>'
            f'{map_block}'
            f'<h2>산업별 상세</h2>'
            f'<input id="f" placeholder="산업명·종목명으로 거르기 (예: 조선, 시멘트, 포스코)">'
            f'{legend}{"".join(cards)}'
            f'<p class="muted">본 자료는 자동 생성된 참고 자료이며 투자 권유가 아닙니다.</p>'
            f"</main>{js}</body></html>")
