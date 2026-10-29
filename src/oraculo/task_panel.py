"""Loopback project task view using the daemon's existing read-only API only."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import PurePosixPath

from oraculo.presentation import plain, task_tracking


def project_binding(task):
    resolved = (task.get("project") or {}).get("project_id")
    if resolved:
        return resolved
    # Unresolved requests remain visible to their host-bound origin project.
    # Never infer a destination from paths, titles or a requested project name.
    return (task.get("origin") or {}).get("project_id")


async def project_tasks(config, project_id):
    # Same official socket request as `task list`, with no offline fallback.
    from oraculo.cli import make_envelope, request

    result = await request(
        config, make_envelope("managed.read", "*", dict(action="list", payload={}))
    )
    if result.get("ok") is not True:
        raise ValueError("task_panel_service_unavailable")
    data = result["data"]
    if data.get("source") != "service" or data.get("protocol_version") != 1:
        raise ValueError("task_panel_service_unproven")
    rows = []
    title = project_id
    for task in data["tasks"]:
        if project_binding(task) != project_id:
            continue
        path = (task.get("project") or {}).get("project_path")
        if path:
            title = plain(PurePosixPath(path).name)
        tracking = task_tracking(task)
        rows.append(
            dict(
                id=task["id"],
                title=tracking["title"],
                tracking=tracking,
                snapshot_revision=task.get("snapshot_revision"),
            )
        )
    return dict(
        project_id=project_id,
        project_title=title,
        source="service",
        fetched_at=datetime.now(timezone.utc).isoformat(),
        tasks=rows,
    )


PAGE = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tarefas · Oráculo</title><style>
:root{color-scheme:light;--ink:#172e34;--accent:#12675f}*{box-sizing:border-box}
body{background:#f2f5ef;color:var(--ink);font:16px/1.55 system-ui;margin:0}
main{max-width:1100px;margin:auto;padding:32px}h1{font-size:clamp(28px,5vw,44px)}
a{color:#095f79}button,select,input{font:inherit;padding:10px;border:1px solid #8aa4a3;border-radius:6px;background:white;color:var(--ink)}
button{cursor:pointer}button:focus-visible,select:focus-visible,input:focus-visible,a:focus-visible{outline:3px solid #bb4a1b;outline-offset:3px}
.controls{display:flex;gap:12px;flex-wrap:wrap;align-items:end}label{display:grid;gap:5px}
#tasks{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,320px),1fr));gap:16px;margin-top:24px}
article{background:white;border:1px solid #cbd9d5;border-radius:12px;padding:22px;overflow-wrap:anywhere}
article h2{font-size:22px;margin:12px 0}article p{white-space:pre-wrap}.state{font-weight:750;color:var(--accent)}
.date{font-size:14px;color:#4e696d}#health{padding:14px;background:#dde9df}#health.failed{background:#ffe5d5;color:#772b0b}
</style></head><body><main><p>ORÁCULO / TAREFAS DO PROJETO</p><h1 id="project">Tarefas</h1>
<p id="health" role="status" aria-live="polite">Consultando serviço…</p>
<p>Observações do daemon; sem ETA. Um run terminado ainda precisa de validação da tarefa. Publicação e implantação são etapas próprias.</p>
<div class="controls"><button id="refresh" type="button">Atualizar agora</button>
<label>Estado<select id="state"><option value="">Todos</option><option value="waiting">Aguardando / fila</option><option value="running">Executando</option><option value="blocked">Bloqueada / parada</option><option value="completed">Concluída</option></select></label>
<label>Buscar tarefa<input id="query" type="search"></label></div><p id="count" aria-live="polite"></p><div id="tasks"></div>
<p class="date">Atualização automática a cada 15 segundos enquanto a consulta responde. A data de cada tarefa é a última mudança observada, não a hora desta consulta. Dados privados; acesso local.</p>
</main><script>
const byId=id=>document.getElementById(id);let tasks=[];let busy=false;let lastSuccess=null;
function node(tag,text,cls){const n=document.createElement(tag);n.textContent=text;if(cls)n.className=cls;return n;}
function render(){const root=byId('tasks');root.replaceChildren();let count=0;
for(const t of tasks){const s=t.tracking;if(byId('state').value&&s.state!==byId('state').value)continue;
if(!(t.title+' '+s.activity+' '+(s.blocked_reason||'')).toLocaleLowerCase('pt-BR').includes(byId('query').value.toLocaleLowerCase('pt-BR')))continue;
count++;const card=node('article','');card.append(node('div',s.label,'state'),node('h2',t.title),node('p',s.phase+' · '+s.activity));
if(s.blocker)card.append(node('p',s.blocker+(s.blocked_reason?' ('+s.blocked_reason+')':'')));
card.append(node('p',s.result),node('p',s.next_action),node('p','Última mudança observada: '+s.updated_at,'date'),node('p','Última observação de execução: '+s.observed_status+' / '+s.observed_work_state,'date'));
for(const [key,label] of [['origin_url','Conversa de origem'],['child_url','Conversa do executor']]){if(s[key]){const a=node('a',label);a.href=s[key];card.append(a,node('br',''));}}
root.append(card);}byId('count').textContent=count+' tarefas';}
async function refresh(){if(busy)return;busy=true;byId('refresh').disabled=true;
try{const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),8000);let r;try{r=await fetch('/api/tasks',{cache:'no-store',signal:controller.signal});}finally{clearTimeout(timer);}
if(!r.ok)throw Error('unavailable');const data=await r.json();tasks=data.tasks;lastSuccess=data.fetched_at;
byId('project').textContent=data.project_title;byId('health').className='';byId('health').textContent='Consulta ao serviço: '+lastSuccess+' · estados conforme última observação registrada.';render();}
catch(_){tasks=[];render();byId('health').className='failed';byId('health').textContent='Serviço indisponível. Estado atual desconhecido. Última consulta bem-sucedida: '+(lastSuccess||'nenhuma')+'. Tente Atualizar agora.';}
finally{busy=false;byId('refresh').disabled=false;}}
byId('refresh').addEventListener('click',refresh);byId('state').addEventListener('input',render);byId('query').addEventListener('input',render);refresh();setInterval(refresh,15000);
</script></body></html>"""


