import json
import html as _h
from typing import List
from src.screener import StockResult

PAGES_URL = "https://jeannychiu.github.io/DayTrade_Radar/"


def build_report(results: dict, date: str, intraday: dict, sector_map: dict = None) -> str:
    p1: List[StockResult] = results.get("p1", [])
    p2: List[StockResult] = results.get("p2", [])
    if sector_map is None:
        sector_map = {}

    chart_data = {}
    for r in p1 + p2:
        if r.stock_id in intraday:
            denom = 1 + r.change_pct
            baseline = round(r.close / denom, 2) if abs(denom) > 0.0001 else r.close
            chart_data[r.stock_id] = {**intraday[r.stock_id], "baseline": baseline}

    p2_shown = p2[:15]
    p1_html = _grid(p1, intraday, sector_map) if p1 else '<p class="empty">今日無符合個股</p>'
    p2_html = _grid(p2_shown, intraday, sector_map) if p2_shown else '<p class="empty">今日無符合個股</p>'

    return _page(date, len(p1), len(p2), len(p2_shown), p1_html, p2_html,
                 json.dumps(chart_data, ensure_ascii=False))


def _grid(stocks: List[StockResult], intraday: dict, sector_map: dict) -> str:
    cards = "".join(_card(r, r.stock_id in intraday, sector_map.get(r.stock_id, "")) for r in stocks)
    return f'<div class="grid">{cards}</div>'


def _card(r: StockResult, has_chart: bool, sector: str = "") -> str:
    up   = r.change_pct >= 0
    cls  = "up" if up else "dn"
    sign = "+" if up else ""
    arr  = "▲" if up else "▼"
    tags = "".join(f'<span class="tag">{_h.escape(c)}</span>' for c in r.conditions)
    chart = (f'<div class="cw"><canvas data-sid="{r.stock_id}"></canvas></div>'
             if has_chart else "")
    sector_html = f'<div class="sec-lbl">{_h.escape(sector)}</div>' if sector else ""
    return (
        f'<div class="card">'
        f'<div class="rt">'
        f'<div class="info-l"><div class="sn">{_h.escape(r.name)}</div>'
        f'<div class="si">{r.stock_id}</div>'
        f'{sector_html}</div>'
        f'<div class="ir"><div class="px {cls}">{r.close:.2f}</div>'
        f'<div class="ch {cls}">{sign}{r.change_pct:.2%}{arr}</div></div>'
        f'</div>'
        f'{chart}'
        f'<div class="vl">量 {r.volume:,.0f}張</div>'
        f'<div class="tg">{tags}</div>'
        f'</div>'
    )


def _page(date, p1n, p2n, p2_shown, p1_html, p2_html, chart_json) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
  <title>台股當沖選股 {date}</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{background:#111318;color:#e8e8e8;font-family:-apple-system,'Segoe UI',PingFang TC,sans-serif;padding:12px;max-width:640px;margin:0 auto}}
    h1{{font-size:18px;font-weight:bold;padding:10px 0 4px}}
    .sub{{color:#888;font-size:12px;margin-bottom:16px}}
    .sec{{font-size:14px;font-weight:bold;margin:16px 0 8px;padding-left:8px;border-left:3px solid #f5a623}}
    .grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}}
    .card{{background:#1c1f2a;border-radius:10px;padding:10px;min-width:0;overflow:hidden}}
    .rt{{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:4px;gap:4px}}
    .info-l{{min-width:0;flex:1}}
    .sn{{font-size:14px;font-weight:bold;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
    .si{{font-size:11px;color:#888;margin-top:2px}}
    .sec-lbl{{font-size:10px;color:#666;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
    .ir{{text-align:right;flex-shrink:0}}
    .px{{font-size:17px;font-weight:bold}}
    .ch{{font-size:11px;margin-top:1px}}
    .up{{color:#ff4444}}.dn{{color:#00cc55}}
    .cw{{height:48px;margin:6px 0}}
    .vl{{font-size:11px;color:#aaa;margin-bottom:5px}}
    .tg{{display:flex;flex-wrap:wrap;gap:3px}}
    .tag{{background:#2a2d3a;color:#9ab;font-size:10px;padding:2px 5px;border-radius:3px}}
    .empty{{color:#666;font-size:13px;padding:8px 0}}
    .foot{{margin-top:24px;color:#555;font-size:11px;text-align:center;padding-bottom:16px}}
  </style>
</head>
<body>
  <h1>📊 台股當沖選股報告</h1>
  <p class="sub">{date}&nbsp;·&nbsp;第一優先 {p1n} 檔&nbsp;·&nbsp;第二優先 {p2_shown}/{p2n} 檔</p>

  <div class="sec">🥇 第一優先（一紅吃三黑 / 突破糾結均線）</div>
  {p1_html}

  <div class="sec">🥈 第二優先（量價＋型態）</div>
  {p2_html}

  <p class="foot">⚠️ 僅供參考，請自行評估風險</p>

  <script>
  const D={chart_json};
  document.querySelectorAll('canvas[data-sid]').forEach(c=>{{
    const d=D[c.dataset.sid];
    if(!d||!d.prices.length)return;
    const bl=d.baseline;
    new Chart(c,{{
      type:'line',
      data:{{
        labels:d.times,
        datasets:[
          {{
            data:d.prices,
            borderWidth:1.2,
            pointRadius:0,
            tension:0.1,
            fill:{{target:{{value:bl}},above:'rgba(255,68,68,0.3)',below:'rgba(0,204,85,0.3)'}},
            segment:{{
              borderColor:ctx=>{{
                const y0=ctx.p0.parsed.y,y1=ctx.p1.parsed.y;
                return(y0>=bl&&y1>=bl)?'#ff4444':(y0<bl&&y1<bl)?'#00cc55':y0>=bl?'#ff4444':'#00cc55';
              }}
            }}
          }},
          {{
            data:d.prices.map(()=>bl),
            borderColor:'rgba(180,180,180,0.5)',
            borderWidth:0.8,
            borderDash:[3,3],
            pointRadius:0,
            fill:false
          }}
        ]
      }},
      options:{{
        animation:false,responsive:true,maintainAspectRatio:false,
        plugins:{{legend:{{display:false}},tooltip:{{enabled:false}}}},
        scales:{{x:{{display:false}},y:{{display:false}}}}
      }}
    }});
  }});
  </script>
</body>
</html>"""
