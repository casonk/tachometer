"""Frequent host temperature samples and the page that charts them.

The weekly host snapshot answers "what does this machine look like". It cannot
answer "was it overheating at 4 a.m.", because one reading a week is not a
history. This module takes a small sample every few minutes (CPU, GPU, board,
drives, fans, load), keeps a rolling window of them in a JSON-lines file, and
renders that window as a time series with a stoplight for the latest reading.

Everything is read from sysfs and, for the GPU, `nvidia-smi`; nothing here
needs root. SATA drive temperatures appear only when the kernel's `drivetemp`
module is loaded.
"""

from __future__ import annotations

import contextlib
import html
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .profile import _read_cpu_temp

SAMPLES_NAME = "host-samples.jsonl"
DEFAULT_KEEP_DAYS = 14
RANGES = {"6h": 6, "24h": 24, "7d": 168}

# (yellow at or above, red at or above), Celsius. Generic desktop guidance:
# yellow is "worth a look if sustained", red sits a little under the point
# where parts throttle or exceed their rated operating range.
LIMITS = {
    "cpu": (80.0, 90.0),
    "gpu": (78.0, 85.0),
    "board": (55.0, 65.0),
    "nvme": (70.0, 80.0),
    "disk": (50.0, 60.0),
}
_LIGHT_WORD = {"green": "OK", "yellow": "Watch", "red": "Hot", "unknown": "No data"}
_LIGHT_ICON = {"green": "✓", "yellow": "▲", "red": "✖", "unknown": "–"}
_LIGHT_RANK = {"unknown": 0, "green": 1, "yellow": 2, "red": 3}


def samples_path(tachometer_root: Path) -> Path:
    return Path(tachometer_root) / ".tachometer" / SAMPLES_NAME


def light_for(kind: str, value: float | None) -> str:
    if value is None:
        return "unknown"
    yellow_at, red_at = LIMITS[kind]
    return "red" if value >= red_at else "yellow" if value >= yellow_at else "green"


def _milli(path: Path) -> float | None:
    with contextlib.suppress(Exception):
        return int(path.read_text(encoding="utf-8").strip()) / 1000.0
    return None


def _text(path: Path) -> str:
    with contextlib.suppress(Exception):
        return path.read_text(encoding="utf-8").strip()
    return ""


def _read_board_temp(sys_class: Path) -> float | None:
    """Motherboard ambient: the Super I/O chip's SYSTIN, else the ACPI zone."""
    for chip in sorted((sys_class / "hwmon").glob("hwmon*")):
        if not _text(chip / "name").startswith(("nct", "it87", "f718")):
            continue
        for label in sorted(chip.glob("temp*_label")):
            if _text(label).upper() in ("SYSTIN", "SYSTEM", "MB TEMP"):
                temp = _milli(label.with_name(label.name.replace("_label", "_input")))
                if temp is not None:
                    return temp
    for zone in sorted((sys_class / "thermal").glob("thermal_zone*")):
        if _text(zone / "type") == "acpitz":
            return _milli(zone / "temp")
    return None


def _read_drive_temps(sys_class: Path) -> dict[str, float]:
    """Temperature per drive, keyed `nvme:<device>` or `disk:<device>`."""
    drives: dict[str, float] = {}
    for chip in sorted((sys_class / "hwmon").glob("hwmon*")):
        name = _text(chip / "name")
        if name not in ("nvme", "drivetemp"):
            continue
        temp = _milli(chip / "temp1_input")
        if temp is None:
            continue
        device = chip.name
        with contextlib.suppress(Exception):
            device = (chip / "device").resolve().name
        drives[f"{'nvme' if name == 'nvme' else 'disk'}:{device}"] = temp
    return drives


def _read_fan_rpm_max(sys_class: Path) -> int | None:
    best: int | None = None
    for fan in (sys_class / "hwmon").glob("hwmon*/fan*_input"):
        with contextlib.suppress(Exception):
            rpm = int(fan.read_text(encoding="utf-8").strip())
            best = rpm if best is None or rpm > best else best
    return best


