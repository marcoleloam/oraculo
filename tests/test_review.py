import json
from html import unescape

import pytest
from test_memory_schema import page, proposal, publisher

from oraculo.memory import ReviewManager
from oraculo.memory.schema import digest, links, markdown_targets


def test_concurrent_review_and_publish_conflict(tmp_path):
    pub = publisher(tmp_path)
    pub.publish(proposal())
    manager = ReviewManager(pub)
    opened = manager.open("alpha")
    review_file = next((pub.vault / opened["wiki_folder"]).glob("concepts/*.md"))
    original_review = review_file.read_text()
    review_file.write_text(
        original_review.replace("Decisão registrada.", "Revisão humana.")
    )
    canonical = pub.vault / proposal()["changes"][0]["path"]
    pub.publish(
        proposal(
            page(body="Nova versão do serviço."),
            operation="service",
            base=digest(canonical.read_bytes()),
        )
    )
    result = manager.close(opened["session_id"])
    assert result["state"] == "conflict"
    assert "Nova versão do serviço." in canonical.read_text()
    assert "Revisão humana." in review_file.read_text()


def test_import_translate_links_and_preserve_post_seal(tmp_path):
    pub = publisher(tmp_path)
    raw = pub.snapshot("alpha", "Evidence")
    pub.publish(proposal(page(sources=[f"[[{raw}]]"])))
    manager = ReviewManager(pub)
    opened = manager.open("alpha", session_id="request1")
    assert manager.open("alpha", session_id="request1") == opened
    review_file = next((pub.vault / opened["wiki_folder"]).glob("concepts/*.md"))
    assert "reviews/request1/raw/request1--alpha--" in review_file.read_text()
    review_file.write_text(
        review_file.read_text().replace("Decisão registrada.", "Revisão humana.")
    )
    result = manager.close("request1")
    assert result["state"] == "imported"
    canonical = pub.vault / proposal()["changes"][0]["path"]
    assert "reviews/" not in canonical.read_text()
    assert raw.removesuffix(".md") in canonical.read_text()
    assert manager.close("request1") == result
    review_file.write_text(review_file.read_text() + "\nedição posterior")
    assert manager.close("request1")["reason"] == "post_seal_changes"
    assert "edição posterior" not in canonical.read_text()


def test_reviewed_protection_and_unique_sessions(tmp_path):
    pub = publisher(tmp_path)
    pub.publish(proposal(page(reviewed=True)))
    manager = ReviewManager(pub)
    one, two = manager.open("alpha"), manager.open("alpha")
    a = next((pub.vault / one["wiki_folder"]).glob("concepts/*.md"))
    b = next((pub.vault / two["wiki_folder"]).glob("concepts/*.md"))
    assert a.name != b.name
    a.write_text(a.read_text() + "\nAdição que o plugin permitiria.")
    assert manager.close(one["session_id"])["state"] == "conflict"
    assert "Adição" not in b.read_text()


def test_new_review_page_has_immutable_source(tmp_path):
    pub = publisher(tmp_path)
    manager = ReviewManager(pub)
    opened = manager.open("alpha")
    target = pub.vault / opened["wiki_folder"] / "concepts/new-note.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page(name="new-note", body="Nova nota humana."))
    result = manager.close(opened["session_id"])
    assert result["state"] == "imported"
    canonical = pub.vault / "projects/alpha/wiki/concepts/alpha--new-note.md"
    assert "projects/alpha/raw/" in canonical.read_text()
    assert len(list((pub.vault / "projects/alpha/raw").glob("*.md"))) == 1


