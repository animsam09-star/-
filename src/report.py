"""리포트 생성: 일자별 HTML 리포트(GitHub Pages용)와 텔레그램 요약 텍스트를 만든다."""

from __future__ import annotations

import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = ROOT / "reports"

CSS = """
:root { --bg:#fafafa; --card:#fff; --fg:#1a1a2e; --muted:#667; --line:#e2e2ea;
        --up:#c0392b; --down:#2471a3; --accent:#5b4b8a; --chip:#eee9f7; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#14141c; --card:#1d1d28; --fg:#e8e8f0; --muted:#99a; --line:#33334a;
          --up:#ff7b6b; --down:#6bb2ff; --accent:#a794e0; --chip:#2b2440; } }
* { box-sizing:border-box; margin:0; }
body { background:var(--bg); color:var(--fg); font-family:'Apple SD Gothic Neo','Malgun Gothic',
       -apple-system,sans-serif; line-height:1.6; padding:24px 16px; }
main { max-width:880px; margin:0 auto; }
h1 { font-size:1.5rem; margin-bottom:4px; }
h2 { font-size:1.15rem; margin:32px 0 12px; color:var(--accent); }
.sub { color:var(--muted); font-size:.9rem; margin-bottom:24px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px;
        padding:16px 18px; margin-bottom:14px; }
.card h3 { font-size:1.05rem; margin-bottom:6px; }
.chip { display:inline-block; background:var(--chip); color:var(--accent); border-radius:99px;
        padding:1px 10px; font-size:.78rem; margin-left:6px; vertical-align:middle; }
.up { color:var(--up); font-weight:600; } .down { color:var(--down); font-weight:600; }
.muted { color:var(--muted); font-size:.86rem; }
table { width:100%; border-collapse:collapse; font-size:.88rem; }
th,td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); }
th { color:var(--muted); font-weight:600; }
.scroll { overflow-x:auto; }
.path { border-left:3px solid var(--accent); padding:6px 12px; margin:8px 0; }
ul.dates { list-style:none; } ul.dates li { padding:6px 0; border-bottom:1px solid var(--line); }
a { color:var(--accent); }
.badge-mi { color:#1e8449; font-weight:600; } .badge-gi { color:var(--muted); }
"""


def _pct(v) -> str:
    if v is None:
        return "-"
    cls = "up" if v > 0 else "down"
    return f'<span class="{cls}">{v:+.1%}</span>'


def _esc(s) -> str:
    return html.escape(str(s or ""))


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


def render_horizontal(horizontal: dict, names: dict[str, str], top_groups: int = 8,
                      top_members: int = 5) -> str:
    """가장 크게 움직인 그룹과 그 안에서 아직 안 따라온 종목을 표로 보여준다."""
    gaps = (horizontal or {}).get("group_gaps") or {}
    if not gaps:
        return ""

    ranked = sorted(gaps.items(), key=lambda kv: abs(kv[1]["group_move"]), reverse=True)[:top_groups]
    blocks = []
    for g, info in ranked:
        rows = ""
        for m in info["members"][:top_members]:
            nm = names.get(m["ticker"], m["ticker"])
            cls = "badge-mi" if m["gap"] > 0.03 else "badge-gi"
            rows += (f"<tr><td>{_esc(nm)}</td><td class='muted'>{m['ticker']}</td>"
                     f"<td>{m['beta']:+.2f}</td><td>{m['expected']:+.1%}</td>"
                     f"<td>{m['actual']:+.1%}</td>"
                     f"<td class='{cls}'>{m['gap']:+.1%}</td></tr>")
        blocks.append(
            f"<div class='card'><h3>{_esc(g)} "
            f"<span class='chip'>그룹 5일 {info['group_move']:+.1%}</span></h3>"
            f"<div class='scroll'><table>"
            f"<tr><th>종목</th><th>코드</th><th>β</th><th>기대</th><th>실제</th><th>갭</th></tr>"
            f"{rows}</table></div></div>")

    moves = (horizontal or {}).get("factor_moves") or {}
    fac = ""
    if moves:
        cells = " · ".join(
            f"{_esc(k)} {v['ret5']:+.1%}" for k, v in moves.items() if v.get("ret5") is not None)
        fac = f"<div class='card'><b>매크로 팩터 최근 5일</b><br><span class='muted'>{cells}</span></div>"

    return ("<h2>수평 파급 — 같은 동인, 아직 안 움직인 종목</h2>"
            "<p class='muted'>갭 = 기대수익률(β × 그룹 수익률) − 실제수익률. "
            "양수가 클수록 함께 움직였어야 하는데 뒤처진 종목입니다.</p>"
            + fac + "".join(blocks))


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
    return ("<h2>그래프 커버리지</h2><div class='card'><p class='muted'>"
            + "<br>".join(bits) + "</p></div>")