def _read_gpu_temp() -> float | None:
    tool = shutil.which("nvidia-smi")
    if not tool:
        return None
    with contextlib.suppress(Exception):
        out = subprocess.run(
            [tool, "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        temps = [float(v) for v in out.stdout.split()]
        if out.returncode == 0 and temps:
            return max(temps)
    return None


def collect_sample(
    sys_class: Path = Path("/sys/class"), *, now: float | None = None, gpu_reader=_read_gpu_temp
) -> dict[str, Any]:
    """One reading of everything the page charts. Missing sensors are None."""
    load = None
    with contextlib.suppress(Exception):
        load = round(os.getloadavg()[0] / (os.cpu_count() or 1), 3)
    drives = _read_drive_temps(sys_class)
    return {
        "ts": int(now if now is not None else time.time()),
        "load": load,
        "cpu": _read_cpu_temp(sys_class),
        "gpu": gpu_reader(),
        "board": _read_board_temp(sys_class),
        "drive_max": max(drives.values()) if drives else None,
        "drives": drives,
        "fan_max": _read_fan_rpm_max(sys_class),
    }


def load_samples(path: Path, *, since: float | None = None) -> list[dict[str, Any]]:
    """Samples in file order, optionally only those at or after `since`."""
    samples: list[dict[str, Any]] = []
    with contextlib.suppress(FileNotFoundError):
        for line in path.read_text(encoding="utf-8").splitlines():
            with contextlib.suppress(Exception):
                sample = json.loads(line)
                if (
                    isinstance(sample, dict)
                    and isinstance(sample.get("ts"), int | float)
                    and (since is None or sample["ts"] >= since)
                ):
                    samples.append(sample)
    return samples


def append_sample(
    path: Path, sample: dict[str, Any], *, keep_days: float = DEFAULT_KEEP_DAYS
) -> int:
    """Append one sample and drop anything older than `keep_days`. Returns the count kept."""
    cutoff = sample["ts"] - keep_days * 86400
    kept = [s for s in load_samples(path) if s["ts"] >= cutoff]
    kept.append(sample)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(s, sort_keys=True) + "\n" for s in kept), encoding="utf-8")
    os.replace(tmp, path)
    return len(kept)


def drive_light(drives: dict[str, float]) -> str:
    """Worst light across drives, each judged against the limit for its kind."""
    worst = "unknown"
    for key, temp in drives.items():
        light = light_for("nvme" if key.startswith("nvme:") else "disk", temp)
        if _LIGHT_RANK[light] > _LIGHT_RANK[worst]:
            worst = light
    return worst


def latest_status(samples: list[dict[str, Any]], *, now: float | None = None) -> dict[str, Any]:
    """The newest sample reduced to labelled readings, each with a light."""
    if not samples:
        return {"has_data": False, "readings": [], "overall": "unknown", "age_seconds": None}
    last = samples[-1]
    readings = [
        {"label": "CPU", "value": last.get("cpu"), "light": light_for("cpu", last.get("cpu"))},
        {"label": "GPU", "value": last.get("gpu"), "light": light_for("gpu", last.get("gpu"))},
        {
            "label": "Board",
            "value": last.get("board"),
            "light": light_for("board", last.get("board")),
        },
        {
            "label": "Hottest drive",
            "value": last.get("drive_max"),
            "light": drive_light(last.get("drives") or {}),
        },
    ]
    overall = max((r["light"] for r in readings), key=_LIGHT_RANK.__getitem__)
    return {
        "has_data": True,
        "readings": readings,
        "overall": overall,
        "age_seconds": int((now if now is not None else time.time()) - last["ts"]),
        "load": last.get("load"),
        "fan_max": last.get("fan_max"),
    }


