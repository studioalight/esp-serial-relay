#!/usr/bin/env python3
"""
serial-relay — gateway-side daemon replacing the bridge-Mac rfc2217_server.

Fredrik's design (2026-10-15 16:14), after the bridge-Mac pyserial server
wedged on a dead client one too many times:

  [bridge Mac browser]  <--HTTPS (tailscale serve :8444)-->  [this daemon]
       |  Web Serial: local USB serial                          |  WS :4002 (page + link)
       |    open/setSignals/readable/writable                   |  RFC2217 :4001 (loopback)
       v                                                        v
  ESP32 on the Mac's USB            esptool rfc2217://127.0.0.1:4001

Nothing runs on the bridge Mac except a browser tab. The gateway owns BOTH
endpoints: RFC2217 (loopback — the WAN SET_CONTROL latency wall from the
remote-flash-rfc2217 skill collapses; one WS hop carries latency instead)
and the WebSocket the browser pipes its local serial through.

Wedge-class killed: when the WS link drops the RFC2217 listener just waits
for the next browser reconnect — the daemon never holds a half-open USB
lease on a remote box. (The browser may briefly hold the USB port in the
wedge window, but Fredrik can fix that by reloading one tab, not SSHing
into a server.)

Machinery: pyserial's serial.rfc2217.PortManager verbatim, driven by a
virtual serial object routed over the WS. Data paths:
  esptool -> socket -> PortManager.filter() -> WS -> browser -> USB
  USB -> browser -> WS -> PortManager.escape() -> socket -> esptool
Control: PortManager's dtr/rts/break setters -> {"t":"sig"} -> port.setSignals
"""

import asyncio
import json
import logging
import os
import queue
import threading
import time
from collections import deque

import websockets
from websockets.http11 import Response, Headers

from serial.rfc2217 import PortManager

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("serial-relay")
START_TS = time.time()
RELAY_VERSION = "v11"


class _RingLog(logging.Handler):
    """Bounded in-memory tail of the daemon log for the status dashboard."""

    def __init__(self, n=200):
        super().__init__()
        self.deque = deque(maxlen=n)

    def emit(self, record):
        try:
            self.deque.append(self.format(record))
        except Exception:
            pass


LOG_RING = _RingLog()
LOG_RING.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
log.addHandler(LOG_RING)

def _env(name, default):
    """ESP_RELAY_* preferred; legacy CLAWKIE_RELAY_* still honoured."""
    return os.environ.get(name, os.environ.get("CLAWKIE_" + name[4:], default))

RFC_PORT = int(_env("ESP_RELAY_RFC_PORT", "4001"))
WS_PORT = int(_env("ESP_RELAY_WS_PORT", "4002"))
RFC_BIND = _env("ESP_RELAY_RFC_BIND", "127.0.0.1")
BROWSER_WAIT_S = float(_env("ESP_RELAY_BROWSER_WAIT", "30"))

LOOP = None  # main asyncio loop, captured at startup (thread-safe handoffs)


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="color-scheme" content="dark light">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Studio Alight — Serial Relay</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;700&family=Space+Mono:ital,wght@0,400;0,700;1,400&display=swap" rel="stylesheet">
<style>
 :root{ --color-bg:#0a0a0a; --color-text:#f5f5f5; --color-accent:#ff0000;
          --font-display:'Space Grotesk',sans-serif; --font-mono:'Space Mono',monospace;
          --color-mute:#999; --color-line:#2a2a2a; --color-ok:#58c158; --color-bad:#e05a5a;
          --color-panel:#161616; --color-inset:#050505; --color-inset-text:#b9c2b9;
          --color-faint:#666; --color-line-soft:#3a3a3a }
 /* FIELD 2026-10-28 rung 20: system-follow + manual light/dark toggle. Dark is
  * the authored default; the light palette activates on [data-theme="light"] —
  * set manually by the toggle or by a system-follow bootstrap (no stored pref). */
 :root[data-theme="light"]{ --color-bg:#f7f4ee; --color-text:#17181a; --color-accent:#c1121f;
          --color-mute:#5c5f63; --color-line:#c9c9c9; --color-ok:#20713f; --color-bad:#b03232;
          --color-panel:#ffffff; --color-inset:#fbfaf7; --color-inset-text:#2c2e30;
          --color-faint:#8a8d90; --color-line-soft:#c9c9c9 }
 *{box-sizing:border-box}
 body{font-family:var(--font-display);background:var(--color-bg);color:var(--color-text);
        line-height:1.6;max-width:760px;margin:0 auto;padding:3rem 1rem 4rem;min-height:100vh}
 .brand{display:flex;align-items:center;gap:.7rem;display:flex;align-items:baseline;gap:.8rem;flex-wrap:wrap}
 .mark{width:.62rem;height:.62rem;border-radius:50%;background:var(--color-accent);
        box-shadow:0 0 10px 2px rgba(255,0,0,.45);align-self:center}
 h1{font-size:1.35rem;font-weight:500;letter-spacing:.02em;margin:0}
 .tagline{color:var(--color-mute);font-weight:300;letter-spacing:.08em;font-size:.85rem}
 /* animated wordmark (staging's logo, verbatim motion: flip A at 1.5s,
    reveal the space at 3s) */
 .wordmark{font-size:1rem;font-weight:400;letter-spacing:.15em;text-transform:uppercase;
        font-family:var(--font-display);display:inline-flex;white-space:nowrap;align-self:center}
 .studio-part,.alight-part{display:inline-flex}
 .wordmark .dim{color:rgba(128,128,128,0.6)}
 .wordmark .hot{color:var(--color-accent)}
 #reveal-space{width:0;overflow:hidden;display:inline-flex;justify-content:flex-start;
        transition:width .8s cubic-bezier(.4,0,.2,1)}
 #alight-part{display:inline-flex;transform:translateX(0);
        transition:transform .8s cubic-bezier(.4,0,.2,1)}
 #flipping-a{display:inline-block;transform-origin:center}
 .rule{border:0;border-top:1px solid var(--color-line);margin:1.4rem 0}
 .box{border:1px solid var(--color-line);border-radius:10px;padding:1.2rem;margin:1.2rem 0;
        background:linear-gradient(180deg,rgba(255,255,255,.02),transparent 60%)}
 /* phase strip: esptool function indicators */
 .phases{display:flex;gap:.5rem;flex-wrap:wrap;margin:.2rem 0 .6rem}
 .phase{font-family:var(--font-mono);font-size:.66rem;letter-spacing:.1em;text-transform:uppercase;
        padding:.34rem .6rem;border:1px solid var(--color-line);border-radius:4px;color:var(--color-mute);
        display:flex;align-items:center;gap:.4rem;transition:all .25s}
 .phase .dot{width:.4rem;height:.4rem;border-radius:50%;background:#3a3a3a;transition:all .25s}
 .phase.active{border-color:var(--color-accent);color:var(--color-text);box-shadow:0 0 10px rgba(255,0,0,.2)}
 .phase.active .dot{background:var(--color-accent);box-shadow:0 0 8px rgba(255,0,0,.6)}
 .phase.done{color:var(--color-text)} .phase.done .dot{background:var(--color-ok)}
 /* serial monitor */
 #mon{font-family:var(--font-mono);font-size:.72rem;line-height:1.55;background:var(--color-inset);
        border:1px solid var(--color-line);border-radius:8px;padding:.8rem;height:15rem;min-height:5rem;
        max-height:70vh;overflow-y:auto;resize:vertical;
        white-space:pre-wrap;word-break:break-all;color:var(--color-inset-text)}
 #mon .tx{color:#c8896a} #mon .rx{color:#7ea87e} #mon .bin{color:#717171;font-style:italic}
 #mon .ann{color:#e8d9a0;font-style:italic}
 .ann-input{font-family:var(--font-mono);font-size:.62rem;background:var(--color-panel);color:var(--color-text);
        border:1px solid var(--color-line);border-radius:4px;padding:.22rem .45rem;width:14rem;flex:1 1 14rem}
 .ann-input:focus{outline:none;border-color:var(--color-accent)}
 .ann-input::placeholder{color:var(--color-faint)}
 #mon .err{color:var(--color-bad)}
 .mon-ctl{display:flex;flex-wrap:wrap;gap:.6rem;align-items:center;margin:.5rem 0 .3rem;row-gap:.35rem}
 .mon-ctl label,.mon-foot label{font-family:var(--font-mono);font-size:.68rem;color:var(--color-mute);letter-spacing:.08em;
        display:flex;align-items:center;gap:.3rem;cursor:pointer;flex-shrink:0;white-space:nowrap}
 .mon-foot{display:flex;gap:.7rem;align-items:center;margin:.25rem 0 0;
        font-family:var(--font-mono);font-size:.62rem;color:var(--color-mute)}
 .mon-foot.ann-row{margin-top:.4rem}
 .mon-foot-legend{color:var(--color-faint);letter-spacing:.03em}
 #mon-clear,#mon-save,.mini{font-family:var(--font-mono);font-size:.62rem;padding:.2rem .5rem;border-radius:4px;
        border:1px solid var(--color-line);background:transparent;color:var(--color-mute);cursor:pointer}
 #mon-clear:hover,#mon-save:hover,.mini:hover{color:var(--color-text)}
 #bytes{font-family:var(--font-mono);font-size:.68rem;color:var(--color-mute);margin-left:auto}
 .mode-row{display:flex;align-items:center;gap:.8rem;margin:.2rem 0 1rem}
 .mode-chip{font-family:var(--font-mono);font-size:.68rem;letter-spacing:.12em;text-transform:uppercase;
        padding:.3rem .7rem;border:1px solid var(--color-line);border-radius:999px;color:var(--color-mute);
        cursor:pointer;transition:all .2s;background:transparent}
 .mode-chip:hover{color:var(--color-text)}
 .mode-chip.on{border-color:var(--color-accent);color:var(--color-text);box-shadow:0 0 8px rgba(255,0,0,.18)}
 body.easy .advanced{display:none}
 body:not(.easy) .easy-only{display:none}
 .box-label{font-family:var(--font-mono);font-size:.68rem;letter-spacing:.14em;
        text-transform:uppercase;color:var(--color-mute);margin-bottom:.7rem}
 button{font-family:var(--font-mono);font-size:.9rem;padding:.65rem 1.4rem;border-radius:6px;
        border:1px solid var(--color-line-soft);background:var(--color-panel);color:var(--color-text);cursor:pointer;
        transition:border-color .2s, box-shadow .2s}
 button:hover{border-color:var(--color-accent);box-shadow:0 0 12px rgba(255,0,0,.25)}
 button:disabled{opacity:.4;cursor:default;box-shadow:none}
 #state{font-family:var(--font-mono);font-size:.82rem;margin-left:.8rem;color:var(--color-mute)}
 #state.up{color:var(--color-ok)} #state.down{color:var(--color-bad)}
 .hint{color:var(--color-mute);font-size:.85rem}
 .hint em{color:var(--color-text);font-style:normal}
 #log{font-family:var(--font-mono);font-size:.75rem;white-space:pre-wrap;color:var(--color-ok);
        max-height:14rem;overflow-y:auto}
 #log .bad{color:var(--color-bad)} #log .ok{color:var(--color-ok)}
 footer{margin-top:2rem;color:var(--color-mute);font-size:.8rem;letter-spacing:.05em}
 footer b{color:var(--color-text);font-weight:500}
 .theme-toggle{margin-left:auto;font-family:var(--font-mono);font-size:.62rem;
        padding:.25rem .6rem;border-radius:4px;border:1px solid var(--color-line);
        background:transparent;color:var(--color-mute);cursor:pointer;flex-shrink:0}
 .theme-toggle:hover{color:var(--color-text);border-color:var(--color-line-soft)}
