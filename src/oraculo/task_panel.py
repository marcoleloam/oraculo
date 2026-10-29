"""Loopback task views using the daemon's existing read-only APIs only."""

from __future__ import annotations

import asyncio
import json
import math
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


async def read_tasks(config):
    # Same official socket request as `task list`, with no offline fallback.
    from oraculo.cli import make_envelope, request

    result = await request(
        config, make_envelope("managed.read", "*", dict(action="list", payload={}))
    )
    if result.get("ok") is not True:
        raise ValueError("task_panel_service_unavailable")
    data = result["data"]
    if not isinstance(data, dict):
        raise ValueError("task_panel_service_unproven")
    if data.get("source") != "service" or data.get("protocol_version") != 1:
        raise ValueError("task_panel_service_unproven")
    if not isinstance(data.get("tasks"), list) or not all(
        isinstance(task, dict) for task in data["tasks"]
    ):
        raise ValueError("task_panel_tasks_invalid")
    return data["tasks"]


def task_row(task, project_id, title):
    tracking = task_tracking(task)
    observed_at = task.get("observed_at")
    if (
        type(observed_at) not in {int, float}
        or not math.isfinite(observed_at)
        or observed_at < 0
    ):
        observed_at = None
    return dict(
        id=task["id"],
        short_id=task["id"].removeprefix("task-")[:10],
        project_id=project_id,
        project_title=title,
        title=tracking["title"],
        tracking=tracking,
        history=tracking["state"] == "completed"
        or task.get("work_status") in {"failed", "stopped", "cancelled"}
        or task.get("phase") == "superseded",
        observed_at=observed_at,
        snapshot_revision=task.get("snapshot_revision"),
    )


async def project_tasks(config, project_id):
    tasks = await read_tasks(config)
    rows = []
    title = project_id
    for task in tasks:
        if project_binding(task) != project_id:
            continue
        path = (task.get("project") or {}).get("project_path")
        if path:
            title = plain(PurePosixPath(path).name)
        rows.append(task_row(task, project_id, title))
    for row in rows:
        row["project_title"] = title
    return dict(
        project_id=project_id,
        project_title=title,
        source="service",
        fetched_at=datetime.now(timezone.utc).isoformat(),
        tasks=rows,
    )


async def project_catalog(config):
    from oraculo.cli import make_envelope, request

    # Protocol 1 has no project-only read endpoint. Do not retain the other
    # private status fields or forward the status response to HTTP.
    result = await request(config, make_envelope("status.get", "*", {}))
    if result.get("ok") is not True:
        raise ValueError("task_panel_catalog_unavailable")
    data = result["data"]
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("runtime"), dict)
        or data["runtime"].get("protocol_version") != 1
    ):
        raise ValueError("task_panel_catalog_unproven")
    projects = data["projects"]
    del result, data
    if not isinstance(projects, list):
        raise ValueError("task_panel_catalog_invalid")
    catalog = {}
    for project in projects:
        if not isinstance(project, dict):
            raise ValueError("task_panel_catalog_invalid")
        if project["status"] != "registered":
            continue
        identifier, path = project["id"], project["path"]
        if not isinstance(identifier, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,200}", identifier
        ):
            raise ValueError("task_panel_catalog_invalid")
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("task_panel_catalog_invalid")
        if not config.project_in_scope(path):
            continue
        if identifier in catalog:
            raise ValueError("task_panel_catalog_duplicate")
        catalog[identifier] = dict(
            id=identifier, title=plain(PurePosixPath(path).name) or identifier
        )
    return catalog


async def all_project_tasks(config):
    catalog = await project_catalog(config)
    tasks = await read_tasks(config)
    rows = []
    for task in tasks:
        # A resolved destination always wins, even if it is no longer allowed.
        # Origin is a fallback only for an unresolved task in this catalog.
        identifier = project_binding(task)
        if identifier in catalog:
            rows.append(task_row(task, identifier, catalog[identifier]["title"]))
    return dict(
        mode="all",
        projects=sorted(
            catalog.values(), key=lambda p: (p["title"].casefold(), p["id"])
        ),
        source="service",
        fetched_at=datetime.now(timezone.utc).isoformat(),
        tasks=rows,
    )


