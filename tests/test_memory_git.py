from test_memory_schema import page, proposal, publisher

from oraculo.memory.schema import digest


def test_commit_and_restore_copy(tmp_path):
    pub = publisher(tmp_path)
    unrelated = pub.vault / "human.txt"
    unrelated.write_text("human staged work")
    pub.git.run("add", "--", "human.txt")
    receipt = pub.publish(proposal())
    assert (
        "human.txt"
        not in pub.git.run("ls-tree", "-r", "--name-only", receipt["commit"]).stdout
    )
    path = pub.vault / proposal()["changes"][0]["path"]
    old = path.read_text()
    pub.publish(proposal(page(body="Atualização"), operation="next", base=digest(old)))
    restored = tmp_path / "restore-copy"
    pub.git.restore_copy(receipt["commit"], restored)
    assert (restored / proposal()["changes"][0]["path"]).read_text() == old
    assert "Atualização" in path.read_text()
    assert "human.txt" in pub.git.run("diff", "--cached", "--name-only").stdout