</style></head><body>
<div class="brand">
 <span class="mark"></span>
 <span class="wordmark" id="header-wordmark">
    <span class="studio-part"><span class="hot">S</span><span class="dim">TUDIO</span></span>
    <span id="reveal-space"><span style="width:0.4em;display:inline-block"></span></span>
    <span class="alight-part" id="alight-part">
      <span id="flipping-a" class="hot">A</span><span class="dim">LIGHT</span>
    </span>
 </span>
 <span class="tagline">We Alight the Invisible</span>
 <button class="theme-toggle" id="theme-toggle" title="toggle light/dark (follows system by default)">◐ system</button>
</div>
<hr class="rule">
<div class="mode-row">
 <span class="box-label" style="margin:0">Mode</span>
 <button class="mode-chip on" id="mode-easy">Easy</button>
 <button class="mode-chip" id="mode-adv">Advanced</button>
</div>
<div class="box" id="compat" style="display:none;border-color:var(--color-bad)"></div>
<div class="box easy-only" id="easy-box">
 <div class="box-label">Step 1 — connect the device</div>
 <button id="btn" style="font-size:1rem;padding:.8rem 1.6rem">Connect USB serial</button>
 <div id="easy-state" style="margin-top:.6rem;font-family:var(--font-mono);font-size:.85rem;color:var(--color-mute)">not connected — click Connect, keep the tab open</div>
 <p class="hint" style="margin-top:.6rem">Pick your <em>serial port</em> when the browser asks. Keep this tab open — that's it. The gateway handles the rest and shows progress in Advanced mode.</p>
</div>
<div class="box advanced">
 <div class="box-label">Serial bridge — bridge-Mac side</div>
 <div class="mon-ctl">
  <button id="btn-adv">Connect USB serial (advanced)</button>
  <label style="margin-left:.4rem">baud
   <select id="baud" style="font-family:var(--font-mono);background:var(--color-panel);color:var(--color-text);border:1px solid var(--color-line-soft);border-radius:4px;padding:.2rem .4rem">
    <option>115200</option><option>230400</option><option>460800</option>
    <option>921600</option><option>1500000</option><option>2000000</option>
   </select>
  </label>
  <span id="state">not connected</span>
 </div>
</div>
<div class="box">
 <div class="box-label">Procedure</div>
 <p class="hint">Grant the browser access to your <em>USB serial port</em>, then keep
 this tab open while a flash runs. Bytes flow <em>browser ⇄ gateway</em>; the gateway
 exposes the line to <em>esptool</em> as rfc2217://127.0.0.1:4001. If a flash wedges,
 reloading this tab is the whole reset procedure.</p>
