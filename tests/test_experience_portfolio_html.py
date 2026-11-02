"""Portfolio dashboard rendered from direct models. No daemon, vault or network."""

import copy
import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

import pytest

from oraculo.experience_portfolio_html import SHARED_EXPORT_ROOT, dashboard

FIXTURE = Path(__file__).parent / "fixtures/experience_portfolio.json"


def model():
    return json.loads(FIXTURE.read_text())


def page(data=None):
    return dashboard(model() if data is None else data)


def region(html, start, end):
    begin = html.index(start)
    stop = html.index(end, begin + len(start))
    return html[begin:stop]


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = 0

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.scripts += 1


def test_fixture_is_a_direct_portfolio_model():
    data = model()
    assert data["schema_version"] == 1 and data["kind"] == "portfolio"
    titles = [project["project_title"] for project in data["projects"]]
    assert len(data["projects"]) >= 2
    assert len(titles) != len(set(titles))
    assert any(not project["entries"] for project in data["projects"])
    local_ids = [
        entry["id"] for project in data["projects"] for entry in project["entries"]
    ]
    assert len(local_ids) != len(set(local_ids))
    alfa = data["projects"][0]
    assert any(entry.get("subtask_ids") for entry in alfa["entries"])
    assert data["shared"]["entries"]


def test_render_does_not_mutate_the_model():
    data = model()
    original = copy.deepcopy(data)
    page(data)
    assert data == original


def test_overview_lists_projects_counts_and_empty_state():
    html = page()
    assert "<title>Todos os projetos · Oráculo</title>" in html
    assert (
        'id="overview-panel"' in html
        and "hidden" not in html.split('id="overview-panel"', 1)[1][:80]
    )
    assert "3 projetos · 3 tarefas · 2 documentos" in html
    assert "Observado em 2026-11-01T09:00:00Z" in html
    assert "Prateleira vazia" in html
    assert "Sem tarefas e sem documentos neste snapshot." in html
    assert "Demonstração sintética" in html
    assert "Atlas · projetos/atlas-alfa" in html
    assert "Atlas · projetos/atlas-beta" in html
    assert html.index("Atlas · projetos/atlas-alfa") < html.index(
        "Atlas · projetos/atlas-beta"
    )


def test_duplicate_titles_and_local_ids_stay_in_their_projects():
    html = page()
    alfa = region(html, 'id="hierarchy-alfa"', 'id="hierarchy-beta"')
    beta = region(html, 'id="hierarchy-beta"', 'id="hierarchy-vazio"')
    assert "Organizar o acervo" in alfa
    assert "Corrigir o índice Beta" not in alfa
    assert "Corrigir o índice Beta" in beta
    assert "Organizar o acervo" not in beta
    assert 'data-project="alfa" data-entry="local-1"' in alfa
    assert 'data-project="beta" data-entry="local-1"' in beta
    assert "Fix exclusivo do segundo Atlas." in html
    assert "FOREIGN_PROJECT_SENTINEL" not in html


def test_subtasks_are_nested_and_records_are_not_children():
    html = page()
    alfa = region(html, 'id="hierarchy-alfa"', 'id="hierarchy-beta"')
    assert "<details" in alfa and "Subtarefas (1)" in alfa
    assert alfa.index("local-1") < alfa.index("<details") < alfa.index("local-child")
    assert 'data-entry="dec-1"' not in alfa
    assert 'data-entry="les-1"' not in alfa
    assert 'data-entry="err-1"' not in alfa
    assert "Decisão de preservação" in html
    assert "Lição sobre o acervo" in html
    assert "erro_acervo_ausente" in html
    assert "Sem subtarefas registradas." in html
    library = region(html, 'id="library-alfa"', 'id="library-beta"')
    assert "Decisões, aprendizados e erros" in library
    assert "Nota do acervo Alfa" in library


def test_shared_scope_and_only_resolvable_cross_project_links():
    html = page()
    assert "Conhecimento compartilhado" in html
    assert "Escopo: conhecimento compartilhado" in html
    assert "Lição compartilhada" in html
    assert "O conhecimento compartilhado permanece rotulado." in html
    assert "referencia" in html and "campo explícito related_project" in html
    assert (
        "usa" in html and "referência explícita ao conhecimento compartilhado" in html
    )
    assert "fora-do-recorte" not in html
    assert html.count("FOREIGN_PROJECT_SENTINEL") == 0


