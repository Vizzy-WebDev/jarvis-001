"""The "Take control" page: Jarvis's browser, as a live picture you can click and type into.

Ported from OpenMuse's `apps/server/src/browser-console.ts` (MIT licence,
CopilotKit), with Jarvis's own paths, an address bar, and a way to reopen a window
that has closed.

**The page the browser is showing never runs in this document.** The console only
ever displays a screenshot (an `<img>` made from a fetched PNG), and every piece of
text that came from the remote page - its title, its address - is put in with
`textContent`, never as markup. The page itself is a fixed template; the only thing
substituted into it is the session id, which has already been checked to be a UUID
and is embedded as JSON.

Input goes back as coordinates in the screenshot's own 1280 x 800 space, scaled
from wherever the picture is displayed.
"""

from __future__ import annotations

import json

from .manager import validate_session_id

#: Sent with the page: no network except back to Jarvis, no framing, no plugins.
CONSOLE_CSP = ("default-src 'none'; img-src blob:; connect-src 'self'; style-src 'unsafe-inline'; "
               "script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; "
               "frame-ancestors 'none'")

_TEMPLATE = r"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Jarvis's browser</title><style>
:root{--bg:#fcfcfc;--ink:#172125;--soft:#697176;--line:#e1e6e9;--chip:#f1f3f4;--accent:#c8e7ff;--ok:#248258;--bad:#984a41;--badbg:#fbefed;--stage:#eef1f3}
@media (prefers-color-scheme:dark){:root{--bg:#10151a;--ink:#e6edf1;--soft:#8c98a0;--line:#26313a;--chip:#1b242c;--accent:#1f4a68;--ok:#5fcf9b;--bad:#f0a79f;--badbg:#3a1f1c;--stage:#0b0f13}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}
header{padding:12px;display:flex;align-items:center;justify-content:space-between;gap:12px}#status{color:var(--soft);font-size:12px}#status.live{color:var(--ok)}
button,input{font:inherit;color:inherit;border:1px solid var(--line);border-radius:24px;padding:10px 14px;background:var(--bg);min-height:42px}
button{cursor:pointer}button:hover{background:var(--chip)}button:disabled{opacity:.45;cursor:default}
button:focus-visible,input:focus-visible{outline:2px solid #1473c8;outline-offset:2px}
form{padding:0 12px 10px;display:flex;gap:8px}input{flex:1;min-width:0;background:var(--chip);border-color:transparent}.go{background:var(--accent)}
nav{display:flex;gap:6px;padding:0 12px 12px;flex-wrap:wrap}nav button{font-size:12px;min-height:36px;padding:7px 12px}
#where{padding:0 12px 10px;color:var(--soft);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#stage{overflow:hidden;background:var(--stage);border-radius:18px;min-height:160px;margin:0 8px}
img{display:block;width:100%;height:auto;cursor:crosshair;touch-action:pan-y}img.stale{opacity:.45;pointer-events:none}
#error{margin:0 12px 12px;color:var(--bad);background:var(--badbg);padding:12px;border-radius:14px}#error:empty{display:none}
#closed{margin:12px;padding:16px;border-radius:14px;background:var(--chip);display:none}#closed.on{display:block}
footer{padding:12px;color:var(--soft);font-size:12px;line-height:1.5}
</style>
<header><strong>Jarvis's browser</strong><span id="status" role="status">Connecting…</span><button id="refresh" aria-label="Refresh the picture">↻</button></header>
<form id="address"><input id="url" aria-label="Web address" placeholder="Go to a web address" autocomplete="off" inputmode="url"><button class="go" type="submit">Go</button></form>
<div id="where"></div>
<div id="closed"><p>This browser window is closed. Opening it again keeps everything you were signed in to.</p><button id="reopen" class="go">Open it again</button></div>
<form id="send"><input id="text" aria-label="Text to type in the page" placeholder="Click a box on the page, then type here" autocomplete="off"><button id="type" type="submit">Send text</button></form>
<nav aria-label="Keyboard"><button data-key="Enter">Enter ↵</button><button data-key="Tab">Tab ⇥</button><button data-key="Backspace">Delete ⌫</button><button id="up">Scroll ↑</button><button id="down">Scroll ↓</button></nav>
<div id="error" role="alert"></div><div id="stage"><img id="screen" class="stale" alt="Jarvis's browser, click to interact" draggable="false"></div>
<footer>Click the page to select something, then send text above. You are controlling Jarvis's own browser: what you sign in to here stays signed in for Jarvis. Nothing from the page itself runs in this window.</footer>
<script>
const ID=__SESSION__,BASE='/api/browsers/'+ID;
const image=document.querySelector('#screen'),error=document.querySelector('#error'),status=document.querySelector('#status'),
field=document.querySelector('#text'),urlField=document.querySelector('#url'),where=document.querySelector('#where'),closed=document.querySelector('#closed');
let refreshing=false,sending=false,imageUrl,live=false,previewError=false,lastUrl='';
function controls(){document.querySelectorAll('nav button,#type').forEach(b=>b.disabled=sending||!live);image.classList.toggle('stale',!live||sending);closed.classList.toggle('on',!live&&previewError==='closed');}
async function details(){try{const r=await fetch(BASE,{cache:'no-store'});if(!r.ok)return;const d=await r.json();
where.textContent=(d.title?d.title+' - ':'')+(d.url||'');lastUrl=d.url||'';if(document.activeElement!==urlField)urlField.value=lastUrl==='about:blank'?'':lastUrl;}catch(e){}}
async function refresh(){if(refreshing||sending||document.hidden)return;refreshing=true;try{
const r=await fetch(BASE+'/preview',{cache:'no-store',signal:AbortSignal.timeout(20000)});
if(r.status===409){live=false;previewError='closed';status.textContent='Closed';status.className='';error.textContent='';await details();return;}
if(r.status===503){status.textContent='Loading…';status.className='';return;}
if(!r.ok)throw new Error(r.status===404?'That browser window no longer exists.':'The browser is not answering. Try the refresh button.');
const blob=await r.blob();const next=URL.createObjectURL(blob);
await new Promise((resolve,reject)=>{const probe=new Image();probe.onload=resolve;probe.onerror=()=>{URL.revokeObjectURL(next);reject(new Error('The picture could not be shown.'));};probe.src=next;});
if(imageUrl)URL.revokeObjectURL(imageUrl);imageUrl=next;image.src=next;live=true;status.textContent='Live';status.className='live';
if(previewError){error.textContent='';previewError=false;}await details();
}catch(e){live=false;previewError=true;status.textContent='Disconnected';status.className='';error.textContent=e.message;}finally{refreshing=false;controls();}}
async function post(path,body){const r=await fetch(BASE+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{}),signal:AbortSignal.timeout(90000)});
if(!r.ok){let m='That did not work.';try{const d=await r.json();if(typeof d.error==='string')m=d.error;}catch(e){}throw new Error(m);}return r;}
async function act(path,body){if(sending)return false;sending=true;controls();error.textContent='';status.textContent='Working…';let ok=false;
try{await post(path,body);ok=true;}catch(e){error.textContent=e.message;}finally{sending=false;controls();await refresh();}return ok;}
image.onclick=e=>{if(!live||sending)return;const r=image.getBoundingClientRect();act('/input',{type:'click',x:Math.min(1279,Math.max(0,Math.floor((e.clientX-r.left)*1280/r.width))),y:Math.min(799,Math.max(0,Math.floor((e.clientY-r.top)*800/r.height)))});};
document.querySelector('#send').onsubmit=async e=>{e.preventDefault();const text=field.value;if(text&&await act('/input',{type:'text',text})&&field.value===text)field.value='';};
document.querySelector('#address').onsubmit=e=>{e.preventDefault();const url=urlField.value.trim();if(url)act(live?'/navigate':'/open',{url:/^[a-z]+:\/\//i.test(url)?url:'https://'+url});};
document.querySelector('#reopen').onclick=()=>act('/open',{});
document.querySelectorAll('[data-key]').forEach(b=>b.onclick=()=>act('/input',{type:'key',key:b.dataset.key}));
document.querySelector('#up').onclick=()=>act('/input',{type:'scroll',deltaY:-600});document.querySelector('#down').onclick=()=>act('/input',{type:'scroll',deltaY:600});
document.querySelector('#refresh').onclick=()=>{error.textContent='';refresh();};document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
controls();refresh();const timer=setInterval(refresh,2000);window.addEventListener('pagehide',()=>{clearInterval(timer);if(imageUrl)URL.revokeObjectURL(imageUrl);});
</script></html>"""


def console_page(session_id: str) -> str:
    """The console for one session. `session_id` is checked, then embedded as JSON."""
    safe = json.dumps(validate_session_id(session_id)).replace("<", "\\u003c")
    return _TEMPLATE.replace("__SESSION__", safe)