</div>
<div class="box advanced">
 <div class="box-label">Esptool phases</div>
 <div class="phases">
  <span class="phase" id="ph-connect"><span class="dot"></span>connect</span>
  <span class="phase" id="ph-sync"><span class="dot"></span>sync</span>
  <span class="phase" id="ph-erase"><span class="dot"></span>write</span>
  <span class="phase" id="ph-verify"><span class="dot"></span>verify</span>
  <span class="phase" id="ph-reset"><span class="dot"></span>reset</span>
 </div>
 <p class="hint" id="phase-note">Phases light up from the gateway's esptool console feed during a flash.</p>
</div>
<div class="box advanced">
 <div class="box-label">Serial monitor</div>
 <div class="mon-ctl">
  <label><input type="checkbox" id="show-text" checked> text</label>
  <label><input type="checkbox" id="show-hex"> hex</label>
  <label><input type="checkbox" id="auto-scroll" checked> auto-scroll</label>
  <button class="mini" id="font-minus">A−</button>
  <button class="mini" id="font-plus">A+</button>
  <button class="mini" id="mon-clear">clear</button>
  <button class="mini" id="mon-save">save log</button>
  <label><input type="checkbox" id="show-stamps"> timestamps</label>
 </div>
 <div id="mon"></div>
 <div class="mon-foot ann-row">
  <input class="ann-input" id="ann-text" type="text" placeholder="mark an event — e.g. 'start test' (lands in relay log with timestamp)" maxlength="120">
  <button class="mini" id="mon-annotate">mark</button>
 </div>
 <div class="mon-foot">
  <label><input type="checkbox" id="flow-dl" checked> device ⇄ gateway flow arrows</label>
  <span class="mon-foot-legend">↑ device → gateway · ↓ gateway → device</span>
  <span id="bytes" style="margin-left:auto">0 B</span>
 </div>
 <p class="hint" style="margin-top:.4rem">Drag the bottom-right corner to resize. Text mode assembles clean lines — collapses binary bursts; hex shows the raw per-chunk bus.</p>
</div>
<div class="box advanced">
 <div class="box-label">Link log</div>
 <div id="log"></div>
</div>
<footer>serial-relay v11 · gateway daemon · <b>Studio Alight</b> 2026 · <a href="/dashboard" style="color:var(--color-mute)">status dashboard</a></footer>
<script>
const $=id=>document.getElementById(id);
const log=(m,cls)=>{const d=document.createElement('div');if(cls)d.className=cls;d.textContent=m;$('log').append(d);$('log').scrollTop=$('log').scrollHeight;};
function setStateEx(t,cls){
  const s=$('state'); if(s){s.textContent=t;s.className=cls||'';}
  const es=$('easy-state');
  if(es){
    es.textContent = t==='link up' ? 'connected \u2014 ready' : t;
    es.style.color = (cls==='up') ? 'var(--color-ok)' : 'var(--color-mute)';
  }
}

/* animated wordmark (staging verbatim) */
setTimeout(()=>{ const a=$('flipping-a'), p=$('alight-part');
  if(a) a.style.transform='scaleY(-1) translateY(1px)';
  if(p) p.style.transform='translateX(0.4em)'; },1500);
setTimeout(()=>{ const s=$('reveal-space'); if(s) s.style.width='0.4em'; },3000);

/* serial monitor v2 — line-assembled terminal view, resizable */
let rxCount=0, dlCount=0;
const hx=n=>n.toString(16).padStart(2,'0').toUpperCase();
const M={pend:{rx:'',tx:''},lastDir:null,font:11};
function setBytes(){$('bytes').textContent=`${rxCount} B \u2191 \u00b7 ${dlCount} B \u2193`;}
function monScroll(mon){ if($('auto-scroll').checked) mon.scrollTop=mon.scrollHeight; }
function trimMon(mon){ while(mon.children.length>1000) mon.firstChild.remove(); }
function dec(bytes){
  let s='';
  for(const b of bytes){
    if(b===13||b===10) s+='\\n';
    else if(b===9) s+='\\t';
    else if(b>=32&&b<127) s+=String.fromCharCode(b);
    else s+='\u00b7';
  }
  return s;
}
function stampPrefix(){
  const d=new Date();
  return $('show-stamps').checked ? d.toTimeString().slice(0,8)+'.'+String(d.getMilliseconds()).padStart(3,'0')+' ' : '';
}
function renderLine(el){
  const dir=el.dataset.dir;
  el.textContent=stampPrefix()
    +(dir==='ann'?'':(dir==='rx'?'\u2191 ':'\u2193 '))
    +el.dataset.text;
}
function emit(dir,seg){
  if(!seg) return;
  const mon=$('mon');
  const dots=(seg.match(/\u00b7/g)||[]).length;
  const binary=seg.length>60 && dots/seg.length>0.5;
  const line=document.createElement('div');
  line.className=dir+(binary?' bin':'');
  M.lastDir=dir;
  line.dataset.dir=dir;
  line.dataset.text=binary?`\u27e8binary ${seg.length} B\u27e9`:seg;
  renderLine(line);
  mon.append(line); trimMon(mon);
}
function flushPend(dir,force){
  const p=M.pend[dir]; if(!p) return;
  const parts=p.split('\\n');
  M.pend[dir]=force?'':(parts.pop()||'');
  for(const seg of parts) emit(dir,seg);
}
function monAdd(bytes, dir){
  if(!$('flow-dl').checked) return;
  if(dir==='rx') rxCount+=bytes.length; else dlCount+=bytes.length;
  setBytes();
  if($('show-hex').checked){
    const mon=$('mon');
    const line=document.createElement('div'); line.className=dir;
    line.dataset.dir=dir;
    line.dataset.text=`(${bytes.length} B) `
      +[...bytes].slice(0,48).map(hx).join(' ')
      +(bytes.length>48?` \u2026+${bytes.length-48}`:'');
    renderLine(line);
    mon.append(line); trimMon(mon); monScroll(mon);
    return;
  }
  if(!$('show-text').checked) return;
  if(M.lastDir!==dir){
    for(const d of ['rx','tx']) if(d!==dir && M.pend[d]) flushPend(d,true);
  }
  M.pend[dir]+=dec(bytes);
  if(M.pend[dir].length>16384) M.pend[dir]=M.pend[dir].slice(-16384);
  flushPend(dir,false);
  monScroll($('mon'));
}
$('mon-clear').onclick=()=>{$('mon').innerHTML='';rxCount=dlCount=0;
  M.pend={rx:'',tx:''};M.lastDir=null;$('bytes').textContent='0 B';};
$('mon-save').onclick=()=>{
  const txt=Array.from($('mon').children).map(el=>el.textContent).join('\\n');
  const a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([txt],{type:'text/plain'}));
  a.download='serial-monitor-'+new Date().toISOString().replace(/[:.]/g,'-')+'.txt';
  document.body.append(a); a.click(); a.remove();
  setTimeout(()=>URL.revokeObjectURL(a.href),1000);
};
function setFont(d){
  M.font=Math.min(18,Math.max(8,M.font+d));
  $('mon').style.fontSize=M.font+'px';
}
$('font-minus').onclick=()=>setFont(-1);
$('font-plus').onclick=()=>setFont(1);