def build_payload(
    tachometer_root: Path, range_key: str = "24h", *, now: float | None = None
) -> dict:
    range_key = range_key if range_key in RANGES else "24h"
    now = now if now is not None else time.time()
    all_samples = load_samples(samples_path(tachometer_root))
    window = [s for s in all_samples if s["ts"] >= now - RANGES[range_key] * 3600]
    return {
        "range": range_key,
        "limits": LIMITS,
        "status": latest_status(all_samples, now=now),
        "samples": [
            {k: s.get(k) for k in ("ts", "load", "cpu", "gpu", "board", "drive_max", "fan_max")}
            for s in window
        ],
    }


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tachometer · Thermal</title>
<style>
:root{--plane:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--rule:#e6e5e1;
 --s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;
 --good:#0ca30c;--warning:#b97b00;--critical:#d03b3b;--none:#8a8983}
@media (prefers-color-scheme:dark){:root{--plane:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;
 --ink2:#c3c2b7;--rule:#32322f;--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;
 --warning:#fab219;--none:#8a8983}}
*{box-sizing:border-box}
body{margin:0;padding:20px 16px 40px;background:var(--plane);color:var(--ink);
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:14px}
main{max-width:1000px;margin:0 auto}
h1{font-size:1.25rem;margin:0 0 2px}
h2{font-size:.95rem;margin:0 0 2px}
.sub{color:var(--ink2);font-size:.8rem;margin-bottom:14px}
a{color:inherit}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px}
.chip{background:var(--surface);border:1px solid var(--rule);border-radius:10px;
 padding:8px 12px;min-width:118px}
.chip .k{color:var(--ink2);font-size:.72rem}
.chip .v{font-size:1.25rem;font-weight:600;font-variant-numeric:tabular-nums}
.chip .s{font-size:.75rem;display:flex;align-items:center;gap:5px}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block}
.green{background:var(--good)}.yellow{background:var(--warning)}
.red{background:var(--critical)}.unknown{background:var(--none)}
.card{background:var(--surface);border:1px solid var(--rule);border-radius:12px;
 padding:14px 14px 10px;margin-bottom:14px;position:relative}
.filters{display:flex;gap:6px;margin-bottom:12px}
.filters a{padding:4px 12px;border:1px solid var(--rule);border-radius:999px;
 text-decoration:none;font-size:.8rem;background:var(--surface)}
.filters a[aria-current=true]{background:var(--ink);color:var(--surface);border-color:var(--ink)}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:.78rem;color:var(--ink2);margin:6px 0 2px}
.legend i{display:inline-block;width:14px;height:2px;border-radius:1px;vertical-align:middle;margin-right:5px}
svg{display:block;width:100%;height:auto;overflow:visible}
.tick{fill:var(--ink2);font-size:11px;font-variant-numeric:tabular-nums}
.end{fill:var(--ink);font-size:11px;font-variant-numeric:tabular-nums}
.grid{stroke:var(--rule);stroke-width:1}
.tip{position:absolute;pointer-events:none;background:var(--surface);border:1px solid var(--rule);
 border-radius:8px;padding:7px 10px;font-size:.76rem;box-shadow:0 4px 14px rgba(0,0,0,.14);
 white-space:nowrap;display:none;font-variant-numeric:tabular-nums}
.tip b{font-weight:600}
.empty{color:var(--ink2);padding:28px 0;text-align:center}
table{border-collapse:collapse;width:100%;font-size:.78rem;font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:3px 8px;border-bottom:1px solid var(--rule)}
th:first-child,td:first-child{text-align:left}
summary{cursor:pointer;color:var(--ink2);font-size:.8rem}
</style></head><body><main>
<h1>Thermal history</h1>
<div class="sub"><a href="/">&larr; Dashboard</a> &middot; __AGE__ &middot;
 <a href="/api/thermal?range=__RANGE__">JSON</a></div>
<div class="chips">__CHIPS__</div>
<div class="filters" role="navigation" aria-label="Time range">__FILTERS__</div>
<div class="card"><h2>Temperature</h2>
 <div class="sub" style="margin:0">Degrees Celsius, one sample about every five minutes</div>
 <div class="legend" id="legend"></div><div id="temp"></div><div class="tip" id="tip-temp"></div></div>
<div class="card"><h2>Load per CPU thread</h2>
 <div class="sub" style="margin:0">1-minute load average divided by thread count; 1.0 is fully busy</div>
 <div id="load"></div><div class="tip" id="tip-load"></div></div>
