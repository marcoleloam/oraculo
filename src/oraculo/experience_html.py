"""Dependency-free, offline reading dashboard of the scoped projection."""

from __future__ import annotations

import json
from html import escape as esc


def dashboard(model: dict) -> str:
    from oraculo.experience import KINDS, metric_lines, source_href

    def e(value):
        return esc(str(value), quote=True)

    entries = model["entries"]
    sources = {v["document_path"]: v for v in entries if v.get("document_path")}

    by_id = {v["id"]: v for v in entries}
    tasks = [v for v in entries if v.get("identity")]

    def context(entry):
        ancestors = entry.get("ancestor_ids", [])
        related = by_id.get(entry.get("related_task_id"))
        if related:
            ancestors = related.get("ancestor_ids", []) + [related["id"]]
        return " / ".join(
            [model["project_title"], *[by_id[a]["title"] for a in ancestors]]
        )

    def button(entry, nested="", show_context=False):
        trail = (
            '<small class="context">' + e(context(entry)) + "</small>"
            if show_context
            else ""
        )
        return (
            f'<li><button type="button" data-entry="{entry["id"]}" '
            f'aria-controls="read-{entry["id"]}">{trail}<span>{e(entry["title"])}</span>'
            f"<small>{e(KINDS[entry['kind']])} · {e(entry['state'])}</small></button>{nested}</li>"
        )

    def task_node(entry):
        children = entry.get("subtask_ids", [])
        nested = (
            (
                '<details class="subtasks" open><summary>Subtarefas ('
                + str(len(children))
                + ")</summary><ul>"
                + "".join(task_node(by_id[c]) for c in children)
                + "</ul></details>"
            )
            if children
            else ""
        )
        return button(entry, nested)

    tree = (
        '<ul class="task-tree">'
        + "".join(task_node(v) for v in tasks if not v.get("parent_entry_id"))
        + "</ul>"
    )
    if not tasks:
        tree += '<p class="meta">Sem tarefas registradas neste snapshot. O legado de memória está na Biblioteca.</p>'
    library = "<ul>" + "".join(button(v) for v in sources.values()) + "</ul>"
    articles = []
    for entry in entries:
        subtasks = ""
        if entry.get("identity"):
            child_links = "".join(
                f'<li><button class="source" type="button" data-entry="{c}">{e(by_id[c]["title"])}</button></li>'
                for c in entry["subtask_ids"]
            )
            subtasks = (
                "<section><h3>Subtarefas</h3>"
                + (
                    '<ul class="sources">' + child_links + "</ul>"
                    if child_links
                    else "<p>Sem subtarefas registradas.</p>"
                )
                + "</section>"
            )
            if entry.get("parent_entry_id"):
                parent = by_id[entry["parent_entry_id"]]
                subtasks += f'<p>Tarefa pai: <button class="source" type="button" data-entry="{parent["id"]}">{e(parent["title"])}</button></p>'
            if entry["relationship_status"] in {"orphan", "cycle"}:
                subtasks += '<p class="meta">Relação de tarefa pai indisponível neste recorte ou inconsistente. Registro preservado sem inferir parentesco.</p>'
        source_links = "".join(
            f'<li><button class="source" type="button" data-entry="{sources[p]["id"]}">{e(sources[p]["title"])}</button></li>'
            for p in entry["sources"]
        )
        parts = []
        for key, label in (("decisions", "Decisões"), ("lessons", "Aprendizados")):
            if entry.get(key):
                parts.append(
                    f"<section><h3>{label}</h3>"
                    + "".join(f'<p class="reading">{e(t)}</p>' for t in entry[key])
                    + "</section>"
                )
        technical = [
            entry.get("identity") or entry["id"],
            "Origem da onda: " + entry["wave_basis"],
        ]
        if entry.get("document_path"):
            from oraculo.memory.schema import digest

            path = entry["document_path"]
            technical += [path, "SHA-256: " + digest(model["evidence"][path])]
            parts.append(
                f'<p><a href="{e(source_href(path))}">Abrir evidência original preservada ↗</a></p>'
            )
        if entry.get("metrics"):
            technical += metric_lines(entry["metrics"])
        view = entry.get("presentation") or {}
        action = f'<p class="action">{e(view["next_action"])}</p>' if view else ""
        tracking = entry.get("tracking") or {}
        if tracking:
            action = (
                f"<p><strong>{e(tracking['label'])}</strong> · {e(tracking['phase'])}</p>"
                f"<p>{e(tracking['activity'])}</p>"
                f"<p>{e(tracking.get('blocker') or tracking['result'])}</p>"
                f'<p class="action">{e(tracking["next_action"])}</p>'
                f"<p>Última observação de execução: {e(tracking['observed_status'])}</p>"
            )
        conversation = (
            f'<p><a href="{e(view["url"])}">Abrir conversa de origem ↗</a></p>'
            if view.get("url")
            else ""
        )
        if tracking:
            conversation = " · ".join(
                f'<a href="{e(tracking[key])}">{label} ↗</a>'
                for key, label in (
                    ("origin_url", "Conversa de origem"),
                    ("child_url", "Conversa do executor"),
                )
                if tracking.get(key)
            )
        unavailable = entry.get("unavailable_sources", 0)
        children = [
            v
            for v in entries
            if entry.get("identity") and v.get("task_id") == entry["identity"]
        ]
        related = (
            (
                '<section><h3>Registros relacionados da tarefa</h3><ul class="sources">'
                + "".join(
                    f'<li><button class="source" type="button" data-entry="{v["id"]}">{e(KINDS[v["kind"]])}: {e(v["title"])}</button></li>'
                    for v in children
                )
                + "</ul></section>"
            )
            if children
            else ""
        )
        original = (
            (
                '<details class="technical"><summary>Conteúdo original integral (JSON)</summary><pre>'
                + e(model["evidence"][entry["document_path"]])
                + "</pre></details>"
            )
            if entry.get("structured")
            else ""
        )
        articles.append(f'''<article id="read-{entry["id"]}" data-id="{entry["id"]}" data-kind="{e(entry["kind"])}" data-wave="{e(entry["wave"])}" data-state="{e(entry["state"])}" data-topics="{e(json.dumps(entry["topics"], ensure_ascii=False))}" tabindex="-1">
<p class="breadcrumb">{e(context(entry))} / {e(KINDS[entry["kind"]])}</p>
<h2>{e(entry["title"])}</h2><p class="meta">{e(entry["state"])} · Atualização: {e(entry["updated_at"])}</p>{action}
<section><h3>Conteúdo</h3><div class="reading">{e(entry["text"])}</div></section>{"".join(parts)}{subtasks}{related}
<section><h3>Evidências</h3><ul class="sources">{source_links}</ul><p class="meta">{unavailable} referências indisponíveis neste recorte.</p>{conversation}</section>
{original}<details class="technical"><summary>Identidade técnica e uso da memória</summary><pre>{e(chr(10).join(technical))}</pre></details></article>''')

    def options(items):
        return "".join(f'<option value="{e(k)}">{e(v)}</option>' for k, v in items)

    kinds = options(KINDS.items())
    states = options((v, v) for v in sorted({v["state"] for v in entries}))
    wave_options = options((v, v) for v in model["waves"])
    topics = options((v, v) for v in sorted({t for v in entries for t in v["topics"]}))
    demo = (
        '<p class="demo">Demonstração sintética · dados fictícios para explorar a navegação</p>'
        if model.get("demonstration")
        else ""
    )
    template = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>@@TITLE@@ · Oráculo</title><style>