/* annotations + timestamps toggle */
function addAnn(text){
  const el=document.createElement('div'); el.className='ann';
  el.dataset.dir='ann';
  el.dataset.text='\u25c6 '+(text.trim()||'(mark)');
  renderLine(el);
  const mon=$('mon'); mon.append(el); trimMon(mon); monScroll(mon);
}
function submitAnn(){
  const t=$('ann-text').value;
  addAnn(t);
  // FIELD 2026-10-26: also send to the relay so the marker survives tab
  // reloads and lands in the relay log with a server-side timestamp.
  if(ws && ws.readyState===1){ try{ ws.send(JSON.stringify({t:'ann',text:t})); }catch(e){} }
  $('ann-text').value='';
}
$('mon-annotate').onclick=submitAnn;
$('ann-text').addEventListener('keydown',e=>{ if(e.key==='Enter') submitAnn(); });
$('show-stamps').onchange=()=>{ for(const el of $('mon').children) renderLine(el); };

/* phase strip */
const phaseOrder=['connect','sync','write','verify','reset'];
const phaseEls={}; phaseOrder.forEach(p=>phaseEls[p]=$('ph-'+p));
function phaseHit(name){
  const el=phaseEls[name]; if(!el) return;
  if(name!=='reset'){ phaseOrder.forEach(p=>{ if(phaseOrder.indexOf(p)<phaseOrder.indexOf(name)){phaseEls[p].classList.remove('active');phaseEls[p].classList.add('done');} }); }
  el.classList.add('active');
  const note=$('phase-note'); if(note) note.textContent='esptool: '+name+(name==='write'&&arguments[1]!==undefined?` (${arguments[1]}%)`:'');
}
function phaseReset(){ phaseOrder.forEach(p=>{phaseEls[p].classList.remove('active','done');}); const n=$('phase-note'); if(n) n.textContent='esptool idle'; }

/* console feed: phase frames + lines */
let monSock=null, monBackoff=500;
function openMon(){
  try{
    monSock=new WebSocket(wsUrl.replace('/relay','/console'));
    monSock.onopen=()=>{monBackoff=500;};
    monSock.onmessage=ev=>{
      if(ev.data[0]==='{'){ const m=JSON.parse(ev.data);
        if(m.t==='phase'){ phaseHit(m.phase, m.pct);
          if(typeof m.pct==='number') phaseHit('write', m.pct); }
      } else { log(ev.data); } };
    monSock.onclose=()=>{ setTimeout(openMon,monBackoff=Math.min(monBackoff*2,8000)); };
  }catch(e){}
}
const wsUrl=(location.protocol==='https:'?'wss://':'ws://')+location.host+'/relay';
openMon();

/* ONE port lifecycle: everything runs through these functions; no stale
   reader/writer zombies across link changes (field: TX path down). */
let ws=null, port=null, reader=null, writer=null, reading=false, sigPoll=false;

async function openPort(baud){
  port=await navigator.serial.requestPort();
  await port.open({baudRate:baud, dataBits:8, stopBits:1, parity:'none'});
  writer=port.writable.getWriter();
  log('serial port opened at '+baud,'ok');
}
async function closePort(){
  reading=false;
  try{ reader && await reader.cancel(); }catch(e){}
  try{ reader && reader.releaseLock(); }catch(e){}
  try{ writer && writer.releaseLock(); }catch(e){}
  reader=null; writer=null;
  try{ port && await port.close(); }catch(e){}
  port=null;
}
async function pumpReads(){
  reading=true;
  while(port && port.readable && reading){
    try{
      reader=port.readable.getReader();
      for(;;){
        const {value,done}=await reader.read();
        if(done) break;
        if(value && ws && ws.readyState===1){ monAdd(value,'rx'); ws.send(value); }
      }
    } catch(e){ log('read: '+e,'bad'); await new Promise(r=>setTimeout(r,200)); }
    finally{ try{ reader && reader.releaseLock(); }catch(e){} reader=null; }
  }
}
async function pollSignals(){
  sigPoll=true;
  while(port && sigPoll){
    try{ const s=await port.getSignals();
      if(ws && ws.readyState===1) ws.send(JSON.stringify({t:'sig',
        cts:s.clearToSend, dsr:s.dataSetReady, cd:s.dataCarrierDetect, ri:false}));
    }catch(e){}
    await new Promise(r=>setTimeout(r,250));
  }
}

async function connect(){
  try{
    setStateEx('requesting port\u2026');
    await closePort();                       // kill any zombie state first
    const baud=parseInt(($('baud')?$('baud').value:'115200'),10)||115200;
    await openPort(baud);
    if(ws && ws.readyState===1){ ws.close(); ws=null; }
    ws=new WebSocket(wsUrl); ws.binaryType='arraybuffer';
    ws.onopen=()=>{ log('relay daemon linked ('+baud+')','ok');
      ws.send(JSON.stringify({t:'hello',baud})); setStateEx('link up','up'); };
    ws.onmessage=async ev=>{
      if(typeof ev.data==='string'){
        let m=null; try{ m=JSON.parse(ev.data); }catch(e){ return; }
        if(m.t==='sig'){
          try{ await port.setSignals({
            dataTerminalReady: m.dtr!==undefined ? !!m.dtr : undefined,
            requestToSend:     m.rts!==undefined ? !!m.rts : undefined,
            break:             m.brk!==undefined ? !!m.brk : undefined }); }
          catch(e){ log('setSignals: '+e,'bad'); }
        } else if(m.t==='reopen'){
          log('reopen at '+m.baud+'\u2026');
          reading=false;
          try{ reader && await reader.cancel(); }catch(e){}
          try{ reader && reader.releaseLock(); }catch(e){}
          try{ writer && writer.releaseLock(); }catch(e){}
          await port.close();
          await port.open({baudRate:m.baud, dataBits:8, stopBits:1, parity:'none'});
          writer=port.writable.getWriter();
          log('reopened at '+m.baud,'ok');
          pumpReads(); pollSignals();
        }
      } else {
        const u8=new Uint8Array(ev.data);
        monAdd(u8,'tx');
        try{ await writer.write(u8); }catch(e){ log('write: '+e,'bad'); }
      }
    };
    ws.onclose=()=>{ log('relay link closed','bad'); setStateEx('link down \u2014 reload the page and reconnect','down'); reading=false; };
    pumpReads(); pollSignals();
  }catch(e){
    log('connect: '+(e.message||e.name||e),'bad');
    setStateEx('failed \u2014 '+(e.message||e.name||e),'down');
    try{ await closePort(); }catch(_){}
  }
}
$('btn').onclick=connect; $('btn-adv').onclick=connect;

/* Web Serial compat banner */
window.addEventListener('DOMContentLoaded',()=>{
  const c=$('compat');
  if(!('serial' in navigator)){
    c.style.display='block';
    c.innerHTML='<div class="box-label">Web Serial unavailable</div><p class="hint">This browser has no <em>navigator.serial</em>. Use Chrome or Edge. If you opened this page over plain http://, reopen it via the https tailnet link.</p>';
    $('btn').disabled=true; $('btn-adv').disabled=true;
  } else if(!window.isSecureContext){
    c.style.display='block';
    c.innerHTML='<div class="box-label">Insecure context</div><p class="hint">Web Serial needs https. Open the page via the <em>tailnet https</em> link, not http.</p>';
    $('btn').disabled=true; $('btn-adv').disabled=true;
  }
});