def test_evidence_links_stay_under_each_export_root():
    html = page()
    hrefs = re.findall(r'href="([^"]+)"', html)
    assert "projetos/atlas-alfa/Evidencias/projects/alfa/wiki/nota.md.html" in hrefs
    shared = f"{SHARED_EXPORT_ROOT}/Evidencias/shared/wiki/licao.md.html"
    assert shared in hrefs
    for href in hrefs:
        if href.startswith("#"):
            continue
        decoded = unquote(href)
        assert ".." not in decoded.split("/")
        assert not decoded.startswith(("/", "\\"))
        assert "://" not in decoded
        assert not decoded.lower().startswith("javascript:")


def test_search_is_global_accent_insensitive_and_wave_is_optional():
    html = page()
    assert 'id="query"' in html and 'type="search"' in html
    assert "Buscar em todo o recorte" in html
    assert "Onda (opcional)" in html
    assert 'id="results"' in html
    assert 'class="context"' in html
    assert "normalize('NFD')" in html
    assert "toLocaleLowerCase('pt-BR')" in html
    assert "Decisão de preservação" in html
    assert "resultados no recorte" in html
    script = re.search(r"<script>(.*)</script>", html, re.S).group(1)
    node = shutil.which("node")
    if node:
        completed = subprocess.run(
            [node, "--check", "-"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
    assert "fetch(" not in html
    assert "position:sticky" in html
    assert "@media(max-width:620px)" in html


def test_labels_keyboard_and_mobile_bar():
    html = page()
    assert 'lang="pt-BR"' in html
    assert 'class="skip"' in html
    assert 'aria-live="polite"' in html
    assert 'aria-label="Modo de navegação"' in html
    assert 'aria-label="Projetos, tarefas e biblioteca"' in html
    assert '<select id="project"' in html
    assert 'id="mobile-nav"' in html
    assert "min-height:44px" in html
    assert "font-size:16px" in html
    parser = _Tags()
    parser.feed(html)
    assert parser.scripts == 1


def test_malicious_metadata_cannot_break_the_script_or_leave_the_vault():
    data = model()
    attack = '</script><img src=x onerror="window.pwned=1">@@TITLE@@'
    data["projects"][0]["project_title"] = attack
    data["projects"][0]["export_root"] = "../evil"
    entry = data["projects"][0]["entries"][0]
    entry["title"] = attack
    entry["text"] = attack + "\n<script>window.pwned=2</script>"
    entry["topics"] = [attack]
    entry["wave"] = attack
    entry["presentation"] = {"url": "javascript:evil()"}
    data["projects"][0]["entries"][-1]["document_path"] = "../../secret.md"
    data["links"][0]["relation"] = attack
    html = page(data)
    parser = _Tags()
    parser.feed(html)
    assert parser.scripts == 1
    assert html.count("</script>") == 1
    assert "<img src=x" not in html
    assert "<script>window.pwned" not in html
    assert "@@TITLE@@" in html
    assert 'href="javascript:' not in html
    assert "fetch(" not in html
    hrefs = [unquote(href) for href in re.findall(r'href="([^"]+)"', html)]
    assert all(".." not in href.split("/") for href in hrefs)
    assert all(not href.startswith(("http:", "https:", "//")) for href in hrefs)
    assert "Content-Security-Policy" in html
    assert "default-src 'none'" in html


def test_rejects_a_model_that_is_not_the_portfolio_contract():
    with pytest.raises(ValueError, match="experience_portfolio_html_model"):
        dashboard({"schema_version": 1, "kind": "project", "projects": []})
    data = model()
    data["projects"][1]["project_id"] = "alfa"
    with pytest.raises(ValueError, match="experience_portfolio_project_id"):
        dashboard(data)
    data = model()
    data["projects"][2]["project_id"] = "shared"
    with pytest.raises(ValueError, match="experience_portfolio_project_id"):
        dashboard(data)