:root{color-scheme:dark;--bg:#141619;--panel:#1c1f23;--text:#eeeae2;--muted:#b6b4ad;--line:#393c40;--gold:#dfbf7e;--active:#34302a}
*{box-sizing:border-box}body{overflow-wrap:anywhere;margin:0;background:var(--bg);color:var(--text);font:16px/1.65 system-ui,sans-serif}button,input,select{font:inherit;color:inherit}button,summary{cursor:pointer}a{color:var(--gold);text-underline-offset:4px}button,input,select{border:1px solid var(--line);border-radius:7px;background:var(--panel)}button{padding:10px 14px;min-height:44px}button:hover{border-color:var(--gold)}:focus-visible{outline:3px solid var(--gold);outline-offset:3px}[hidden]{display:none!important}.skip{position:absolute;left:12px;top:-100px;background:var(--panel);padding:12px;z-index:5}.skip:focus{top:8px}header{padding:28px 32px 22px;border-bottom:1px solid var(--line)}.brand{color:var(--gold);font-weight:700;font-size:12px;letter-spacing:.15em}h1{font-size:clamp(27px,4vw,40px);line-height:1.2;margin:10px 0}header p{margin:6px 0;color:var(--muted);font-size:14px}.demo{color:var(--gold);border-left:3px solid var(--gold);padding-left:12px}.project-bar{position:sticky;top:0;z-index:3;display:flex;align-items:center;gap:12px;padding:12px 20px;background:var(--bg);border-bottom:1px solid var(--line);font-size:14px}.project-bar span{color:var(--gold)}.project-bar strong{flex:1;min-width:0}.subtasks{margin-left:14px;border-left:1px solid var(--line);padding-left:10px}.subtasks summary{font-size:13px;color:var(--muted)}.controls{padding:20px 32px;display:grid;grid-template-columns:2fr repeat(4,minmax(0,1fr));gap:14px;border-bottom:1px solid var(--line)}label{display:grid;gap:5px;font-size:13px;color:var(--muted)}input,select{width:100%;min-width:0;min-height:44px;padding:9px 12px}.workspace{display:grid;grid-template-columns:minmax(250px,330px) minmax(0,1fr);align-items:start}aside{padding:22px 20px;border-right:1px solid var(--line);min-width:0}.modes{display:flex;gap:8px}.modes button{flex:1;padding:8px;font-size:14px}.modes [aria-pressed=true]{color:var(--gold);background:var(--active);border-color:var(--gold)}#count{font-size:13px;color:var(--muted);margin:15px 0}.wave{border-top:1px solid var(--line);padding-top:12px;margin:12px 0}.wave summary{font-weight:650;min-height:38px;color:var(--gold)}.wave summary span{font-size:12px;color:var(--muted);margin-left:8px}nav{max-height:620px;overflow:auto;scrollbar-gutter:stable}nav ul{list-style:none;margin:0;padding:0}nav li{margin:6px 0}nav li button{display:block;text-align:left;width:100%;border-color:transparent;background:transparent;padding:10px 12px;border-left:2px solid transparent}nav li button[aria-current=true]{background:var(--active);border-left-color:var(--gold)}nav li button span{overflow-wrap:anywhere;display:block;font-weight:550;line-height:1.4}nav small{display:block;color:var(--muted);font-size:12px;margin-top:5px}main{min-width:0;padding:28px 40px 48px}article{scroll-margin-top:85px;max-width:850px;overflow-wrap:anywhere}article:focus{outline:none}.js article{display:none}.js article.selected{display:block}.breadcrumb{color:var(--gold);font-size:13px;margin:0 0 15px}h2{font-size:clamp(25px,3.5vw,34px);line-height:1.3;margin:0 0 12px}h3{font-size:15px;color:var(--gold);margin:0 0 14px}.meta{color:var(--muted);font-size:13px}article section{border-top:1px solid var(--line);padding-top:24px;margin-top:26px}.reading{white-space:pre-wrap;font:16px/1.85 system-ui;tab-size:2}.sources{padding-left:0;list-style:none}.source{border:0;background:transparent;color:var(--gold);text-align:left;text-decoration:underline;text-underline-offset:3px;padding:8px 0}.technical{margin-top:30px;padding-top:14px;border-top:1px solid var(--line);font-size:13px;color:var(--muted)}summary{min-height:36px}pre{white-space:pre-wrap;overflow-wrap:anywhere}footer{padding:20px 32px;border-top:1px solid var(--line);font-size:12px;color:var(--muted)}#mobile-nav{display:none}#empty{color:var(--muted)}
@media(max-width:850px){.controls{grid-template-columns:1fr 1fr}.search{grid-column:1/-1}.workspace{grid-template-columns:270px minmax(0,1fr)}main{padding:24px}.controls label:last-child{grid-column:1/-1}}
@media(max-width:620px){header,.controls{padding:20px}.workspace{display:block}aside{border-right:0;border-bottom:1px solid var(--line);padding:16px 20px}main{padding:24px 20px 36px}.js #mobile-nav{display:inline-block;margin-top:10px}nav{max-height:420px;overflow:auto}h2{font-size:26px}footer{padding:20px}.reading{font-size:16px}.controls{gap:10px}}

</style></head><body><a class="skip" href="#reader">Ir para o conteúdo</a>
<div class="project-bar"><span>Projeto</span><strong>@@TITLE@@</strong><a href="#reader">Conteúdo</a></div><header><div class="brand">ORÁCULO / LEITURA</div><h1>@@TITLE@@</h1>@@DEMO@@
<p>Projeto → Tarefas → Subtarefas · Ondas opcionais</p><p>@@ENTRIES@@ entradas · @@SOURCES@@ fontes e notas · Observado em @@OBSERVED@@</p><p>Snapshot offline, sem atualização automática. Abra Inicio.md no Obsidian, sem plugins.</p></header>
<div class="controls" role="search"><label class="search">Buscar no título e conteúdo<input id="query" type="search" placeholder="Ex.: importação, decisão, aprendizado"></label><label>Tipo<select id="kind"><option value="">Todos os tipos</option>@@KINDS@@</select></label><label>Assunto<select id="topic"><option value="">Todos os assuntos</option>@@TOPICS@@</select></label><label>Estado<select id="state"><option value="">Todos os estados</option>@@STATES@@</select></label><label>Onda (opcional)<select id="wave"><option value="">Todas as ondas / sem filtro</option>@@WAVES@@</select></label></div>
<div class="workspace"><aside><div class="modes" aria-label="Modo de navegação"><button id="hierarchy-mode" type="button" aria-pressed="true">Tarefas</button><button id="library-mode" type="button" aria-pressed="false">Biblioteca</button></div><button type="button" id="mobile-nav" aria-controls="navigation" aria-expanded="true">Mostrar / ocultar navegação</button><p id="count" aria-live="polite">@@ENTRIES@@ entradas</p><nav id="navigation" aria-label="Conteúdo do projeto"><div id="hierarchy">@@TREE@@</div><div id="library" hidden>@@LIBRARY@@</div><div id="results" hidden>@@RESULTS@@</div></nav></aside><main id="reader" tabindex="-1"><p id="empty" hidden>Nenhum registro corresponde aos filtros. Ajuste a busca, o tipo ou o estado.</p>@@ARTICLES@@</main></div>
<footer>Somente projeto selecionado e conhecimento compartilhado do recorte. Declarações e resultados verificados são evidências distintas. Mantenha este pacote privado.</footer>
<script>
document.documentElement.classList.add('js');
const articles=Array.from(document.querySelectorAll('article'));
const byId=new Map(articles.map(a=>[a.dataset.id,a]));
const nav=document.getElementById('navigation');
const controls=['query','kind','topic','state','wave'].map(id=>document.getElementById(id));
const normalize=s=>s.normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').toLocaleLowerCase('pt-BR');
const searchText=new Map(articles.map(a=>[a.dataset.id,normalize(a.textContent)]));
let selected=null,mode=document.querySelector('#hierarchy [data-entry]')?'hierarchy':'library';
function select(id,focus=false){
 selected=id;
 for(const a of articles)a.classList.toggle('selected',a.dataset.id===id);
 for(const b of nav.querySelectorAll('[data-entry]'))b.setAttribute('aria-current',String(b.dataset.entry===id));
 if(focus&&byId.has(id))byId.get(id).focus();
}
function filter(){
 const [query,kind,topic,state,wave]=controls.map(c=>c.value);
 const matches=new Set(articles.filter(a=>searchText.get(a.dataset.id).includes(normalize(query))&&(!kind||a.dataset.kind===kind)&&(!topic||JSON.parse(a.dataset.topics).includes(topic))&&(!state||a.dataset.state===state)&&(!wave||a.dataset.wave===wave)).map(a=>a.dataset.id));
 for(const b of nav.querySelectorAll('[data-entry]'))b.parentElement.hidden=!matches.has(b.dataset.entry);
 const searching=Boolean(query||kind||topic||state||wave);
 document.getElementById('results').hidden=!searching;document.getElementById('hierarchy').hidden=searching||mode!=='hierarchy';document.getElementById('library').hidden=searching||mode!=='library';
 const scope=document.getElementById(searching?'results':mode),visible=Array.from(scope.querySelectorAll('[data-entry]')).filter(b=>matches.has(b.dataset.entry));

 document.getElementById('count').textContent=visible.length+(searching?' resultados no recorte':mode==='library'?' entradas na biblioteca':' tarefas e subtarefas');
 document.getElementById('empty').hidden=visible.length!==0;
 if(!visible.some(b=>b.dataset.entry===selected)){const preferred=visible.find(b=>['task','fix'].includes(byId.get(b.dataset.entry).dataset.kind))||visible[0];select(preferred?preferred.dataset.entry:null);}
}
function setMode(value){
 mode=value;document.getElementById('hierarchy').hidden=mode!=='hierarchy';document.getElementById('library').hidden=mode!=='library';
 document.getElementById('hierarchy-mode').setAttribute('aria-pressed',String(mode==='hierarchy'));
 document.getElementById('library-mode').setAttribute('aria-pressed',String(mode==='library'));filter();
}
nav.addEventListener('click',event=>{const b=event.target.closest('[data-entry]');if(b)select(b.dataset.entry,true);});
document.getElementById('reader').addEventListener('click',event=>{const b=event.target.closest('[data-entry]');if(b){controls.forEach(c=>c.value='');setMode(document.querySelector('#hierarchy [data-entry="'+b.dataset.entry+'"]')?'hierarchy':'library');select(b.dataset.entry,true);}});
controls.forEach(c=>c.addEventListener('input',filter));
document.getElementById('hierarchy-mode').addEventListener('click',()=>setMode('hierarchy'));
document.getElementById('library-mode').addEventListener('click',()=>setMode('library'));
document.getElementById('mobile-nav').addEventListener('click',event=>{nav.hidden=!nav.hidden;event.currentTarget.setAttribute('aria-expanded',String(!nav.hidden));});
setMode(mode);
const mobile=matchMedia('(max-width:620px)'),toggle=document.getElementById('mobile-nav');
function adaptNavigation(){nav.hidden=mobile.matches;toggle.setAttribute('aria-expanded',String(!nav.hidden));}
mobile.addEventListener('change',adaptNavigation);adaptNavigation();
</script></body></html>"""
    replacements = {
        "TITLE": e(model["project_title"]),
        "DEMO": demo,
        "ENTRIES": str(len(entries)),
        "SOURCES": str(len(sources)),
        "OBSERVED": e(model["observed_at"]),
        "KINDS": kinds,
        "STATES": states,
        "TOPICS": topics,
        "TREE": tree,
        "WAVES": wave_options,
        "LIBRARY": library,
        "RESULTS": "<ul>"
        + "".join(button(v, show_context=True) for v in entries)
        + "</ul>",
        "ARTICLES": "".join(articles),
    }
    # One replacement pass: source text containing template markers stays inert.
    import re

    return re.sub(r"@@([A-Z]+)@@", lambda match: replacements[match[1]], template)