/* Easy/Advanced mode (default Easy, persisted) */
function setMode(mode){
  const easy = mode !== 'adv';
  document.body.classList.toggle('easy', easy);
  $('mode-easy').classList.toggle('on', easy);
  $('mode-adv').classList.toggle('on', !easy);
  try{ localStorage.setItem('relay-mode', easy ? 'easy' : 'adv'); }catch(e){}
}
$('mode-easy').onclick=()=>setMode('easy');
$('mode-adv').onclick=()=>setMode('adv');
try{ setMode(localStorage.getItem('relay-mode') || 'easy'); }catch(e){ setMode('easy'); }
</script><script>
/* rung 20: light/dark — stored choice wins; otherwise follow system, live. */
(function(){
  var mq = window.matchMedia('(prefers-color-scheme: light)');
  var btn = document.getElementById('theme-toggle');
  var current = 'dark';
  function apply(mode, label){
    if(mode === 'light') document.documentElement.setAttribute('data-theme','light');
    else document.documentElement.removeAttribute('data-theme');
    current = mode;
    if(btn) btn.textContent = mode === 'light' ? '☀ light' : '◐ system';
    void label;
  }
  var stored = null;
  try { stored = localStorage.getItem('relay-theme'); } catch(e){}
  if(stored === 'light' || stored === 'dark'){ apply(stored); }
  else { apply(mq.matches ? 'light' : 'dark');
         try{ mq.addEventListener('change', function(e){ apply(e.matches ? 'light':'dark'); }); }catch(e){}
       }
  if(btn) btn.onclick = function(){
    var next = current === 'light' ? 'dark' : 'light';
    try { localStorage.setItem('relay-theme', next); } catch(e){}
    apply(next);
  };
})();
</script>
</body></html>"""


def render_serial(b):
    """Bytes -> display text for the live mirror (printable kept, rest ·)."""
    out = []
    for byte in b:
        if byte == 10:
            out.append("\\n")
        elif byte == 13:
            out.append("\\r")
        elif byte == 9:
            out.append("\\t")
        elif 32 <= byte < 127:
            out.append(chr(byte))
        else:
            out.append("·")
    return "".join(out)


DASH_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Studio Alight — Relay Status</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;700&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
 :root{ --color-bg:#0a0a0a; --color-text:#f5f5f5; --color-accent:#ff0000;
        --font-display:'Space Grotesk',sans-serif; --font-mono:'Space Mono',monospace;
        --color-mute:#999; --color-line:#2a2a2a; --color-ok:#58c158; --color-bad:#e05a5a ,
          --color-panel:#161616; --color-inset:#050505; --color-inset-text:#b9c2b9;
          --color-faint:#666; --color-line-soft:#3a3a3a }
 :root[data-theme="light"]{ --color-bg:#f7f4ee; --color-text:#17181a; --color-accent:#c1121f;
          --color-mute:#5c5f63; --color-line:#c9c9c9; --color-ok:#20713f; --color-bad:#b03232;
          --color-panel:#ffffff; --color-inset:#fbfaf7; --color-inset-text:#2c2e30;
          --color-faint:#8a8d90; --color-line-soft:#c9c9c9 }
 *{box-sizing:border-box}
 body{font-family:var(--font-display);background:var(--color-bg);color:var(--color-text);
      line-height:1.55;max-width:820px;margin:0 auto;padding:3rem 1rem 4rem;min-height:100vh}
 .brand{display:flex;align-items:baseline;gap:.8rem;flex-wrap:wrap}
 .mark{width:.62rem;height:.62rem;border-radius:50%;background:var(--color-accent);
       box-shadow:0 0 10px 2px rgba(255,0,0,.45);align-self:center}
 .wordmark{font-size:1rem;font-weight:400;letter-spacing:.15em;text-transform:uppercase;
       display:inline-flex;white-space:nowrap;align-self:center}
 .wordmark .dim{color:rgba(128,128,128,0.6)}
 .wordmark .hot{color:var(--color-accent)}
 .tagline{color:var(--color-mute);font-weight:300;letter-spacing:.08em;font-size:.85rem}
 .rule{border:0;border-top:1px solid var(--color-line);margin:1.4rem 0}
 .statusgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(10rem,1fr));gap:.6rem;margin:1.2rem 0}
 .stat{border:1px solid var(--color-line);border-radius:8px;padding:.7rem .85rem;
       background:linear-gradient(180deg,rgba(255,255,255,.02),transparent 60%)}
 .stat .k{font-family:var(--font-mono);font-size:.58rem;letter-spacing:.14em;
       text-transform:uppercase;color:var(--color-mute)}
 .stat .v{font-family:var(--font-mono);font-size:.92rem;margin-top:.3rem;word-break:break-all}
 .v.up{color:var(--color-ok)} .v.down{color:var(--color-bad)}
 .box{border:1px solid var(--color-line);border-radius:10px;padding:1.1rem;margin:1.2rem 0;
       background:linear-gradient(180deg,rgba(255,255,255,.02),transparent 60%)}
 .box-label{font-family:var(--font-mono);font-size:.66rem;letter-spacing:.14em;
       text-transform:uppercase;color:var(--color-mute);margin-bottom:.7rem;
       display:flex;align-items:center;gap:.6rem;flex-wrap:wrap}
 .phases{display:flex;gap:.5rem;flex-wrap:wrap;margin:.2rem 0 .6rem}
 .phase{font-family:var(--font-mono);font-size:.66rem;letter-spacing:.1em;text-transform:uppercase;
       padding:.34rem .6rem;border:1px solid var(--color-line);border-radius:4px;color:var(--color-mute);
       display:flex;align-items:center;gap:.4rem;transition:all .25s}
 .phase .dot{width:.4rem;height:.4rem;border-radius:50%;background:#3a3a3a;transition:all .25s}
 .phase.active{border-color:var(--color-accent);color:var(--color-text);box-shadow:0 0 10px rgba(255,0,0,.2)}
 .phase.active .dot{background:var(--color-accent);box-shadow:0 0 8px rgba(255,0,0,.6)}
 .phase.done{color:var(--color-text)} .phase.done .dot{background:var(--color-ok)}
 #mon{font-family:var(--font-mono);font-size:.66rem;line-height:1.5;background:var(--color-inset);
       border:1px solid var(--color-line);border-radius:8px;padding:.8rem;height:18rem;
       overflow-y:auto;white-space:pre-wrap;word-break:break-all;color:var(--color-inset-text)}
 #mon .rx{color:#7ea87e} #mon .tx{color:#c8896a}
 #dlog{font-family:var(--font-mono);font-size:.64rem;line-height:1.45;white-space:pre-wrap;
       color:var(--color-ok);max-height:12rem;overflow-y:auto;background:var(--color-inset);
       border:1px solid var(--color-line);border-radius:8px;padding:.7rem}
 #dlog .bad{color:var(--color-bad)} #dlog .warn{color:#d8b966}
 .hint{color:var(--color-mute);font-size:.85rem}
 .hint em{color:var(--color-text);font-style:normal}
 #live{font-family:var(--font-mono);font-size:.62rem;letter-spacing:.08em}
 #live.on{color:var(--color-ok)} #live.off{color:var(--color-mute)}
 #mon-clear{font-family:var(--font-mono);font-size:.62rem;padding:.2rem .5rem;border-radius:4px;
       border:1px solid var(--color-line);background:transparent;color:var(--color-mute);
       cursor:pointer;margin-left:auto}
 #mon-clear:hover{color:var(--color-text)}
 footer{margin-top:2rem;color:var(--color-mute);font-size:.8rem;letter-spacing:.05em}
 footer b{color:var(--color-text);font-weight:500}
 footer a{color:var(--color-mute)} footer a:hover{color:var(--color-text)}
 .theme-toggle{font-family:var(--font-mono);font-size:.62rem;
        padding:.25rem .6rem;border-radius:4px;border:1px solid var(--color-line);
        background:transparent;color:var(--color-mute);cursor:pointer}
 .theme-toggle:hover{color:var(--color-text);border-color:var(--color-line-soft)}
</style></head><body>
<div class="brand">
 <button class="theme-toggle" id="theme-toggle" title="toggle light/dark (follows system by default)">◐ system</button>
 <span class="mark"></span>
 <span class="wordmark">
   <span class="hot">S</span><span class="dim">TUDIO</span><span style="width:.4em;display:inline-block"></span><span class="hot">A</span><span class="dim">LIGHT</span>
 </span>
 <span class="tagline">We Alight the Invisible</span>
</div>
<hr class="rule">
<div class="statusgrid">
 <div class="stat"><div class="k">serial link — mac tab</div><div class="v" id="st-serial">…</div></div>
 <div class="stat"><div class="k">rfc2217 :4001</div><div class="v" id="st-rfc">…</div></div>
 <div class="stat"><div class="k">console listeners</div><div class="v" id="st-listeners">…</div></div>
 <div class="stat"><div class="k">uptime</div><div class="v" id="st-uptime">…</div></div>
 <div class="stat"><div class="k">serial bytes</div><div class="v" id="st-bytes">…</div></div>
 <div class="stat"><div class="k">last activity</div><div class="v" id="st-last">…</div></div>
</div>
<div class="box">
 <div class="box-label">Esptool phases</div>
 <div class="phases">
  <span class="phase" id="ph-connect"><span class="dot"></span>connect</span>
  <span class="phase" id="ph-sync"><span class="dot"></span>sync</span>
  <span class="phase" id="ph-erase"><span class="dot"></span>write</span>
  <span class="phase" id="ph-verify"><span class="dot"></span>verify</span>
  <span class="phase" id="ph-reset"><span class="dot"></span>reset</span>
 </div>
 <p class="hint" id="phase-note">esptool idle</p>
</div>
<div class="box">
 <div class="box-label">Live serial mirror — device ⇄ gateway
  <span id="live" class="off">○ connecting</span>
  <button id="mon-clear">clear</button>
 </div>
 <div id="mon"></div>
 <p class="hint" style="margin-top:.6rem">Every byte crossing the relay, both directions, batched ~150 ms.
   The mirror stays live even when no esptool client holds the port. ↑ = device → gateway, ↓ = gateway → device.</p>
</div>
<div class="box">
 <div class="box-label">Daemon log — tail</div>
 <div id="dlog"></div>
</div>
<footer>relay status v8 · <b>Studio Alight</b> 2026 · <a href="/">← serial relay control</a></footer>
<script>
const $=id=>document.getElementById(id);
function fmtDur(s){ if(s==null)return'—'; s=Math.max(0,s|0);
  if(s<60)return s+'s'; if(s<3600)return Math.floor(s/60)+'m '+(s%60)+'s';
  return Math.floor(s/3600)+'h '+Math.floor((s%3600)/60)+'m'; }
function fmtB(n){ return n>=1048576 ? (n/1048576).toFixed(2)+' MB'
  : n>=1024 ? (n/1024).toFixed(1)+' kB' : n+' B'; }
const phaseOrder=['connect','sync','write','verify','reset'];
const phaseEls={}; phaseOrder.forEach(p=>phaseEls[p]=$('ph-'+p));
function phaseHit(name,pct){
  const el=phaseEls[name]; if(!el) return;
  if(name!=='reset'){ phaseOrder.forEach(p=>{ if(phaseOrder.indexOf(p)<phaseOrder.indexOf(name)){phaseEls[p].classList.remove('active');phaseEls[p].classList.add('done');} }); }
  el.classList.add('active');
  const n=$('phase-note');
  if(n) n.textContent='esptool: '+name+(name==='write'&&typeof pct==='number'?` (${pct}%)`:'');
}
function monAdd(dir,ts,text){
  const line=document.createElement('div'); line.className=dir;
  line.textContent=ts+(dir==='rx'?' \\u2191 ':' \\u2193 ')+text;
  const mon=$('mon');
  const stick=mon.scrollHeight-mon.scrollTop-mon.clientHeight<48;
  mon.append(line);
  if(stick) mon.scrollTop=mon.scrollHeight;
  while(mon.children.length>600) mon.firstChild.remove();
}
$('mon-clear').onclick=()=>{$('mon').innerHTML='';};
let monSock=null, backoff=500;
function setLive(on){
  const l=$('live'); l.className=on?'on':'off';
  l.textContent=on?'\\u25cf live':'\\u25cb connecting';
}
function openMon(){
  try{
    monSock=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/console');
    monSock.onopen=()=>{backoff=500;setLive(true);};
    monSock.onmessage=ev=>{
      if(ev.data[0]!=='{') return;
      let m; try{m=JSON.parse(ev.data)}catch(e){return}
      if(m.t==='mon') monAdd(m.dir,m.ts,m.text);
      else if(m.t==='phase') phaseHit(m.phase,m.pct);
    };
    monSock.onclose=()=>{ setLive(false); setTimeout(openMon,backoff=Math.min(backoff*2,8000)); };
  }catch(e){ setLive(false); setTimeout(openMon,2000); }
}
function renderLog(tail){
  const l=$('dlog'); l.innerHTML='';
  (tail||[]).forEach(m=>{ const d=document.createElement('div');
    if(m.indexOf('ERROR')>=0) d.className='bad'; else if(m.indexOf('WARNING')>=0) d.className='warn';
    d.textContent=m; l.append(d); });
  l.scrollTop=l.scrollHeight;
}
async function poll(){
  try{
    const r=await fetch('/status.json',{cache:'no-store'});
    const s=await r.json();
    const el=(id,txt,cls)=>{ const e=$(id); e.textContent=txt; e.className='v'+(cls?' '+cls:''); };
    el('st-serial', s.serial_link?'up':'down', s.serial_link?'up':'down');
    el('st-rfc', s.rfc2217, s.rfc2217==='serving'?'up':null);
    el('st-listeners', s.console_listeners);
    el('st-uptime', fmtDur(s.uptime_s));
    el('st-bytes', fmtB(s.counts.rx)+' \\u2191 · '+fmtB(s.counts.tx)+' \\u2193');
    el('st-last', s.last_activity_s==null?'—':s.last_activity_s+'s ago');
    if(s.esptool_phase) phaseHit(s.esptool_phase.phase, s.esptool_phase.pct);
    renderLog(s.log_tail);
    if(s.mon_tail && s.mon_tail.length && $('mon').children.length===0){
      s.mon_tail.forEach(e=>monAdd(e[1],e[0],e[2]));
    }
  }catch(e){}
}
poll(); setInterval(poll,2000); openMon();
</script><script>
(function(){
  var mq = window.matchMedia('(prefers-color-scheme: light)');
  var btn = document.getElementById('theme-toggle');
  var current = 'dark';
  function apply(mode){
    if(mode === 'light') document.documentElement.setAttribute('data-theme','light');
    else document.documentElement.removeAttribute('data-theme');
    current = mode;
    if(btn) btn.textContent = mode === 'light' ? '☀ light' : '◐ system';
  }
  var stored = null;
  try { stored = localStorage.getItem('relay-theme'); } catch(e){}
  if(stored === 'light' || stored === 'dark'){ apply(stored); }
  else { apply(mq.matches ? 'light' : 'dark');
         try{ mq.addEventListener('change', function(e){ apply(e.matches ? 'light':'dark'); }); }catch(e){} }
  if(btn) btn.onclick = function(){
    var next = current === 'light' ? 'dark' : 'light';
    try { localStorage.setItem('relay-theme', next); } catch(e){}
    apply(next);
  };
})();
</script>
</body></html>"""