def make_server(config, project_id, port=8765):
    if not isinstance(project_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]{1,200}", project_id
    ):
        raise ValueError("task_panel_project_required")
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("task_panel_invalid_port")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Avoid recording private request metadata.

        def respond(self, status, content, mime):
            body = content.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", mime + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            # Fix project at process launch; no query parameters or file serving.
            expected = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != expected:
                self.respond(403, "Forbidden", "text/plain")
            elif self.headers.get("Sec-Fetch-Site") == "cross-site":
                self.respond(403, "Forbidden", "text/plain")
            elif self.path == "/":
                self.respond(200, PAGE, "text/html")
            elif self.path == "/api/tasks":
                try:
                    result = asyncio.run(project_tasks(config, project_id))
                except (OSError, TimeoutError, ValueError, KeyError, TypeError):
                    self.respond(
                        503,
                        '{"error":"service_unavailable","tasks":[]}',
                        "application/json",
                    )
                else:
                    self.respond(
                        200, json.dumps(result, ensure_ascii=True), "application/json"
                    )
            else:
                self.respond(404, "Not found", "text/plain")

    return HTTPServer(("127.0.0.1", port), Handler)


def serve(config, project_id, port):
    from oraculo.stdio import print_json

    with make_server(config, project_id, port) as server:
        print_json(
            dict(
                state="serving",
                project_id=project_id,
                url=f"http://127.0.0.1:{server.server_port}/",
                source="managed.read",
                read_only=True,
            )
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