def render_html(base_date: str, candidates: list[dict], analysis: dict, site_title: str,
                horizontal: dict | None = None, names: dict[str, str] | None = None) -> str:
    date_fmt = f"{base_date[:4]}-{base_date[4:6]}-{base_date[6:]}"
    parts = [f"<main><h1>{_esc(site_title)}</h1>"
             f'<p class="sub">기준일 {date_fmt} · 후보 {len(candidates)}종목 · '
             f'<a href="index.html">지난 리포트</a></p>']

    synthesis = (analysis or {}).get("synthesis")
    if synthesis:
        parts.append("<h2>오늘의 종합</h2><div class='card'>")
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
            parts.append(
                f"<div class='card'><h3>💡 {_esc(idea['title'])}{axis}</h3>"
                f"<p><b>동인:</b> {_esc(idea['driver'])}</p>"
                f"<p><b>경로:</b> {_esc(idea['path'])}</p>"
                f"<ul>{bens}</ul>"
                f"<p class='muted'>체크포인트: {_esc(idea['watch_points'])}</p></div>")

    analyses = (analysis or {}).get("analyses", [])
    if analyses:
        parts.append("<h2>종목별 분석</h2>")
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
            parts.append(
                f"<div class='card'><h3>{_esc(a['name'])} <span class='muted'>{a['ticker']}</span>"
                f"<span class='chip'>{_esc(a['trigger'])}</span>"
                f"<span class='chip'>{_esc(a['cause_type'])}</span>"
                f"<span class='chip'>{_esc(a['cause_scope'])}</span>{axis}</h3>"
                f"<p>{_esc(a['cause_summary'])}</p>"
                f"<p class='muted'>근거: {_esc(a['evidence'])} · 확신도 {_esc(a['confidence'])} · "
                f"20일 수익률 {a['ret20']:+.1%}</p>{grp}{paths}</div>")

    parts.append(render_horizontal(horizontal or {}, names or {}))

    if candidates:
        rows = "".join(
            f"<tr><td>{_esc(c['name'])}</td><td>{c['ticker']}</td><td>{_esc(c['trigger'])}</td>"
            f"<td>{_pct(c['ret5'])}</td><td>{_pct(c['ret20'])}</td>"
            f"<td>{c['vol_surge']}x</td><td>{c['high_proximity']:.0%}</td></tr>"
            for c in candidates)
        parts.append(
            "<h2>스크리닝 전체 후보</h2><div class='card scroll'><table>"
            "<tr><th>종목</th><th>코드</th><th>유형</th><th>5일</th><th>20일</th>"
            "<th>거래량</th><th>신고가대비</th></tr>" + rows + "</table></div>")

    parts.append(render_coverage(analysis or {}))
    parts.append("<p class='muted'>본 자료는 자동 생성된 참고 자료이며 투자 권유가 아닙니다.</p></main>")
    body = "".join(parts)
    return (f"<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{_esc(site_title)} {date_fmt}</title><style>{CSS}</style></head>"
            f"<body>{body}</body></html>")


def render_index(site_title: str) -> str:
    dates = sorted((p.stem for p in REPORTS_DIR.glob("2*.html")), reverse=True)
    items = "".join(f'<li><a href="{d}.html">{d[:4]}-{d[4:6]}-{d[6:]}</a></li>' for d in dates)
    latest = f'<meta http-equiv="refresh" content="0; url={dates[0]}.html">' if dates else ""
    return (f"<!doctype html><html lang='ko'><head><meta charset='utf-8'>{latest}"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{_esc(site_title)}</title><style>{CSS}</style></head><body><main>"
            f"<h1>{_esc(site_title)}</h1><p class='sub'>일자별 리포트</p>"
            f"<ul class='dates'>{items}</ul></main></body></html>")


def render_telegram(base_date: str, candidates: list[dict], analysis: dict, pages_url: str | None) -> str:
    """텔레그램용 요약 (HTML parse mode)."""
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
    print(f"리포트 생성: {out}")
    return out