class WebSerialBridge:
    """The 'serial' object PortManager drives; routed over the browser WS."""

    def __init__(self, relay):
        self.relay = relay
        self._baud = 115200
        self.bytesize = 8
        self.parity = 'N'
        self.stopbits = 1
        self.xonxoff = False
        self.rtscts = False
        self.break_condition = False
        self._dtr = True
        self._rts = True
        # control-line inputs reported by the browser
        self.cts = True
        self.dsr = True
        self.cd = True
        self.ri = False

    @property
    def baudrate(self):
        return self._baud

    @baudrate.setter
    def baudrate(self, v):
        # Web Serial can't change baud on an open port. When esptool
        # negotiates a different rate than the tab opened with, ask the tab
        # to reopen (it drops the port, reopens with the new baud, resumes
        # the read pump). Field flow 2026-10-15: 115200 -> whatever esptool
        # sets (--baud on flash).
        v = int(v)
        if v != self._baud:
            log.info("esptool SET_BAUDRATE %d != tab-open %d — requesting reopen",
                     v, self._baud)
            self._baud = v
            self.relay.send_json({"t": "reopen", "baud": v})

    def _push_sig(self):
        self.relay.send_json({"t": "sig", "dtr": self._dtr,
                              "rts": self._rts, "brk": self.break_condition})

    @property
    def dtr(self):
        return self._dtr

    @dtr.setter
    def dtr(self, v):
        self._dtr = bool(v)
        self._push_sig()

    @property
    def rts(self):
        return self._rts

    @rts.setter
    def rts(self, v):
        self._rts = bool(v)
        self._push_sig()

    def write(self, data):
        self.relay.send_bytes(bytes(data))

    def read(self, size=1, timeout=None):
        return b""  # serial->network path is push-based (on_serial_bytes)

    @property
    def in_waiting(self):
        return 0

    def reset_input_buffer(self):
        pass

    def reset_output_buffer(self):
        pass


