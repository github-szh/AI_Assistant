"""Built-in monitoring dashboard — served from the app itself.

Provides:
  GET /dashboard        → HTML dashboard with system & LLM stats
  GET /monitoring       → JSON snapshot for AJAX refresh
"""

import logging
import time

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from src.api.permissions import require_permission

from src.monitoring.metrics import (
    get_gpu_metrics,
    get_http_counts,
    get_latency_percentiles,
    get_llm_history,
    get_redis_metrics,
    db_pool_min,
    db_pool_max,
    db_pool_available,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/alerts/ack")
async def ack_alert(body: dict, user: dict = Depends(require_permission("quality:admin"))):
    from src.monitoring.storage import acknowledge_alert
    if body.get("all"):
        from src.monitoring.storage import get_recent_alerts
        for a in get_recent_alerts(100):
            if not a.get("acknowledged"):
                acknowledge_alert(a["id"])
    elif body.get("id"):
        acknowledge_alert(body["id"])
    return {"ok": True}


@router.get("/api/phoenix/status")
async def phoenix_status(user: dict = Depends(require_permission("quality:view"))):
    """Check if Arize Phoenix is enabled and running."""
    from src.config import settings
    if not settings.phoenix_enabled:
        return {"running": False}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=2) as client:
            r = await client.get("http://localhost:6006/health")
            return {"running": r.status_code < 500}
    except Exception:
        return {"running": False}


@router.get("/api/phoenix/info")
async def phoenix_info(user: dict = Depends(require_permission("quality:view"))):
    """Return Phoenix connection details for the frontend."""
    from src.config import settings
    base = "http://localhost:6006"
    return {
        "enabled": settings.phoenix_enabled,
        "url": base,
        "running": False,  # filled by status check
    }


@router.get("/monitoring")
async def monitoring_json(user: dict = Depends(require_permission("quality:view"))):
    now = time.time()

    # System resources (read directly via psutil, bypass Prometheus Gauge corruption)
    cpu = 0.0
    mem_used = 0
    mem_total = 0
    disks: list[dict] = []
    disk_total_map: dict[str, int] = {}
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()
        mem_used = mem.used
        mem_total = mem.total
        for part in psutil.disk_partitions():
            if part.fstype:
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                    disks.append({"mount": part.mountpoint, "used": usage.used})
                    disk_total_map[part.mountpoint] = usage.total
                except PermissionError:
                    continue
    except Exception:
        pass

    # HTTP counts from in-memory dict (avoids Prometheus counter corruption on reload)
    http_raw = get_http_counts()
    http_by_endpoint: dict[str, int] = {}
    for key, count in http_raw.items():
        parts = key.split(":")
        ep = parts[0] if len(parts) > 1 else key
        status = parts[1] if len(parts) > 1 else ""
        http_by_endpoint[f"{ep}:{status}"] = count

    # Alerts
    alerts: list[dict] = []
    try:
        from src.monitoring.storage import get_recent_alerts
        alerts = get_recent_alerts(20)
    except Exception:
        pass

    # RAG quality from SQLite
    rag_quality: dict = {}
    try:
        from src.monitoring.storage import get_rag_query_stats
        rag_quality = get_rag_query_stats(7)
    except Exception:
        pass

    # LLM data from SQLite (persistent, survives reload)
    llm_tokens: dict[str, int] = {}
    llm_costs: dict[str, float] = {}
    llm_call_count = 0
    recent_calls: list[dict] = []
    try:
        from src.monitoring.storage import get_llm_call_count, get_llm_calls, get_cost_summary
        llm_call_count = get_llm_call_count()

        for row in get_cost_summary(30):
            day = row["day"]
            provider = row["provider"]
            model = row["model"]
            token_key = f"{provider}/{model}"
            cost_key = f"{provider}/{model}"
            if token_key not in llm_tokens:
                llm_tokens[token_key] = 0
            llm_tokens[token_key] += (row["total_prompt"] or 0) + (row["total_completion"] or 0)
            if cost_key not in llm_costs:
                llm_costs[cost_key] = 0.0
            llm_costs[cost_key] += row["total_cost"] or 0.0

        for row in get_llm_calls(50):
            recent_calls.append({
                "t": row["ts"],
                "provider": row["provider"],
                "model": row["model"],
                "prompt": row["prompt_tokens"],
                "completion": row["completion_tokens"],
                "cost": f"{row['cost']:.6f}",
                "elapsed": f"{row['elapsed']:.2f}",
            })
    except Exception:
        # Fall back to in-memory buffer
        for rec in get_llm_history()[:50]:
            recent_calls.append({
                "t": rec.timestamp,
                "provider": rec.provider,
                "model": rec.model,
                "prompt": rec.prompt_tokens,
                "completion": rec.completion_tokens,
                "cost": f"{rec.cost:.6f}",
                "elapsed": f"{rec.elapsed:.2f}",
            })

    return {
        "cpu": cpu,
        "mem_used": mem_used,
        "mem_total": mem_total,
        "disks": disks,
        "disk_totals": disk_total_map,
        "db_pool": {
            "min": next((s.value for s in db_pool_min.collect()[0].samples), 0),
            "max": next((s.value for s in db_pool_max.collect()[0].samples), 0),
            "avail": next((s.value for s in db_pool_available.collect()[0].samples), 0),
        },
        "gpu": get_gpu_metrics(),
        "redis": get_redis_metrics(),
        "latency": get_latency_percentiles(),
        "rag": rag_quality,
        "alerts": alerts,
        "http": http_by_endpoint,
        "llm_tokens": llm_tokens,
        "llm_costs": llm_costs,
        "llm_call_count": llm_call_count,
        "recent": recent_calls,
        "now": now,
    }

