"""Exercise the real model and HTML together, including shared-file hoisting."""

import posixpath
from html.parser import HTMLParser
from urllib.parse import unquote

import pytest

from oraculo.experience_portfolio import portfolio_export_files, portfolio_model


class Hrefs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = []

    def handle_starttag(self, tag, attrs):
        self.values.extend(value for key, value in attrs if key == "href" and value)


@pytest.mark.parametrize("conflict", [False, True])
def test_real_html_evidence_links_resolve_after_shared_export(conflict):
    snapshots = []
    for project_id in ["alpha", "beta"]:
        content = "# Conhecimento compartilhado\n\nConteúdo completo.\n"
        if conflict and project_id == "beta":
            content += "Versão divergente explicitamente preservada.\n"
        snapshots.append(
            {
                "schema_version": 1,
                "project_id": project_id,
                "projects": [{"id": project_id, "title": "Mesmo nome"}],
                "documents": [
                    {
                        "path": "shared/wiki/concepts/shared--common.md",
                        "content": content,
                    },
                    {
                        "path": f"projects/{project_id}/wiki/index.md",
                        "content": f"# Projeto {project_id}\n",
                    },
                ],
                "tasks": [],
                "cycles": [],
            }
        )
    files = portfolio_export_files(portfolio_model(snapshots))
    for path, content in files.items():
        if not path.endswith(".html"):
            continue
        parser = Hrefs()
        parser.feed(content)
        for href in parser.values:
            if href.startswith(("#", "https:", "http:", "t3-thread:", "mailto:")):
                continue
            target = posixpath.normpath(
                posixpath.join(posixpath.dirname(path), unquote(href.split("#", 1)[0]))
            )
            assert target in files, (path, href)