class _SockConn:
    """PortManager's connection: thread-safe write into the asyncio loop."""

    def __init__(self, writer):
        self.writer = writer

    def write(self, data):
        LOOP.call_soon_threadsafe(self._send, bytes(data))

    def _send(self, data):
        try:
            self.writer.write(data)
        except Exception as e:
            log.warning("rfc2217 write failed: %s", e)


class Relay:
    def __init__(self):
        self.ws = None              # browser websocket (serial link)
        self.consoles = set()        # ALL console listeners (broadcast)
        self.pm = None              # active PortManager (one esptool client)
        self.pm_writer = None        # its stream writer
        self.browser_ready = asyncio.Event()
        # ---- status dashboard / live mirror (v8) ----
        self.mon_lines = deque(maxlen=600)   # (ts, dir, rendered text)
        self.mon_buf = {"rx": bytearray(), "tx": bytearray()}
        self.counts = {"rx": 0, "tx": 0}
        self.last_activity = None
        self.phase = None        # last esptool phase frame from the shim
        self._mon_flush_scheduled = False

    # ---------- browser side ----------

    async def browser_handler(self, connection):
        path = connection.request.path if connection.request else "/relay"
        if path == "/console":
            # Phase feed: BROADCAST model (field 18:09 — the shim's connect
            # replaced the page's console feed and the phase strip never
            # lit). Many listeners; every text frame goes to all of them.
            # Producers: the esptool shim; consumers: browser tabs.
            self.consoles.add(connection)
            log.info("console link up (%d)", len(self.consoles))
            try:
                async for msg in connection:
                    if isinstance(msg, str):
                        if msg[:1] == "{":
                            try:
                                m = json.loads(msg)
                                if m.get("t") == "phase":
                                    self.phase = {"phase": m.get("phase"),
                                                  "pct": m.get("pct")}
                            except Exception:
                                pass
                        dead = []
                        for c in list(self.consoles):
                            if c is connection:
                                continue
                            try:
                                await c.send(msg)
                            except websockets.ConnectionClosed:
                                dead.append(c)
                        for c in dead:
                            self.consoles.discard(c)
            except websockets.ConnectionClosed:
                pass
            finally:
                self.consoles.discard(connection)
                log.info("console link down (%d)", len(self.consoles))
            return

        if self.ws is not None:
            log.warning("second browser link — replacing")
            try:
                await self.ws.close()
            except Exception:
                pass
        self.ws = connection
        self.browser_ready.set()
        log.info("browser link up")
        try:
            async for msg in connection:
                if isinstance(msg, bytes):
                    self.monitor_tap(msg, "rx")
                    self.on_serial_bytes(msg)
                else:
                    m = json.loads(msg)
                    if m.get("t") == "hello":
                        log.info("BROWSER hello: baud=%s (port currently open)", m.get("baud"))
                    if m.get("t") == "ann":
                        # FIELD 2026-10-26: annotations used to be browser-local
                        # only and died with the tab. Mirror them into the relay
                        # log so event markers survive and correlate with capture.
                        log.info("\u25c6 ANN: %s", str(m.get("text", ""))[:120])
                    if m.get("t") == "sig" and self.pm is not None:
                        b = self.pm.serial
                        b.cts = bool(m.get("cts", True))
                        b.dsr = bool(m.get("dsr", True))
                        b.cd = bool(m.get("cd", True))
                        b.ri = bool(m.get("ri", False))
                        self.pm.check_modem_lines()
        except websockets.ConnectionClosed:
            pass
        finally:
            log.info("browser link down")
            self.ws = None
            self.browser_ready.clear()

    def on_serial_bytes(self, data):
        """USB -> esptool: escape through PortManager, write to socket."""
        if self.pm is None or self.pm_writer is None:
            return
        try:
            out = b"".join(self.pm.escape(data))
        except Exception as e:
            log.warning("escape failed: %s", e)
            return
        LOOP.call_soon_threadsafe(self._sock_send, out)

    def _sock_send(self, data):
        try:
            self.pm_writer.write(data)
        except Exception as e:
            log.warning("rfc2217 send: %s", e)

    # ---------- thread-safe senders into the loop ----------

    def send_bytes(self, data):
        """esptool -> USB: binary frame to the browser WS."""
        self.monitor_tap(data, "tx")
        if self.ws is not None and LOOP is not None:
            asyncio.run_coroutine_threadsafe(self._ws_send(data), LOOP)

    def send_json(self, obj):
        """Control frames are WS TEXT frames — a binary sig frame would be
        routed to the browser's serial writer (and corrupt a real flash);
        caught by the pty loopback 2026-10-15."""
        if self.ws is not None and LOOP is not None:
            asyncio.run_coroutine_threadsafe(
                self._ws_send_text(json.dumps(obj)), LOOP)

    async def _ws_send(self, data):
        if self.ws is not None:
            try:
                await self.ws.send(data)
            except websockets.ConnectionClosed:
                self.ws = None   # dead link: yield the slot (field 16:58)

    async def _ws_send_text(self, s):
        if self.ws is not None:
            try:
                await self.ws.send(s)   # str -> text frame; JS sees a string
            except websockets.ConnectionClosed:
                self.ws = None

    # ---------- status dashboard / live mirror ----------

    def monitor_tap(self, data, direction):
        """Every byte that crosses the relay, either way, feeds the mirror."""
        self.counts[direction] = self.counts.get(direction, 0) + len(data)
        self.last_activity = time.time()
        buf = self.mon_buf[direction]
        room = 2048 - len(buf)
        if room > 0:
            chunk = data[:room]
            buf += chunk
            if len(chunk) < len(data):
                buf += b" ~trunc~"
        if self._mon_flush_scheduled or LOOP is None:
            return
        self._mon_flush_scheduled = True
        try:
            LOOP.call_soon_threadsafe(self._mon_flush_start)
        except RuntimeError:
            self._mon_flush_scheduled = False

    def _mon_flush_start(self):
        try:
            LOOP.create_task(self._mon_flush())
        except Exception:
            self._mon_flush_scheduled = False

    async def _mon_flush(self):
        await asyncio.sleep(0.15)
        self._mon_flush_scheduled = False
        ts = time.strftime("%H:%M:%S")
        frames = []
        for d in ("rx", "tx"):
            buf = self.mon_buf[d]
            if not buf:
                continue
            text = render_serial(bytes(buf))
            self.mon_buf[d] = bytearray()
            self.mon_lines.append((ts, d, text))
            frames.append(json.dumps({"t": "mon", "dir": d, "ts": ts,
                                      "text": text}))
        for frame in frames:
            dead = []
            for c in list(self.consoles):
                try:
                    await c.send(frame)
                except websockets.ConnectionClosed:
                    dead.append(c)
            for c in dead:
                self.consoles.discard(c)

    def status_snapshot(self):
        now = time.time()
        return {
            "ok": True,
            "version": RELAY_VERSION,
            "uptime_s": int(now - START_TS),
            "serial_link": self.ws is not None,
            "rfc2217": "serving" if self.pm is not None else "free",
            "console_listeners": len(self.consoles),
            "counts": dict(self.counts),
            "last_activity_s": (int(now - self.last_activity)
                                if self.last_activity else None),
            "esptool_phase": self.phase,
            "mon_tail": list(self.mon_lines)[-30:],
            "log_tail": list(LOG_RING.deque)[-40:],
        }

    # ---------- RFC2217 side (esptool clients) ----------

    async def rfc2217_client(self, reader, writer):
        """Fully in-loop: asyncio reader/writer, no raw-socket thread."""
        if self.pm is not None:
            log.warning("rfc2217: client already active — refusing new one")
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            return

        # Slot guard: if a previous client died without cleanup, reap it.
        if self.pm_writer is not None:
            try:
                self.pm_writer.close()
            except Exception:
                pass
            self.pm_writer = None
        peer = writer.get_extra_info("peername")
        log.info("rfc2217 client from %s", peer)

        # Handshake FIRST (field fix 17:26): esptool times out in ~2s if the
        # server doesn't negotiate — gating the whole PortManager on
        # browser_ready made every reflash fail "does not support RFC2217"
        # whenever the tab's serial link wasn't up yet. Data forwarding is
        # gated instead, further down.
        bridge = WebSerialBridge(self)
        conn = _SockConn(writer)
        pm = PortManager(bridge, conn, logger=log)
        self.pm = pm
        self.pm_writer = writer
        log.info("rfc2217 serving %s", peer)

        try:
            # NEGOTIATION MUST NEVER BE GATED (field 17:16): esptool's
            # protocol answers sit unread while the loop waits for the
            # browser — deadlock at ~2s, "does not support RFC2217".
            # filter() processes telnet/RFC2217 control all the time; only
            # the PAYLOAD forwarding is gated on the browser link.
            started = False
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                batch = bytearray()
                for b in pm.filter(data):
                    batch += b
                if batch:
                    started = True
                    if self.browser_ready.is_set():
                        await self._ws_send(bytes(batch))
                    # else: payload dropped when no browser link (USB is
                    # nowhere connected — nothing to lose)
        except (OSError, asyncio.IncompleteReadError) as e:
            log.info("rfc2217 client closed: %s", e)
        finally:
            if self.pm is pm:
                log.info("rfc2217 client gone")
                self.pm = None
                self.pm_writer = None
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            # belt+braces: any transport remnant must not block the next client
            transport = writer.transport
            if transport is not None:
                try:
                    transport.abort()
                except Exception:
                    pass

    def _client_gone(self, pm):
        if self.pm is pm:
            log.info("rfc2217 client gone")
            self.pm = None
            self.pm_writer = None
            try:
                pm.connection.writer.close()
            except Exception:
                pass