# ── HTML dashboard ──

_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Assistant 监控看板</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#0f172a;color:#e2e8f0;padding:20px}
h1{font-size:1.5rem;margin-bottom:16px;color:#38bdf8}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:24px}
.card{background:#1e293b;border-radius:10px;padding:16px;text-align:center}
.card .label{font-size:.75rem;color:#94a3b8;text-transform:uppercase}
.card .value{font-size:1.8rem;font-weight:700;margin-top:4px}
.card .value.green{color:#22c55e}
.card .value.blue{color:#38bdf8}
.card .value.yellow{color:#eab308}
.card .value.red{color:#ef4444}
.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(400px,1fr));gap:16px;margin-bottom:24px}
.chart-box{background:#1e293b;border-radius:10px;padding:16px}
.chart-box h3{font-size:.9rem;margin-bottom:8px;color:#94a3b8}
table{width:100%;border-collapse:collapse;font-size:.8rem}
th{text-align:left;color:#94a3b8;padding:6px 8px;border-bottom:1px solid #334155}
td{padding:6px 8px;border-bottom:1px solid #1e293b}
#recent-table{max-height:400px;overflow-y:auto}
.llm-table-wrap{background:#1e293b;border-radius:10px;padding:16px;margin-bottom:24px}
.llm-table-wrap h3{font-size:.9rem;margin-bottom:8px;color:#94a3b8}
</style>
</head>
<body>
<h1>AI Assistant 监控看板</h1>

<div class="stats" id="stat-cards">
  <div class="card"><div class="label">CPU</div><div class="value blue" id="cpu">-</div></div>
  <div class="card"><div class="label">内存</div><div class="value blue" id="mem">-</div></div>
  <div class="card"><div class="label">磁盘</div><div class="value blue" id="disk">-</div></div>
  <div class="card"><div class="label">DB 连接池</div><div class="value yellow" id="dbpool">-</div></div>
  <div class="card"><div class="label">LLM 总调用</div><div class="value green" id="llm-count">0</div></div>
  <div class="card"><div class="label">LLM 总成本</div><div class="value red" id="llm-cost">$0.00</div></div>
</div>

<div class="charts">
  <div class="chart-box"><h3>HTTP 请求数 (按端点)</h3><canvas id="chart-http"></canvas></div>
  <div class="chart-box"><h3>LLM Tokens (按模型)</h3><canvas id="chart-tokens"></canvas></div>
  <div class="chart-box"><h3>LLM 成本 (按模型)</h3><canvas id="chart-cost"></canvas></div>
  <div class="chart-box"><h3>磁盘使用</h3><canvas id="chart-disk"></canvas></div>
</div>

<div class="llm-table-wrap">
  <h3>最近 LLM 调用</h3>
  <div id="recent-table"><table><thead><tr>
    <th>时间</th><th>Provider</th><th>模型</th><th>Prompt</th><th>Completion</th><th>耗时</th><th>成本</th>
  </tr></thead><tbody id="recent-body"></tbody></table></div>
</div>

<script>
let httpChart, tokenChart, costChart, diskChart;

function fmt(s) { return s.toLocaleString(); }
function fmtBytes(b) {
  if (b===0) return '0 B';
  const u=['B','KB','MB','GB','TB']; let i=0;
  let v=b; while(v>=1024&&i<u.length-1){v/=1024;i++}
  return v.toFixed(1)+' '+u[i];
}

async function refresh() {
  try {
    const r=await fetch('/monitoring');
    const d=await r.json();

    // Stat cards
    document.getElementById('cpu').textContent=d.cpu.toFixed(1)+'%';
    const memPct=(d.mem_total>0)?(d.mem_used/d.mem_total*100).toFixed(1):'-';
    document.getElementById('mem').textContent=`${fmtBytes(d.mem_used)} / ${fmtBytes(d.mem_total)} (${memPct}%)`;
    let diskUsed=0,diskTotal=0;
    (d.disks||[]).forEach(dk=>{diskUsed+=dk.used});
    Object.values(d.disk_totals||{}).forEach(v=>diskTotal+=v);
    document.getElementById('disk').textContent=diskTotal>0?`${fmtBytes(diskUsed)} / ${fmtBytes(diskTotal)}`:'-';
    document.getElementById('dbpool').textContent=d.db_pool.avail+' / '+d.db_pool.max;

    const calls=d.recent||[];
    document.getElementById('llm-count').textContent=calls.length;
    let totalCost=0;
    Object.values(d.llm_costs||{}).forEach(v=>totalCost+=v);
    document.getElementById('llm-cost').textContent='$'+totalCost.toFixed(4);

    // HTTP chart
    const eps=Object.keys(d.http||{}).filter(k=>!k.includes('GET /api/monitoring')&&!k.includes('GET /dashboard'));
    const httpLabels=eps.map(k=>k.split(':')[0]);
    const httpVals=eps.map(k=>d.http[k]);
    if(httpChart)httpChart.destroy();
    httpChart=new Chart(document.getElementById('chart-http'),{
      type:'bar',
      data:{labels:httpLabels,datasets:[{label:'请求数',data:httpVals,backgroundColor:'#38bdf8'}]},
      options:{responsive:true,plugins:{legend:{display:false}},scales:{y:{beginAtZero:true}}}
    });

    // Token chart
    const tkeys=Object.keys(d.llm_tokens||{}).filter(k=>k.includes('/completion')||k.includes('/prompt'));
    const tLabels=[...new Set(tkeys.map(k=>{const p=k.split('/');return p[0]+'/'+p[1]}))];
    const promptVals=tLabels.map(l=>d.llm_tokens[l+'/prompt']||0);
    const compVals=tLabels.map(l=>d.llm_tokens[l+'/completion']||0);
    if(tokenChart)tokenChart.destroy();
    tokenChart=new Chart(document.getElementById('chart-tokens'),{
      type:'bar',
      data:{labels:tLabels,datasets:[
        {label:'Prompt',data:promptVals,backgroundColor:'#22c55e'},
        {label:'Completion',data:compVals,backgroundColor:'#38bdf8'},
      ]},
      options:{responsive:true,scales:{y:{beginAtZero:true}}}
    });

    // Cost chart
    const ckeys=Object.keys(d.llm_costs||{});
    const cLabels=ckeys.map(k=>k.split('/')[0]+'/'+k.split('/')[1]);
    const cVals=ckeys.map(k=>d.llm_costs[k]);
    if(costChart)costChart.destroy();
    costChart=new Chart(document.getElementById('chart-cost'),{
      type:'doughnut',
      data:{labels:cLabels,datasets:[{data:cVals,backgroundColor:['#22c55e','#38bdf8','#eab308','#ef4444','#a855f7']}]},
      options:{responsive:true,plugins:{legend:{position:'bottom'}}}
    });

    // Disk chart
    const dLabels=(d.disks||[]).map(dk=>dk.mount);
    const dUsed=(d.disks||[]).map(dk=>dk.used);
    const dTotal=dLabels.map(l=>d.disk_totals[l]||0);
    if(diskChart)diskChart.destroy();
    diskChart=new Chart(document.getElementById('chart-disk'),{
      type:'bar',
      data:{labels:dLabels,datasets:[
        {label:'已用',data:dUsed,backgroundColor:'#ef4444'},
        {label:'总量',data:dTotal,backgroundColor:'#334155'},
      ]},
      options:{responsive:true,scales:{y:{beginAtZero:true}}}
    });

    // Recent table
    const tbody=document.getElementById('recent-body');
    tbody.innerHTML=calls.map(c=>{
      const dt=new Date(c.t*1000).toLocaleTimeString();
      return `<tr><td>${dt}</td><td>${c.provider}</td><td>${c.model}</td><td>${c.prompt}</td><td>${c.completion}</td><td>${c.elapsed}s</td><td>$${c.cost}</td></tr>`;
    }).join('');
  } catch(e){
    console.warn('refresh error',e);
  }
}
refresh();
setInterval(refresh,5000);
</script>
</body>
</html>
"""


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(user: dict = Depends(require_permission("quality:view"))):
    return _DASHBOARD_HTML