<details class="card"><summary>Table view (__COUNT__ samples)</summary><div id="table"></div></details>
<script id="data" type="application/json">__DATA__</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent),S=D.samples;
const SERIES=[['cpu','CPU','--s1'],['gpu','GPU','--s2'],['board','Board','--s3'],['drive_max','Hottest drive','--s4']];
const NS='http://www.w3.org/2000/svg';
const el=(n,a,t)=>{const e=document.createElementNS(NS,n);for(const k in a)e.setAttribute(k,a[k]);if(t!=null)e.textContent=t;return e};
const fmtT=ts=>{const d=new Date(ts*1000);return d.toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})};
const fmtX=ts=>{const d=new Date(ts*1000);return D.range==='7d'?d.toLocaleDateString([], {weekday:'short',day:'numeric'}):d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})};
function chart(host,tipId,series,opts){
  const box=document.getElementById(host),tip=document.getElementById(tipId);
  const have=series.filter(s=>S.some(p=>p[s[0]]!=null));
  if(S.length<2||!have.length){box.innerHTML='<div class="empty">Not enough samples in this range yet.</div>';return}
  const W=960,H=opts.h,L=38,R=opts.endLabels?118:14,T=10,B=24;
  const t0=S[0].ts,t1=S[S.length-1].ts;
  let lo=Infinity,hi=-Infinity;
  S.forEach(p=>have.forEach(s=>{const v=p[s[0]];if(v!=null){lo=Math.min(lo,v);hi=Math.max(hi,v)}}));
  lo=opts.zero?0:Math.floor(lo/10)*10;hi=Math.max(opts.min||0,Math.ceil(hi/opts.step)*opts.step);
  if(hi<=lo)hi=lo+opts.step;
  const x=t=>L+(t-t0)/(t1-t0||1)*(W-L-R),y=v=>T+(1-(v-lo)/(hi-lo))*(H-T-B);
  const svg=el('svg',{viewBox:`0 0 ${W} ${H}`,role:'img','aria-label':opts.label});
  for(let v=lo;v<=hi+1e-9;v+=opts.step){
    svg.append(el('line',{class:'grid',x1:L,x2:W-R,y1:y(v),y2:y(v)}));
    svg.append(el('text',{class:'tick',x:L-6,y:y(v)+4,'text-anchor':'end'},opts.fmt(v)))}
  for(let i=0;i<=4;i++){const t=t0+(t1-t0)*i/4;
    svg.append(el('text',{class:'tick',x:x(t),y:H-6,'text-anchor':i===0?'start':i===4?'end':'middle'},fmtX(t)))}
  const css=getComputedStyle(document.documentElement);
  const ends=[];
  have.forEach(s=>{let d='',pen=false,last=null;
    S.forEach(p=>{const v=p[s[0]];if(v==null){pen=false;return}
      d+=(pen?'L':'M')+x(p.ts).toFixed(1)+' '+y(v).toFixed(1);pen=true;last=[p.ts,v]});
    svg.append(el('path',{d,fill:'none',stroke:css.getPropertyValue(s[2]),'stroke-width':2,'stroke-linejoin':'round','stroke-linecap':'round'}));
    if(last)ends.push({y:y(last[1]),text:`${s[1]} ${opts.fmt(last[1])}`})});
  if(opts.endLabels){ends.sort((a,b)=>a.y-b.y);
    ends.forEach((e,i)=>{if(i&&e.y-ends[i-1].ly<13)e.ly=ends[i-1].ly+13;else e.ly=e.y;
      svg.append(el('text',{class:'end',x:W-R+6,y:e.ly+4},e.text))})}
  const cross=el('line',{class:'grid',y1:T,y2:H-B,style:'display:none;stroke:var(--ink2)'});svg.append(cross);
  const hit=el('rect',{x:L,y:T,width:W-L-R,height:H-T-B,fill:'transparent'});svg.append(hit);
  const move=ev=>{const r=svg.getBoundingClientRect(),px=(ev.clientX-r.left)/r.width*W;
    const t=t0+(px-L)/(W-L-R)*(t1-t0);let best=S[0];
    for(const p of S)if(Math.abs(p.ts-t)<Math.abs(best.ts-t))best=p;
    cross.setAttribute('x1',x(best.ts));cross.setAttribute('x2',x(best.ts));cross.style.display='';
    tip.innerHTML='<b>'+fmtT(best.ts)+'</b><br>'+have.map(s=>`<span class="legend"><i style="background:var(${s[2]})"></i></span>${s[1]}: <b>${best[s[0]]==null?'–':opts.fmt(best[s[0]])}</b>`).join('<br>');
    tip.style.display='block';const cr=box.parentElement.getBoundingClientRect();
    let lx=ev.clientX-cr.left+14;if(lx+tip.offsetWidth>cr.width-8)lx=ev.clientX-cr.left-tip.offsetWidth-14;
    tip.style.left=lx+'px';tip.style.top=(ev.clientY-cr.top-10)+'px'};
  hit.addEventListener('pointermove',move);
  hit.addEventListener('pointerleave',()=>{tip.style.display='none';cross.style.display='none'});
  box.append(svg);return have}