async def http_gate(connection, request):
    if request.path == "/":
        body = PAGE.encode()
        return Response(200, "OK", Headers([
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(body))),
        ]), body)
    if request.path == "/healthz":
        # status detail: browser serial link + console feeds + rfc client
        r = relay_state.get("relay")
        if r is None:
            from serial_relay import RELAY  # fallback import path
            r = RELAY
        n_consoles = len(getattr(r, "consoles", []))
        serial = "linked" if (r.ws is not None) else "NOT linked"
        rfc = "serving" if r.pm is not None else "free"
        body = (f"serial-relay up | serial-link: {serial} | "
                f"console-listeners: {n_consoles} | rfc2217: {rfc}\n")
        return Response(200, "OK", Headers([("Content-Type", "text/plain")]),
                        body.encode())
    if request.path == "/dashboard":
        body = DASH_PAGE.encode()
        return Response(200, "OK", Headers([
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(body))),
        ]), body)
    if request.path == "/status.json":
        r = relay_state.get("relay")
        snap = r.status_snapshot() if r is not None else {"ok": False}
        body = json.dumps(snap).encode()
        return Response(200, "OK", Headers([("Content-Type", "application/json")]),
                        body)
    if request.path == "/favicon.ico":
        # 1x1 transparent PNG: stops the browser's auto-favicon hit from
        # reaching the WS handshake and logging a 426 (field 21:37).
        import base64
        PNG_1PX = base64.b64decode(
            b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nG"
            b"NgYGBgAAAABQABh6FO1AAAAABJRU5ErkJggg==")
        return Response(200, "OK", Headers([("Content-Type", "image/png")]), PNG_1PX)
    return None  # /relay and others: fall through to WS handling


RELAY = None
relay_state = {}

async def main():
    global LOOP, RELAY
    LOOP = asyncio.get_running_loop()
    relay = Relay()
    RELAY = relay
    relay_state["relay"] = relay

    ws_server = await websockets.serve(
        relay.browser_handler, "0.0.0.0", WS_PORT,
        process_request=http_gate, max_size=None, compression="deflate")
    log.info("browser page + WS on :%d", WS_PORT)

    rfc_server = await asyncio.start_server(
        relay.rfc2217_client, RFC_BIND, RFC_PORT)
    log.info("rfc2217 on %s:%d — esptool: rfc2217://127.0.0.1:%d",
             RFC_BIND, RFC_PORT, RFC_PORT)

    await asyncio.gather(ws_server.wait_closed(), rfc_server.serve_forever())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass