"""Dependency-free, offline dashboard of the same model as the Obsidian export."""

from __future__ import annotations

import json
from html import escape as esc


def dashboard(model: dict) -> str:
    from oraculo.experience import KINDS, metric_lines, note_title, source_href

    def e(value):
        return esc(str(value), quote=True)

    cards = []
    for entry in model["entries"]:
        sources = "".join(
            f'<li><a href="{e(source_href(p))}">{e(note_title(model["documents"][p], "Evidência estruturada"))}</a></li>'
            for p in entry["sources"]
        )
        metrics = ""
        if entry.get("metrics"):
            metrics = (
                '<section class="memory"><h3>O que a memória trouxe</h3><ul>'
                + "".join(
                    "<li>" + e(line) + "</li>"
                    for line in metric_lines(entry["metrics"])
                )
                + "</ul></section>"
            )
        view = entry.get("presentation") or {}
        action = f'<p class="action">{e(view["next_action"])}</p>' if view else ""
        conversation = (
            f'<a href="{e(view["url"])}">Abrir conversa da tarefa ↗</a>'
            if view.get("url")
            else ""
        )
        recurrence = (
            f"<p>Ocorrências declaradas em tarefas: {entry['occurrences']}</p>"
            if entry.get("occurrences")
            else ""
        )
        cards.append(f'''<article data-kind="{e(entry["kind"])}" data-topics="{e(json.dumps(entry["topics"], ensure_ascii=False))}" data-state="{e(entry["state"])}">
<div class="eyebrow">{e(KINDS[entry["kind"]])} <span>{e(entry["state"])}</span></div>
<h2>{e(entry["title"])}</h2><p class="date">Atualização: {e(entry["updated_at"])}</p>
<p class="topics">{e(" · ".join(entry["topics"]) or "Assunto não informado")}</p>
<p class="excerpt">{e(entry["text"][:700])}</p>{action}{conversation}{recurrence}{metrics}
<details><summary>Fontes e detalhes ({len(entry["sources"])})</summary><ul>{sources}</ul>
<p>{entry.get("unavailable_sources", 0)} referências indisponíveis neste recorte.</p>
<details><summary>Texto completo e identidade técnica</summary><pre>{e(entry["text"])}</pre><code>{e(entry.get("identity") or entry["id"])}</code></details></details></article>''')
    topics = sorted({t for entry in model["entries"] for t in entry["topics"]})
    kinds = "".join(f'<option value="{e(k)}">{e(v)}</option>' for k, v in KINDS.items())
    topic_options = "".join(f'<option value="{e(t)}">{e(t)}</option>' for t in topics)
    states = "".join(
        f"<option>{e(s)}</option>"
        for s in sorted({entry["state"] for entry in model["entries"]})
    )
    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>{e(model["project_title"])} · Oráculo</title><style>