const shown=chart('temp','tip-temp',SERIES,{h:300,step:10,min:60,endLabels:true,label:'Temperature over time',fmt:v=>Math.round(v)+'\\u00b0'})||[];
document.getElementById('legend').innerHTML=shown.map(s=>`<span><i style="background:var(${s[2]})"></i>${s[1]}</span>`).join('');
chart('load','tip-load',[['load','Load','--s1']],{h:150,step:0.5,min:1,zero:true,endLabels:false,label:'Load per CPU thread over time',fmt:v=>(+v).toFixed(2)});
const rows=S.slice(-200).reverse().map(p=>'<tr><td>'+fmtT(p.ts)+'</td>'+['cpu','gpu','board','drive_max'].map(k=>'<td>'+(p[k]==null?'–':Math.round(p[k]))+'</td>').join('')+'<td>'+(p.load==null?'–':p.load.toFixed(2))+'</td><td>'+(p.fan_max==null?'–':p.fan_max)+'</td></tr>').join('');
document.getElementById('table').innerHTML='<table><thead><tr><th>Time</th><th>CPU °C</th><th>GPU °C</th><th>Board °C</th><th>Hottest drive °C</th><th>Load</th><th>Fan max RPM</th></tr></thead><tbody>'+rows+'</tbody></table>';
</script></main></body></html>
"""


def _fmt_age(seconds: int | None) -> str:
    if seconds is None:
        return "no samples yet"
    if seconds < 90:
        return "latest sample just now"
    if seconds < 5400:
        return f"latest sample {seconds // 60} min ago"
    return f"latest sample {seconds // 3600} h ago"


def render_page(tachometer_root: Path, range_key: str = "24h", *, now: float | None = None) -> str:
    payload = build_payload(tachometer_root, range_key, now=now)
    status = payload["status"]
    chips = []
    for reading in status["readings"]:
        value = "–" if reading["value"] is None else f"{reading['value']:.0f}°C"
        light = reading["light"]
        chips.append(
            f'<div class="chip"><div class="k">{html.escape(reading["label"])}</div>'
            f'<div class="v">{value}</div><div class="s"><span class="dot {light}"></span>'
            f"{_LIGHT_ICON[light]} {_LIGHT_WORD[light]}</div></div>"
        )
    if not chips:
        chips.append(
            '<div class="chip"><div class="k">Host</div><div class="v">–</div>'
            '<div class="s">No samples yet</div></div>'
        )
    filters = "".join(
        f'<a href="/thermal?range={key}" aria-current="{"true" if key == payload["range"] else "false"}">'
        f"{key}</a>"
        for key in RANGES
    )
    data = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    return (
        _PAGE.replace("__CHIPS__", "".join(chips))
        .replace("__FILTERS__", filters)
        .replace("__AGE__", _fmt_age(status["age_seconds"]))
        .replace("__RANGE__", payload["range"])
        .replace("__COUNT__", str(len(payload["samples"])))
        .replace("__DATA__", data)
    )