def test_close_recovers_after_commit_before_receipt(tmp_path, monkeypatch):
    pub = publisher(tmp_path)
    pub.publish(proposal())
    manager = ReviewManager(pub)
    opened = manager.open("alpha")
    target = next((pub.vault / opened["wiki_folder"]).glob("concepts/*.md"))
    target.write_text(target.read_text() + "\nMudança revisada.")
    original = manager._save

    def crash(path, session):
        if session["state"] == "imported":
            raise OSError("lost receipt")
        original(path, session)

    monkeypatch.setattr(manager, "_save", crash)
    import pytest

    with pytest.raises(OSError):
        manager.close(opened["session_id"])
    head = pub.git.head()
    monkeypatch.setattr(manager, "_save", original)
    assert manager.close(opened["session_id"])["state"] == "imported"
    assert pub.git.head() == head


def test_import_then_reopen_raw_does_not_link_to_previous_review(tmp_path):
    pub = publisher(tmp_path)
    pub.publish(proposal())
    manager = ReviewManager(pub)
    first = manager.open("alpha", "first")
    destination = next((pub.vault / first["wiki_folder"]).glob("concepts/*.md"))
    target = destination.relative_to(pub.vault).as_posix().removesuffix(".md")
    authored = destination.parent / "new-note.md"
    original = page(
        name="new-note",
        sources=[f"[[{target}]]"],
        body=f"Leia [[{target}#Decisão|Fonte]] e [fonte]({target}.md).",
    )
    authored.write_text(original)
    assert manager.close("first")["state"] == "imported"
    raw = pub.vault / f"projects/alpha/raw/{digest(original)}.md"
    assert raw.read_bytes() == original.encode()
    first_files = manager._capture("first")
    head = pub.git.head()

    second = manager.open("alpha", "second")
    projected = next((pub.vault / "reviews/second/raw").glob("*.md"))
    rendered = projected.read_text()
    assert not links(rendered)
    assert not markdown_targets(rendered)
    # Every original character remains available as displayed text, not a link.
    assert unescape(rendered.split("<pre>", 1)[1].rsplit("</pre>", 1)[0]) == original
    assert digest(original) in rendered
    assert not rendered.startswith("---\n")
    wiki = "\n".join(
        p.read_text() for p in (pub.vault / second["wiki_folder"]).rglob("*.md")
    )
    assert "reviews/first/" not in wiki
    assert "reviews/second/raw/" in wiki
    assert manager.close("second")["state"] == "imported"
    assert raw.read_bytes() == original.encode()
    assert pub.git.head() == head
    assert manager._capture("first") == first_files


@pytest.mark.parametrize("scope", ["alpha", "shared"])
def test_raw_projection_is_literal_and_remains_protected(tmp_path, scope):
    pub = publisher(tmp_path)
    original = (
        '---\naliases: ["Duplicated alias"]\n'
        "source_note: reviews/old/wiki/private.md\n"
        'sources: ["[[projects/beta/wiki/private]]"]\n---\n'
        "[[reviews/old/wiki/private#Heading|alias]] ![[secret]]\n"
        "[relative](../secret.md) ![embed](projects/beta/raw/private.md)\n"
        "[reference][id]\n[id]: obsidian://open?vault=other\n"
        '<a href="obsidian://open?file=private">jump</a>\n'
        '</pre><iframe src="file:///private"></iframe>\n'
        "```\n[[link-in-code]]\n```\n& &#91; <script>alert(1)</script>\n"
    )
    raw = pub.snapshot(scope, original)
    manager = ReviewManager(pub)
    opened = manager.open("alpha", "literal")
    metadata = json.loads((manager.sessions / "literal/session.json").read_text())
    projected = pub.vault / metadata["mapping"][raw]
    text = projected.read_text()
    assert text.count("<pre>") == text.count("</pre>") == 1
    assert not links(text) and not markdown_targets(text)
    assert "<iframe" not in text and "<a " not in text and "<script" not in text
    assert unescape(text.split("<pre>", 1)[1].rsplit("</pre>", 1)[0]) == original
    assert manager.open("alpha", "literal") == opened
    projected.write_text(text + "\nAlteração indevida")
    result = manager.close("literal")
    assert result["state"] == "conflict"
    assert result["findings"] == ["review_raw_immutable"]
    assert (pub.vault / raw).read_bytes() == original.encode()