:root{{color-scheme:light;--ink:#172e34;--muted:#4e696d;--line:#cbd9d5;--accent:#12675f}}
*{{box-sizing:border-box}}body{{margin:0;background:#f2f5ef;color:var(--ink);font:16px/1.55 system-ui,sans-serif}}
a{{color:#095f79;text-underline-offset:3px}}a:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{{outline:3px solid #bb4a1b;outline-offset:3px}}
header,main,footer{{max-width:1240px;margin:auto;padding:28px}}header{{padding-top:40px}}
.brand{{font-weight:750;letter-spacing:.18em;font-size:13px;color:var(--accent)}}h1{{font-size:clamp(30px,5vw,52px);line-height:1.12;margin:16px 0}}.intro{{max-width:740px;color:var(--muted)}}
.status{{display:flex;gap:12px;flex-wrap:wrap;margin:24px 0}}.status p{{margin:0;background:#dde9df;border-radius:8px;padding:12px 18px}}
.controls{{display:grid;grid-template-columns:2fr 1fr 1fr 1fr;gap:14px;padding:20px;background:#fff;border:1px solid var(--line);border-radius:12px}}
label{{font-size:14px;font-weight:650;display:grid;gap:6px}}input,select{{width:100%;min-width:0;min-height:46px;font:inherit;border:1px solid #8aa4a3;border-radius:6px;padding:8px;background:#fff;color:var(--ink)}}
#count{{color:var(--muted);margin:20px 0}}.cards{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px;align-items:start}}
article{{border:1px solid var(--line);border-radius:12px;background:#fff;padding:24px;overflow-wrap:anywhere}}article[hidden]{{display:none}}
.eyebrow{{font-weight:750;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--accent);display:flex;gap:12px;justify-content:space-between;flex-wrap:wrap}}.eyebrow span{{color:#5b5a40;font-weight:600}}
h2{{font-size:23px;line-height:1.3;margin:14px 0 10px}}h3{{font-size:16px}}.date,.topics{{font-size:13px;color:var(--muted)}}.excerpt{{white-space:pre-wrap}}.action{{border-left:3px solid var(--accent);padding-left:12px}}
.memory{{background:#f0f5ef;border-radius:6px;padding:1px 14px;margin:18px 0;font-size:14px}}ul{{padding-left:20px}}li{{margin:7px 0}}
details{{border-top:1px solid var(--line);margin-top:20px;padding-top:12px;font-size:14px}}summary{{cursor:pointer;font-weight:650;min-height:32px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.6 system-ui}}code{{font-size:12px;overflow-wrap:anywhere}}footer{{color:var(--muted);font-size:14px}}
@media(max-width:700px){{header,main,footer{{padding:20px}}header{{padding-top:28px}}.cards{{grid-template-columns:1fr}}.controls{{grid-template-columns:1fr 1fr}}.search{{grid-column:1/-1}}article{{padding:20px}}}}
@media(max-width:380px){{.controls{{grid-template-columns:1fr}}}}
</style></head><body><header><div class="brand">ORÁCULO / SEGUNDO CÉREBRO</div>
<h1>{e(model["project_title"])}</h1><p class="intro">Encontre assuntos, acompanhe tarefas e confira de onde veio cada aprendizado. As mesmas informações estão no Obsidian, a partir de Inicio.md.</p>
<div class="status"><p>{len(model["entries"])} registros navegáveis</p><p>{len(model["documents"])} fontes disponíveis</p><p>Benefício da memória: desconhecido</p></div>
<p class="intro">Última atualização observada: {e(model["observed_at"])}. Exportação local; sem atualização automática.</p></header>
<main><div class="controls" role="search"><label class="search">Buscar por assunto ou texto<input id="query" type="search" placeholder="Ex.: testes, decisões, erro de importação"></label>
<label>Tipo<select id="kind"><option value="">Todos os tipos</option>{kinds}</select></label>
<label>Assunto<select id="topic"><option value="">Todos os assuntos</option>{topic_options}</select></label>
<label>Estado<select id="state"><option value="">Todos os estados</option>{states}</select></label></div>
<p id="count" aria-live="polite">{len(cards)} registros</p><div class="cards">{"".join(cards)}</div>
<p id="empty" hidden>Nenhum registro corresponde aos filtros.</p></main>
<footer>Dados do projeto selecionado e conhecimento compartilhado. Leitura do agente, aplicação declarada e resultado verificado são evidências distintas. Checks não demonstram causalidade. Mantenha este pacote privado.</footer>
<script>
const controls=['query','kind','topic','state'].map(id=>document.getElementById(id));
const normalize=s=>s.normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').toLocaleLowerCase('pt-BR');
function filter(){{let count=0;const [query,kind,topic,state]=controls.map(c=>c.value);
for(const card of document.querySelectorAll('article')){{const match=normalize(card.textContent).includes(normalize(query))&&(!kind||card.dataset.kind===kind)&&(!topic||JSON.parse(card.dataset.topics).includes(topic))&&(!state||card.dataset.state===state);card.hidden=!match;if(match)count++;}}
document.getElementById('count').textContent=count+' registros';document.getElementById('empty').hidden=count!==0;}}
controls.forEach(c=>c.addEventListener('input',filter));
</script></body></html>"""