PAGE = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Central de tarefas · Oráculo</title>
<style>
:root{color-scheme:dark;--bg:#151719;--surface:#1d2023;--raised:#24282c;--line:#373b40;--ink:#f0eee9;--muted:#adb1b5;--gold:#dfbd7b;--running:#99c9fa;--waiting:#e5c581;--blocked:#ffb0a6;--completed:#a0d5ba}

*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
button,input,select{font:inherit;color:inherit}
button,select,input{border:1px solid var(--line);background:var(--surface);border-radius:7px}
button,select{cursor:pointer}
button{padding:9px 13px}
button:hover{background:var(--raised)}
button:disabled{cursor:wait;opacity:.6}
a{color:var(--gold);text-underline-offset:4px}
button:focus-visible,input:focus-visible,select:focus-visible,a:focus-visible,summary:focus-visible{outline:2px solid var(--gold);outline-offset:4px}
button[aria-pressed=true]{border-color:var(--gold);background:#353025;color:#f4d69e}
[hidden]{display:none!important}
.skip{position:absolute;top:-70px;left:20px;z-index:2;padding:12px;background:var(--surface)}
.skip:focus{top:12px}

.shell{max-width:1420px;margin:auto;padding:0 40px}
.topbar{border-bottom:1px solid var(--line)}
.topbar .shell{display:flex;align-items:center;justify-content:space-between;min-height:64px;gap:20px}
.brand{letter-spacing:.19em;font-size:15px;font-weight:750;color:var(--gold)}
.private{color:var(--muted);font-size:12px}
.private::before{content:"";display:inline-block;width:6px;height:6px;background:var(--gold);border-radius:50%;margin-right:8px}
.scope{display:flex;align-items:center;gap:12px;margin:22px 0 20px;color:var(--muted);font-size:12px;flex-wrap:wrap}
.project-pill{display:inline-flex;align-items:baseline;gap:10px;border:1px solid var(--line);border-radius:7px;padding:8px 12px;background:var(--surface);max-width:100%;color:var(--ink)}
.project-pill strong{min-width:0;font-size:14px;overflow-wrap:anywhere}
.project-pill span{font-size:11px;color:var(--muted);overflow-wrap:anywhere}
.intro{display:flex;justify-content:space-between;align-items:end;gap:20px;margin-bottom:22px}
.eyebrow{color:var(--gold);font-size:11px;letter-spacing:.12em;font-weight:700;text-transform:uppercase;margin:0 0 6px}
h1{font-size:clamp(28px,4vw,34px);line-height:1.15;letter-spacing:-.035em;margin:0 0 10px;font-weight:650}
.intro p:last-child{color:var(--muted);margin:0;max-width:620px}
.project-navigation{display:flex;flex-wrap:wrap;align-items:end;gap:10px 16px;margin:0 0 20px}
.project-navigation label{flex:1;min-width:0;max-width:740px}
.project-navigation select{width:100%;min-width:0;text-overflow:ellipsis}
.project-navigation button{font-size:12px;min-height:39px;flex-shrink:0}
.project-navigation p{flex-basis:100%;margin:0;color:var(--muted);font-size:11px}
.connection{display:flex;align-items:center;gap:15px;justify-content:space-between;border-block:1px solid var(--line);padding:12px 0;margin-bottom:20px;min-height:56px}
.connection p{margin:0;color:var(--muted);font-size:12px;overflow-wrap:anywhere}
.connection.failed p{color:var(--blocked)}
.connection button{font-size:12px;flex-shrink:0}
.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.metric{text-align:left;padding:12px 18px;border-radius:9px;position:relative}
.metric .metric-top{display:flex;align-items:center;justify-content:space-between;gap:8px}
.metric strong{display:block;font-size:27px;line-height:1.2;margin:8px 0 4px;font-weight:600;font-variant-numeric:tabular-nums}
.metric small{color:var(--muted);font-size:11px}
.dot{width:7px;height:7px;border-radius:50%;background:currentColor;display:inline-block;flex-shrink:0}
.running{color:var(--running)}
.waiting{color:var(--waiting)}
.blocked{color:var(--blocked)}
.completed{color:var(--completed)}
.metric .metric-top{color:var(--ink)}
.metric-note{font-size:11px;color:var(--muted);margin:10px 0 24px;overflow-wrap:anywhere}
.toolbar{display:flex;align-items:end;gap:18px;justify-content:space-between;margin:20px 0 16px}
.segments{display:flex;flex-wrap:wrap;gap:4px}
.segments button{font-size:12px;padding:8px 12px;background:transparent}
.segments span{color:var(--muted);margin-left:7px;font-variant-numeric:tabular-nums}
.filters{display:flex;align-items:end;gap:12px;flex:1;justify-content:flex-end}
label{display:grid;gap:6px;font-size:11px;color:var(--muted)}
input,select{padding:9px 11px;min-height:39px}
input{width:100%;min-width:0}
select{max-width:100%}
.search{flex:1;max-width:330px;min-width:180px}
.list-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:22px 0 12px;min-height:30px}
.list-heading h2{margin:0;font-size:17px;font-weight:600}
.list-heading p{margin:0;color:var(--muted);font-size:12px}
.list-heading button{font-size:11px;padding:5px 9px}
.columns,.task-summary{display:grid;grid-template-columns:minmax(220px,1.5fr) minmax(180px,1.15fr) 165px 135px;gap:22px;align-items:center}
.columns{padding:10px 20px;color:var(--muted);font-size:10px;letter-spacing:.09em;text-transform:uppercase}
.project-group-heading{display:flex;align-items:baseline;flex-wrap:wrap;gap:6px 14px;margin:22px 8px 12px;font-size:13px;overflow-wrap:anywhere}
.project-group-heading span{font-size:11px;color:var(--muted);font-weight:400}
.task{border-top:1px solid var(--line)}
.task:last-child{border-bottom:1px solid var(--line)}
.task[open]{background:var(--surface);border:1px solid #59503f;border-radius:8px;margin:6px 0}
.task-summary{list-style:none;padding:17px 20px;cursor:pointer;min-height:92px}
.task-summary::-webkit-details-marker{display:none}
.task-summary:hover{background:#ffffff04}
.identity{min-width:0}
.identity-line{display:flex;gap:9px;font-size:11px;color:var(--muted);align-items:baseline;margin-bottom:5px;flex-wrap:wrap}
.project-name{color:#d4c4a6}
.project-name{overflow-wrap:anywhere;min-width:0}
.short-id{font:11px ui-monospace,SFMono-Regular,Consolas,monospace}
.task-title{font-size:14px;font-weight:600;line-height:1.45;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere}
.activity{color:#c4c6c8;font-size:12px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere}
.status{font-size:11px;display:flex;align-items:center;gap:7px}
.status-label{color:var(--ink)}
.changed{overflow-wrap:anywhere;min-width:0}
.change{display:flex;align-items:center;justify-content:space-between;gap:8px;color:var(--muted);font-size:11px}
.chevron{font-size:17px;color:var(--gold)}
.task[open] .chevron{transform:rotate(90deg)}
.detail{padding:4px 20px 22px}
.detail-head{display:flex;justify-content:space-between;align-items:start;gap:14px;border-top:1px solid var(--line);padding-top:18px}
.detail-head h3{font-size:17px;line-height:1.45;margin:0;overflow-wrap:anywhere}
.close{font-size:11px;flex-shrink:0}
.detail-grid{display:grid;grid-template-columns:1.15fr 1fr;gap:20px 40px;margin-top:20px}
.detail h4{font-size:10px;text-transform:uppercase;letter-spacing:.09em;color:var(--muted);margin:0 0 7px}
.detail p{font-size:13px;margin:0;white-space:pre-wrap;overflow-wrap:anywhere}
.next{border-left:2px solid var(--gold);padding-left:15px}
.detail-blocker{border-left:2px solid var(--blocked);padding-left:15px}
.detail footer{border-top:1px solid var(--line);margin-top:22px;padding-top:16px}
.links{display:flex;gap:20px;flex-wrap:wrap;font-size:12px;margin:0 0 18px}
.metadata{margin-top:16px}
.metadata summary{cursor:pointer;color:var(--muted);font-size:12px}
.metadata dl{display:grid;grid-template-columns:160px minmax(0,1fr);font-size:11px;gap:8px 18px;margin:16px 0 0}
.metadata dt{color:var(--muted)}
.metadata dd{margin:0;overflow-wrap:anywhere;white-space:pre-wrap}
.empty{border:1px dashed var(--line);border-radius:9px;padding:48px 20px;text-align:center;margin-top:12px}
.empty h3{font-size:18px;margin:0 0 8px}
.empty p{color:var(--muted);margin:0 auto;max-width:450px}
.empty button{margin-top:18px}
.footnote{font-size:11px;color:var(--muted);border-top:1px solid var(--line);margin:32px 0 0;padding:18px 0 30px;max-width:850px;line-height:1.7}

@media(min-width:1400px){.shell{padding-inline:52px}
}

@media(max-width:1000px){.shell{padding-inline:24px}
.toolbar{align-items:stretch;flex-direction:column;gap:16px}
.filters{justify-content:space-between}
.search{max-width:none}
.columns,.task-summary{grid-template-columns:minmax(170px,1.4fr) minmax(140px,1fr) 145px;gap:16px}
.columns span:last-child{display:none}
.change{grid-column:1/-1;justify-content:flex-end;margin-top:-8px}
.task-summary{row-gap:8px}
.change .changed{display:none}
.change .chevron{font-size:13px}
.task-title{font-size:14px}
}

@media(max-width:600px){.shell{padding-inline:18px}
.topbar .shell{min-height:62px}
.brand{font-size:13px}
.private{font-size:10px}
.scope{margin:16px 0 18px;gap:8px}
.scope>span:last-child{display:none}
.project-navigation label{flex-basis:100%}
.project-pill{flex-wrap:wrap;gap:4px 10px}
.intro{margin-bottom:20px}
.intro p:last-child{font-size:13px}
h1{font-size:30px}
.connection{align-items:start;gap:12px}
.connection button{padding:8px 10px;font-size:11px}
.metrics{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
.metric{padding:12px;display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;align-items:center;min-height:60px}
.metric .metric-top{font-size:11px}
.metric strong{font-size:24px;margin:0}
.metric small{display:none}
.metric-note{margin-bottom:20px}
.filters{display:grid;grid-template-columns:minmax(0,1fr) 1fr;gap:12px}
.search{grid-column:1/-1;min-width:0}
.filters label{min-width:0}
.filters select{width:100%;min-width:0;font-size:11px}
.segments{gap:4px}
.segments button{padding:7px 10px}
.list-heading{align-items:start}
.list-heading p{text-align:right}
.columns{display:none}
.task-summary{grid-template-columns:minmax(0,1fr) auto;padding:16px 8px;gap:10px 12px}
.identity{grid-column:1/-1}
.task-title{-webkit-line-clamp:3}
.activity{grid-column:1/-1;grid-row:3;font-size:12px}
.status{grid-column:1;grid-row:2}
.change{grid-column:2;grid-row:2;margin:0;font-size:10px}
.change .changed{display:inline}
.detail{padding:0 14px 18px}
.detail-head{padding-top:16px}
.detail-head h3{font-size:15px}
.close{padding:7px}
.detail-grid{grid-template-columns:minmax(0,1fr);gap:20px}
.metadata dl{grid-template-columns:minmax(0,1fr);gap:4px}
.metadata dd{margin-bottom:10px}
.list-heading h2{font-size:16px}
.footnote{margin-top:24px}
.empty{padding:32px 16px}
}

</style>
</head>
<body data-mode="project">
<a class="skip" href="#workspace">Ir para tarefas</a>
<header class="topbar"><div class="shell"><span class="brand">ORÁCULO</span><span class="private">Central local · somente leitura</span></div></header>
<main class="shell">
<nav class="scope" aria-label="Projeto selecionado"><span>PROJETO</span><div class="project-pill"><strong id="project">Projeto ainda não consultado</strong><span id="project-id"></span></div><span id="scope-note">Escopo fixo nesta central</span></nav>
<div class="intro"><div><p class="eyebrow">Central de operações</p><h1>Central de tarefas</h1><p>O trabalho em andamento, os impedimentos e a próxima ação.</p></div></div>
<nav id="project-nav" class="project-navigation" aria-label="Navegar por projeto" hidden><label for="project-select">Projeto · tarefas nos filtros atuais<select id="project-select" disabled><option disabled>Catálogo ainda indisponível</option></select></label><button id="all-projects" type="button" disabled>Todos os projetos</button><p id="project-count-note">Aguardando catálogo autorizado.</p></nav>
<div id="connection" class="connection"><p id="health" role="status" aria-live="polite">Consultando serviço…</p><button id="refresh" type="button">Atualizar agora</button></div>
<section aria-label="Resumo por estado"><div class="metrics" id="metrics">
<button class="metric" data-state="running" aria-pressed="false"><span class="metric-top">Executando <i class="dot running" aria-hidden="true"></i></span><strong>—</strong><small>Trabalho em andamento</small></button>
<button class="metric" data-state="waiting" aria-pressed="false"><span class="metric-top">Aguardando <i class="dot waiting" aria-hidden="true"></i></span><strong>—</strong><small>Fila, validação ou aprovação</small></button>
<button class="metric" data-state="blocked" aria-pressed="false"><span class="metric-top">Bloqueadas / paradas <i class="dot blocked" aria-hidden="true"></i></span><strong>—</strong><small>Confira o motivo e a ação</small></button>
<button class="metric" data-state="completed" aria-pressed="false"><span class="metric-top">Concluídas <i class="dot completed" aria-hidden="true"></i></span><strong>—</strong><small>Trabalho validado</small></button>
</div><p id="metric-note" class="metric-note">Contagens disponíveis após a consulta.</p></section>
<section id="workspace" aria-label="Tarefas na seleção" tabindex="-1">
<div class="toolbar"><div class="segments" role="group" aria-label="Período das tarefas">
<button type="button" data-period="active" aria-pressed="true">Em aberto<span>—</span></button><button type="button" data-period="history" aria-pressed="false">Histórico<span>—</span></button><button type="button" data-period="all" aria-pressed="false">Todas<span>—</span></button>
</div><div class="filters"><label class="search" for="query">Buscar tarefa<input id="query" type="search" placeholder="Título, ID, atividade ou motivo" autocomplete="off"></label><label for="state">Estado<select id="state"><option value="">Todos os estados</option><option value="running">Executando</option><option value="waiting">Aguardando / fila</option><option value="blocked">Bloqueada / parada</option><option value="completed">Concluída</option></select></label><label for="sort">Ordenar<select id="sort"><option value="attention">Impedimentos primeiro</option><option value="recent">Mudança mais recente</option></select></label></div></div>
<div class="list-heading"><h2 id="list-title">Em aberto</h2><p id="count" role="status" aria-live="polite">Aguardando consulta</p><button id="clear" hidden type="button">Limpar filtros</button></div>
<div id="columns" class="columns" aria-hidden="true" hidden><span>Tarefa / projeto</span><span>Última atividade ou impedimento</span><span>Estado observado</span><span>Última mudança</span></div>
<div id="tasks"></div>
<div class="empty" id="empty"><h3 id="empty-title">Consultando tarefas</h3><p id="empty-message">Aguarde a primeira consulta ao serviço.</p></div>
</section>
<p class="footnote">Cada tarefa mostra a última observação registrada pelo serviço. Consultar novamente não observa o executor em tempo real. Um run terminado ainda precisa da validação do trabalho; publicação e implantação têm estados próprios. Consulta automática a cada 15 segundos. Dados privados, acesso local.</p>
</main>
<script>
'use strict';
const byId = id => document.getElementById(id);
const stateNames = {running:'Executando', waiting:'Aguardando / fila', blocked:'Bloqueada / parada', completed:'Concluída'};
const periodNames = {active:'Em aberto', history:'Histórico', all:'Todas as tarefas'};
const entries = new Map(), projectGroups = new Map();
const globalMode = document.body.dataset.mode === 'all', projectOptions = new Map();
let projects = [], selectedProject = '', selectedProjectTitle = '';
let tasks = [], period = 'active', busy = false, available = false, lastSuccess = null;
if (globalMode) {
  byId('project-nav').hidden = false; setText(byId('project'), 'Todos os projetos');
  setText(byId('scope-note'), 'Projetos autorizados · catálogo ainda indisponível');
}
function node(tag, text = '', cls = '') {
  const n = document.createElement(tag); n.textContent = text; if (cls) n.className = cls; return n;
}
function normalize(value) {
  return String(value ?? '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLocaleLowerCase('pt-BR').trim();
}
function matches(t, query) {
  const s = t.tracking;
  return normalize([t.title, t.id, t.short_id, t.project_title, t.project_id, s.activity, s.blocker, s.blocked_reason, s.phase].join(' ')).includes(query);
}
function stamp(value) {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString('pt-BR', {day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit'}) : 'desconhecida';
}
function recency(t) { return typeof t.observed_at === 'number' ? t.observed_at : 0; }
function setText(element, value) { const text = String(value ?? 'Não informado'); if (element.textContent !== text) element.textContent = text; }
function field(parent, title, cls = '') {
  const section = node('section', '', cls); section.append(node('h4', title)); const value = node('p'); section.append(value); parent.append(section); return {section, value};
}
function createEntry(id) {
  const detail = node('details', '', 'task');
  const summary = node('summary', '', 'task-summary');
  const identity = node('span', '', 'identity');
  const identityLine = node('span', '', 'identity-line');
  const project = node('span', '', 'project-name'), short = node('span', '', 'short-id'), title = node('span', '', 'task-title');
  identityLine.append(project, short); identity.append(identityLine, title);
  const activity = node('span', '', 'activity');
  const status = node('span', '', 'status'), dot = node('i', '', 'dot'), label = node('span', '', 'status-label'); dot.setAttribute('aria-hidden', 'true'); status.append(dot, label);
  const change = node('span', '', 'change'), changed = node('span', '', 'changed'), chevron = node('span', '›', 'chevron'); chevron.setAttribute('aria-hidden', 'true'); change.append(changed, chevron);
  summary.append(identity, activity, status, change); detail.append(summary);
  const content = node('div', '', 'detail'), head = node('div', '', 'detail-head'), fullTitle = node('h3'), close = node('button', 'Fechar detalhes', 'close'); close.type = 'button';
  head.append(fullTitle, close); content.append(head);
  const grid = node('div', '', 'detail-grid');
  const happening = field(grid, 'O que está acontecendo'), next = field(grid, 'Próxima ação', 'next'), blocker = field(grid, 'Por que está impedida', 'detail-blocker'), result = field(grid, 'Resultado do trabalho');
  content.append(grid);
  const footer = node('footer'), links = node('div', '', 'links');
  const origin = node('a', 'Abrir conversa de origem ↗'), executor = node('a', 'Abrir conversa do executor ↗');
  for (const link of [origin, executor]) link.rel = 'noreferrer';
  links.append(origin, executor); footer.append(links);
  const meta = node('details', '', 'metadata'); meta.append(node('summary', 'Identificação e observações'));
  const dl = node('dl'), values = {};
  for (const [key, name] of [['id','ID completo'],['project','Projeto'],['phase','Etapa'],['updated','Mudança observada'],['execution','Execução observada'],['reason','Código do impedimento'],['revision','Revisão do registro']]) {
    const dd = node('dd'); dl.append(node('dt', name), dd); values[key] = dd;
  }
  meta.append(dl); footer.append(meta); content.append(footer); detail.append(content);
  close.addEventListener('click', () => { detail.open = false; summary.focus({preventScroll:true}); });
  detail.addEventListener('keydown', event => { if (event.key === 'Escape' && detail.open) { event.preventDefault(); event.stopPropagation(); detail.open = false; summary.focus({preventScroll:true}); } });
  // Native disclosure semantics provide expanded/collapsed state and keyboard activation.
  return {id, detail, summary, project, short, title, activity, status, label, changed, fullTitle, happening, next, blocker, result, origin, executor, links, values};
}
function updateEntry(entry, t) {
  const s = t.tracking;
  setText(entry.project, t.project_title + (globalMode ? ' · ' + t.project_id : '')); setText(entry.short, '#' + t.short_id); entry.short.title = t.id;
  setText(entry.title, t.title); setText(entry.fullTitle, t.title);
  setText(entry.activity, s.blocker || s.activity); entry.activity.title = s.blocker || s.activity;
  entry.status.className = 'status ' + s.state; setText(entry.label, stateNames[s.state]);
  setText(entry.changed, typeof t.observed_at === 'number' ? stamp(t.observed_at * 1000) : s.updated_at); entry.changed.title = 'Última mudança observada: ' + s.updated_at;
  setText(entry.happening.value, s.activity); setText(entry.next.value, s.next_action); setText(entry.blocker.value, s.blocker); entry.blocker.section.hidden = !s.blocker; setText(entry.result.value, s.result);
  setText(entry.values.id, t.id); setText(entry.values.project, t.project_title + ' · ' + t.project_id); setText(entry.values.phase, s.phase); setText(entry.values.updated, s.updated_at);
  setText(entry.values.execution, s.observed_status + ' / ' + s.observed_work_state); setText(entry.values.reason, s.blocked_reason || 'Nenhum registrado'); setText(entry.values.revision, t.snapshot_revision ?? 'Desconhecida');
  for (const [link, url] of [[entry.origin,s.origin_url], [entry.executor,s.child_url]]) {
    // Defense in depth: only the official native conversation scheme is navigable.
    const safe = typeof url === 'string' && /^t3-thread:\/\/v1\/[^\s]+$/.test(url);
    link.hidden = !safe; if (safe) link.href = url; else link.removeAttribute('href');
  }
  entry.links.hidden = entry.origin.hidden && entry.executor.hidden;
}
function renderProjects(filtered, projectExists) {
  const select = byId('project-select');
  select.disabled = !available; byId('all-projects').disabled = !available;
  byId('all-projects').setAttribute('aria-pressed', String(!selectedProject));
  if (!available && !projectOptions.size) return;
  const counts = new Map(); for (const t of filtered) counts.set(t.project_id, (counts.get(t.project_id) || 0) + 1);
  const catalog = [{id:'', title:'Todos os projetos'}, ...projects];
  if (!projectExists) catalog.push({id:selectedProject, title:'Projeto indisponível', missing:true});
  const ids = new Set(catalog.map(p => p.id));
  for (const [id, option] of projectOptions) if (!ids.has(id)) { option.remove(); projectOptions.delete(id); }
  // Reuse options as well as task rows, keeping keyboard focus through refresh.
  let previous = null;
  for (const p of catalog) {
    let option = projectOptions.get(p.id);
    if (!option) { option = node('option'); option.value = p.id; projectOptions.set(p.id, option); }
    option.disabled = !!p.missing;
    const count = available && !p.missing ? (p.id ? counts.get(p.id) || 0 : filtered.length) : '—';
    setText(option, p.title + (p.id ? ' · ' + p.id : '') + ' · ' + count);
    const next = previous ? previous.nextSibling : select.firstChild;
    if (next !== option) select.insertBefore(option, next);
    previous = option;
  }
  for (const option of [...select.options]) if (!projectOptions.has(option.value) || projectOptions.get(option.value) !== option) option.remove();
  select.value = selectedProject;
  const current = projects.find(p => p.id === selectedProject);
  if (current) selectedProjectTitle = current.title;
  setText(byId('project'), selectedProject ? (projectExists ? selectedProjectTitle : 'Projeto indisponível') : 'Todos os projetos');
  setText(byId('project-id'), selectedProject);
  setText(byId('scope-note'), available ? 'Catálogo autorizado · ' + projects.length + ' projetos' : 'Catálogo indisponível · seleção preservada');
  setText(byId('project-count-note'), available ? 'Contagens por projeto: período, busca e estado atuais. O resumo abaixo usa o projeto selecionado.' : 'Contagens por projeto indisponíveis. Aguarde uma consulta completa.');
}
function render() {
  const focus = document.activeElement;
  const query = normalize(byId('query').value).replace(/^#/, ''), state = byId('state').value;
  const projectExists = !globalMode || !selectedProject || projects.some(p => p.id === selectedProject);
  const known = available && projectExists;
  const inPeriod = t => period === 'all' || (period === 'history' ? t.history : !t.history);
  const inState = t => !state || t.tracking.state === state;
  const searchedAll = tasks.filter(t => matches(t, query));
  const searched = searchedAll.filter(t => !globalMode || !selectedProject || t.project_id === selectedProject);
  const scoped = searched.filter(t => period === 'all' || (period === 'history' ? t.history : !t.history));
  const selected = scoped.filter(inState);
  if (globalMode) {
    renderProjects(searchedAll.filter(t => inPeriod(t) && inState(t)), projectExists);
  }
  const priority = {blocked:0, waiting:1, running:2, completed:3};
  selected.sort((a,b) => (byId('sort').value === 'attention' ? priority[a.tracking.state] - priority[b.tracking.state] : 0) || recency(b)-recency(a) || a.id.localeCompare(b.id));
  for (const button of document.querySelectorAll('[data-state]')) {
    const key = button.dataset.state, count = scoped.filter(t => t.tracking.state === key).length;
    setText(button.querySelector('strong'), known ? count : '—'); button.disabled = !known;
    button.setAttribute('aria-pressed', String(state === key));
  }
  for (const button of document.querySelectorAll('[data-period]')) {
    const key = button.dataset.period;
    const count = searched.filter(inState).filter(t => key === 'all' || (key === 'history' ? t.history : !t.history)).length;
    setText(button.querySelector('span'), ' ' + (known ? count : '—')); button.setAttribute('aria-pressed', String(key === period));
  }
  const scopeName = globalMode ? selectedProject ? selectedProjectTitle + ' · ' + selectedProject : 'Todos os projetos' : byId('project').textContent;
  setText(byId('metric-note'), known ? scopeName + ' · ' + periodNames[period].toLocaleLowerCase('pt-BR') + (query ? ' para esta busca' : '') + ' · resumo antes do filtro de estado.' : 'Contagens indisponíveis. Estado atual desconhecido.');
  setText(byId('list-title'), periodNames[period]);
  setText(byId('count'), known ? selected.length + (selected.length === 1 ? ' tarefa' : ' tarefas') + ' de ' + scoped.length + (state ? ' · ' + stateNames[state] : '') : 'Contagem indisponível');
  byId('clear').hidden = !query && !state && period === 'active';
  byId('columns').hidden = !known || !selected.length;
  const root = byId('tasks'); root.hidden = !known;
  const ids = new Set(tasks.map(t => t.id)), visibleIds = new Set(selected.map(t => t.id));
  for (const [id, entry] of entries) { if (!ids.has(id)) { entry.detail.remove(); entries.delete(id); } else entry.detail.hidden = !visibleIds.has(id); }
  const groups = globalMode ? projects.map(p => ({...p, rows:selected.filter(t => t.project_id === p.id)})) : [{rows:selected}];
  for (const [id, group] of projectGroups) {
    if (!projects.some(p => p.id === id)) { group.section.remove(); projectGroups.delete(id); }
    else group.section.hidden = !selected.some(t => t.project_id === id);
  }
  let previousGroup = null;
  for (const p of groups) {
    if (!p.rows.length) continue;
    let container = root;
    if (globalMode) {
      let group = projectGroups.get(p.id);
      if (!group) {
        const section = node('section', '', 'project-group'), heading = node('h3', '', 'project-group-heading'), name = node('strong'), count = node('span'), list = node('div');
        heading.append(name, count); section.append(heading, list); group = {section, heading, name, count, list}; projectGroups.set(p.id, group);
      }
      group.section.hidden = false; group.section.setAttribute('aria-label', p.title + ' · ' + p.id);
      setText(group.name, p.title + ' · ' + p.id); setText(group.count, p.rows.length + (p.rows.length === 1 ? ' tarefa' : ' tarefas'));
      group.heading.hidden = !!selectedProject;
      const next = previousGroup ? previousGroup.nextSibling : root.firstChild;
      if (next !== group.section) root.insertBefore(group.section, next);
      previousGroup = group.section; container = group.list;
    }
    let previous = null;
    for (const t of p.rows) {
      let entry = entries.get(t.id); if (!entry) { entry = createEntry(t.id); entries.set(t.id, entry); }
      updateEntry(entry, t); entry.detail.hidden = false;
      const next = previous ? previous.nextSibling : container.firstChild;
      if (next !== entry.detail) container.insertBefore(entry.detail, next);
      previous = entry.detail;
    }
  }
  byId('empty').hidden = known && selected.length > 0;
  if (available && !projectExists) {
    setText(byId('empty-title'), 'Projeto selecionado indisponível');
    setText(byId('empty-message'), 'O projeto ' + selectedProject + ' saiu do catálogo autorizado. Escolha um projeto disponível ou volte a Todos os projetos. A seleção foi mantida.');
  } else if (known) {
    const filtered = !!query || !!state;
    setText(byId('empty-title'), filtered ? 'Nenhuma tarefa corresponde aos filtros' : period === 'active' ? 'Nenhuma tarefa em aberto' : 'Nenhuma tarefa neste período');
    setText(byId('empty-message'), filtered ? 'Tente outro título, ID, atividade ou motivo, ou limpe os filtros.' : period === 'active' ? 'Consulte o histórico para ver trabalhos encerrados e seus resultados.' : 'A consulta foi concluída. Não há tarefas registradas nesta seleção.');
  }
  if (focus && focus !== document.body && focus.isConnected && !focus.closest('[hidden]') && document.activeElement !== focus) focus.focus({preventScroll:true});
}
async function refresh() {
  if (busy) return; busy = true; byId('refresh').setAttribute('aria-busy','true');
  try {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 8000);
    let data;
    try { const response = await fetch('/api/tasks', {cache:'no-store', signal:controller.signal}); if (!response.ok) throw Error('unavailable'); data = await response.json(); } finally { clearTimeout(timer); }
    if (!Array.isArray(data.tasks) || data.source !== 'service' || !data.fetched_at) throw Error('invalid');
    if (globalMode) {
      if (data.mode !== 'all' || !Array.isArray(data.projects) || !data.projects.every(p => typeof p.id === 'string' && p.id && typeof p.title === 'string')) throw Error('invalid catalog');
      projects = data.projects;
    }
    tasks = data.tasks; lastSuccess = data.fetched_at; available = true;
    if (!globalMode) { setText(byId('project'), data.project_title); setText(byId('project-id'), data.project_id); }
    byId('connection').className = 'connection'; setText(byId('health'), 'Consulta ao serviço: ' + stamp(lastSuccess) + ' · estados da última observação registrada.'); render();
  } catch (_) {
    available = false;
    byId('connection').className = 'connection failed';
    setText(byId('health'), 'Serviço indisponível. Estado atual desconhecido. Última consulta bem-sucedida: ' + (lastSuccess ? stamp(lastSuccess) : 'nenhuma') + '.');
    render(); setText(byId('empty-title'), 'Não foi possível consultar as tarefas'); setText(byId('empty-message'), 'Os estados e as contagens estão indisponíveis. Use Atualizar agora para tentar novamente.');
  } finally { busy = false; byId('refresh').removeAttribute('aria-busy'); }
}
byId('refresh').addEventListener('click', refresh);
byId('project-select').addEventListener('change', () => { selectedProject = byId('project-select').value; render(); });
byId('all-projects').addEventListener('click', () => { selectedProject = ''; render(); });
for (const id of ['state', 'query', 'sort']) byId(id).addEventListener('input', render);
for (const button of document.querySelectorAll('[data-state]')) button.addEventListener('click', () => { byId('state').value = byId('state').value === button.dataset.state ? '' : button.dataset.state; render(); });
for (const button of document.querySelectorAll('[data-period]')) button.addEventListener('click', () => { period = button.dataset.period; render(); });
byId('clear').addEventListener('click', () => { byId('query').value = ''; byId('state').value = ''; period = 'active'; render(); byId('query').focus(); });
render(); refresh(); setInterval(refresh,15000);
</script>
</body></html>"""


def make_server(config, project_id, port=8765, *, all_projects=False):
    if type(all_projects) is not bool or (all_projects and project_id is not None):
        raise ValueError("task_panel_scope_conflict")
    if not all_projects and (
        not isinstance(project_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", project_id)
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
            # Fix scope at process launch; no query parameters or file serving.
            expected = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != expected:
                self.respond(403, "Forbidden", "text/plain")
            elif self.headers.get("Sec-Fetch-Site") == "cross-site":
                self.respond(403, "Forbidden", "text/plain")
            elif self.path == "/":
                page = (
                    PAGE.replace('data-mode="project"', 'data-mode="all"')
                    if all_projects
                    else PAGE
                )
                self.respond(200, page, "text/html")
            elif self.path == "/api/tasks":
                try:
                    result = asyncio.run(
                        all_project_tasks(config)
                        if all_projects
                        else project_tasks(config, project_id)
                    )
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


def serve(config, project_id, port, *, all_projects=False):
    from oraculo.stdio import print_json

    with make_server(config, project_id, port, all_projects=all_projects) as server:
        print_json(
            dict(
                state="serving",
                project_id=project_id,
                all_projects=all_projects,
                url=f"http://127.0.0.1:{server.server_port}/",
                source="managed.read",
                read_only=True,
            )
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
