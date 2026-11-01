"""Observed Binance Futures UI contract; no account or trading controls.

Selectors below were observed on the public Binance Original chart on 2026-11-01.
Only public page navigation, timeframe selection, chart hover/zoom and screenshots
are permitted. Private chart objects, React state and application endpoints are
not used. A changed UI is an explicit technical failure, never a market HOLD.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
import re
from urllib.parse import urlsplit

SOURCE = 'BINANCE_USDM_FUTURES_WEBSITE_SCREENSHOT'
VIEWPORT = {'width': 1920, 'height': 1080}
CONTRACT = 'BINANCE_ORIGINAL_VISIBLE_UI_20261101_V1'
ROOT = '.chart-widget-root'
TIMEFRAMES = {'15m': '15m', '1h': '1H'}
PERIODS = {'15m': 900000, '1h': 3600000}

# This reads only rendered DOM/canvas and the browser's public URL/title. Canvas
# pixels establish that a real chart was drawn; prices are read from visible UI.
OBSERVE = r"""roots => {
 const show=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return e.isConnected&&s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;};
 const charts=Array.from(document.querySelectorAll('.chart-widget-root')).filter(show);
 const result={url:location.href,title:document.title,timezone:Intl.DateTimeFormat().resolvedOptions().timeZone,chart_count:charts.length};
 if(charts.length!==1)return result;
 const chart=charts[0],canvases=Array.from(chart.querySelectorAll('canvas.scene')).filter(show);
 const chartRect=chart.getBoundingClientRect();
 result.capture_clip={x:0,y:0,width:Math.ceil(chartRect.right),height:Math.ceil(chartRect.bottom)};
 result.canvas_count=canvases.length;
 result.timeframes={};
 for(const [tf,label]of [['15m','15m'],['1h','1H']]){
  const nodes=Array.from(chart.querySelectorAll('div[id]')).filter(e=>e.id===tf&&show(e)&&e.textContent.trim()===label);
  result.timeframes[tf]={count:nodes.length,active:nodes.length===1&&nodes[0].classList.contains('text-PrimaryText')};
 }
 result.header_rows=Array.from(chart.querySelectorAll('.chart-title-row')).filter(show).map(e=>e.innerText);
 const original=Array.from(chart.querySelectorAll('*')).filter(e=>e.children.length===0&&show(e)&&e.textContent.trim()==='Original');
 result.original_chart_visible=original.length===1;
 if(canvases.length===1){
  const e=canvases[0],r=e.getBoundingClientRect();result.canvas={x:r.x,y:r.y,width:r.width,height:r.height,pixel_width:e.width,pixel_height:e.height};
  try{const d=e.getContext('2d').getImageData(0,0,e.width,e.height).data,colors=new Set();
   for(let y=0;y<e.height;y+=Math.max(1,Math.floor(e.height/35)))for(let x=0;x<e.width;x+=Math.max(1,Math.floor(e.width/70))){let i=(y*e.width+x)*4;colors.add([d[i],d[i+1],d[i+2],d[i+3]].join(','));}
   result.canvas.sampled_color_count=colors.size;
  }catch(_){result.canvas.pixel_read_failed=true;}
 }
 const price=document.title.split('|')[0].trim();result.visible_price=price;
 result.price_visible=document.body.innerText.includes(price);
 return result;
}"""
SELECT = r"""(roots,tf) => {
 const label=tf==='15m'?'15m':tf==='1h'?'1H':null;
 if(label===null)return false;
 const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.visibility==='visible';};
 const charts=Array.from(document.querySelectorAll('.chart-widget-root')).filter(visible);
 if(charts.length!==1)return false;
 const buttons=Array.from(charts[0].querySelectorAll('div[id]')).filter(e=>e.id===tf&&visible(e)&&e.textContent.trim()===label);
 if(buttons.length!==1)return false;if(buttons[0].classList.contains('text-PrimaryText'))return true;buttons[0].click();return true;
}"""
DISMISS_TOUR = r"""roots => {
 const nodes=Array.from(document.body.querySelectorAll('*')).filter(e=>{const r=e.getBoundingClientRect();return !e.children.length&&e.textContent.trim()==='Skip'&&r.width>0&&r.height>0;});
 if(nodes.length===1){nodes[0].click();return true;}return false;
}"""

class WebsiteChartUnavailable(ValueError):
    pass


def need(value, reason='BINANCE_WEBSITE_CHART_UNVERIFIED'):
    if not value:
        raise WebsiteChartUnavailable(reason)


def url_for(symbol):
    need(type(symbol) is str and re.fullmatch(r'[A-Z0-9]{2,24}USDT', symbol) is not None)
    return 'https://www.binance.com/en/futures/' + symbol


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('ascii')


def observation(value, symbol, timeframe):
    need(type(value) is dict and value.get('url') == url_for(symbol)
         and timeframe in TIMEFRAMES and value.get('chart_count') == 1
         and value.get('canvas_count') == 1 and value.get('original_chart_visible') is True)
    title = value.get('title')
    need(type(title) is str and re.fullmatch(
        r'[0-9,.]+ \| ' + re.escape(symbol) + r' USD[ⓈS]-Margined Perpetual Chart \| Binance Futures', title) is not None)
    quote = value.get('visible_price')
    need(type(quote) is str and value.get('price_visible') is True)
    parsed = Decimal(quote.replace(',', ''))
    need(parsed.is_finite() and parsed > 0)
    frames = value.get('timeframes')
    need(type(frames) is dict and set(frames) == set(TIMEFRAMES)
         and frames[timeframe] == {'count': 1, 'active': True}
         and frames['1h' if timeframe == '15m' else '15m'] == {'count': 1, 'active': False})
    need(value.get('timezone') in ('UTC', 'Etc/UTC'))
    box = value.get('canvas')
    need(type(box) is dict and all(type(box.get(k)) in (int, float)
        and math.isfinite(box[k]) for k in ('x', 'y', 'width', 'height'))
         and box['width'] >= 700 and box['height'] >= 250
         and 0 <= box['x'] < VIEWPORT['width'] and 0 <= box['y'] < VIEWPORT['height']
         and box['x']+box['width'] <= VIEWPORT['width']+1
         and box['y']+box['height'] <= VIEWPORT['height']+1
         and box.get('sampled_color_count', 0) >= 8
         and box.get('pixel_read_failed') is not True)
    clip = value.get('capture_clip')
    need(type(clip) is dict and set(clip) == {'x','y','width','height'}
         and clip['x'] == clip['y'] == 0
         and all(type(clip[k]) is int for k in clip)
         and 700 <= clip['width'] <= VIEWPORT['width']
         and 400 <= clip['height'] <= VIEWPORT['height']
         and clip['width'] >= box['x']+box['width']
         and clip['height'] >= box['y']+box['height'])
    return value


def hover_open_time(value):
    rows = value.get('header_rows')
    need(type(rows) is list and rows and type(rows[0]) is str)
    label = rows[0].splitlines()[0].strip()
    need(re.fullmatch(r'\d{4}/\d{2}/\d{2} \d{2}:\d{2}', label) is not None,
         'BINANCE_WEBSITE_VISIBLE_TIME_UNVERIFIED')
    stamp = datetime.strptime(label, '%Y/%m/%d %H:%M').replace(tzinfo=timezone.utc)
    return int(stamp.timestamp()*1000)


def coverage(snapshot, timeframe, left, right):
    """Verify visible chart endpoints cover every immutable CLOSED CSV row.

    Endpoint timestamps come from the ordinary candle-hover header, not private
    chart state. A two-bar left margin avoids claiming a clipped edge candle.
    The running candle is reported separately and never adds to closed_count.
    """
    symbol = snapshot['symbol'];observation(left, symbol, timeframe);observation(right, symbol, timeframe)
    first, last = hover_open_time(left), hover_open_time(right)
    period = PERIODS[timeframe];rows = snapshot['candles'][timeframe]
    need(first % period == last % period == 0 and first < last
         and first <= rows[0]['open_time']-2*period
         and last >= rows[-1]['open_time']
         and last <= snapshot.get('collection_finished_server_time_ms', snapshot['server_time_ms'])//period*period,
         'BINANCE_WEBSITE_200_CLOSED_RANGE_NOT_VISIBLE')
    closed_last = min(last, snapshot['server_time_ms']//period*period-period)
    return {'verification': 'VISIBLE_CANDLE_HOVER_ENDPOINTS_UTC_V1',
        'first_visible_open_time_ms': first,'last_visible_open_time_ms': last,
        'covered_closed_count': (closed_last-first)//period+1,
        'required_closed_count': 200, 'required_first_open_time_ms': rows[0]['open_time'],
        'required_last_close_time_ms': rows[-1]['close_time'],
        'running_candle_visible': last >= snapshot['server_time_ms']//period*period,
        'running_candle_in_closed_count': False}
