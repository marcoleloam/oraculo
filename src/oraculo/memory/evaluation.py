"""Synthetic F0 corpus and measurements against the real legacy bundle.

Question splits are fixed in this module. Ranker choice reads the tuning split
only. Acceptance questions are measured and never fed back into that choice.
No-answer cases stay out of recall and MRR. This module does not score a
generated natural-language answer.
"""

from __future__ import annotations

import math
import statistics
import time
from pathlib import Path
from typing import Any

from .context import Context
from .retrieval import (
    FROZEN_RANKER,
    RANKER_SIMPLICITY,
    TOKEN_ESTIMATOR,
    estimate_tokens,
)
from .schema import MemoryError, render_page

SEED = 20261101
DEFINITION_BASE_SHA = "cfa2b3564b03a60341c901018ee75a7f7a3e4bd4"
SCALES = (50, 500, 5000)
PROJECTS = ("atlas-harbor", "atlas-ridge")
LANGUAGE_REGRESSION = 0.02
DISTRACTORS_PER_PROJECT = 8

# Ranker identity is chosen from the tuning split at the smallest scale only.
# Larger scales confirm that choice; they do not replace it.
TUNING_SCALE = 50


def _code(project_id: str) -> str:
    if project_id == "atlas-harbor":
        return "HQ-17-CARMINE"
    if project_id == "atlas-ridge":
        return "RQ-44-INDIGO"
    raise MemoryError("unknown_synthetic_project")


def _bait_phrases(project_id: str) -> list[str]:
    """Phrases stored here and asked only against the other project."""
    if project_id == "atlas-harbor":
        return [
            "zarabatana3",
            "quilombo4",
            "blowgunprotocol3",
            "hammockprotocol4",
        ]
    return ["xilofone9", "oboe8", "xylophoneprotocol9", "oboeprotocol8"]


def _no_answer_queries(project_id: str) -> dict[tuple[str, str], str]:
    """Queries this project must not be able to support."""
    other = _bait_phrases(
        "atlas-ridge" if project_id == "atlas-harbor" else "atlas-harbor"
    )
    return {
        ("pt", "tuning"): other[0],
        ("pt", "acceptance"): other[1],
        ("en", "tuning"): other[2],
        ("en", "acceptance"): other[3],
    }


def page_specs(project_id: str) -> list[dict[str, Any]]:
    code = _code(project_id)
    hop = f"projects/{project_id}/wiki/concepts/z-hop-target.md"
    return [
        {
            "id": "direct",
            "title": "Código do cais norte",
            "aliases": [
                "código vigente do cais norte",
                "marca do cais norte",
                "north quay code",
                "standing north quay code",
                "mark of the north quay",
            ],
            "tags": ["cais"],
            "updated": "2026-04-01",
            "body": (
                f"O código vigente do cais norte é {code}.\n"
                f"The standing north quay code is {code}.\n"
                "Marca do cais norte e mark of the north quay nomeiam o mesmo fato."
            ),
        },
        {
            "id": "paraphrase",
            "title": "Plano carmim",
            "aliases": [
                "plano carmim do ancoradouro",
                "desenho carmim do ancoradouro",
                "crimson berth plan",
                "crimson berth drawing",
            ],
            "tags": ["berco"],
            "updated": "2026-04-02",
            "body": (
                "O plano carmim fixa o calado em quatro metros.\n"
                "The crimson berth plan fixes the draft at four meters."
            ),
        },
        {
            "id": "hop-source",
            "title": "Mapa do cais norte",
            "aliases": [
                "carta do cais norte",
                "north quay map",
                "north quay chart",
            ],
            "tags": ["mapa"],
            "updated": "2026-04-03",
            "body": (f"A carta aponta a medição sem copiar o número.\nVeja [[{hop}]]."),
        },
        {
            "id": "hop-target",
            "title": "Tabela de calado",
            "aliases": ["draft table"],
            "tags": ["calado"],
            "updated": "2026-04-03",
            "body": (
                "O calado operacional medido é quatro metros e meio.\n"
                "The measured operating draft is four and a half meters."
            ),
        },
        {
            "id": "farol-2026",
            "title": "Estado do farol em 2026",
            "aliases": ["lighthouse state in 2026"],
            "tags": ["farol"],
            "updated": "2026-06-01",
            "body": (
                "Em 2026 o farol opera em verde.\nIn 2026 the lighthouse shows green."
            ),
        },
        {
            "id": "farol-2023",
            "title": "Estado do farol em 2023",
            "aliases": ["lighthouse state in 2023"],
            "tags": ["farol"],
            "updated": "2023-06-01",
            "body": (
                "Em 2023 o farol opera em âmbar.\nIn 2023 the lighthouse shows amber."
            ),
        },
        {
            "id": "decision",
            "title": "Decisão do canal norte",
            "aliases": [
                "decisão registrada do canal norte",
                "north channel decision",
                "recorded north channel decision",
            ],
            "tags": ["decision"],
            "updated": "2026-05-01",
            "body": (
                "Decisão: o canal norte permanece aberto no inverno.\n"
                "Decision: the north channel stays open in winter."
            ),
        },
        {
            "id": "contra-closed",
            "title": "Canal sul fechado",
            "aliases": [
                "panorama do canal sul",
                "retrato do canal sul",
                "picture of the south channel",
                "view of the south channel",
                "south channel closed",
            ],
            "tags": ["canal"],
            "updated": "2026-02-01",
            "body": (
                "O canal sul está fechado para dragas.\n"
                "The south channel is closed to dredgers."
            ),
        },
        {
            "id": "contra-open",
            "title": "Canal sul aberto",
            "aliases": [
                "panorama do canal sul",
                "retrato do canal sul",
                "picture of the south channel",
                "view of the south channel",
                "south channel open",
            ],
            "tags": ["canal"],
            "updated": "2026-03-01",
            "body": (
                "O canal sul está aberto para barcaças.\n"
                "The south channel is open to barges."
            ),
        },
        {
            "id": "bait",
            "title": "Isca lexical isolada",
            "aliases": ["isolated lexical bait"],
            "tags": ["isca"],
            "updated": "2026-01-01",
            "body": "\n".join(_bait_phrases(project_id)),
        },
    ]


def _questions_for(project_id: str) -> list[dict[str, Any]]:
    code = _code(project_id)
    direct_pt = f"O código vigente do cais norte é {code}."
    direct_en = f"The standing north quay code is {code}."
    rows = [
        ("direct", "pt", "tuning", "código do cais norte", ["direct"], [direct_pt]),
        (
            "direct",
            "pt",
            "acceptance",
            "código vigente do cais norte",
            ["direct"],
            [direct_pt],
        ),
        (
            "direct",
            "pt",
            "acceptance",
            "marca do cais norte",
            ["direct"],
            [direct_pt],
        ),
        ("direct", "en", "tuning", "north quay code", ["direct"], [direct_en]),
        (
            "direct",
            "en",
            "acceptance",
            "standing north quay code",
            ["direct"],
            [direct_en],
        ),
        (
            "direct",
            "en",
            "acceptance",
            "mark of the north quay",
            ["direct"],
            [direct_en],
        ),
        (
            "paraphrase",
            "pt",
            "tuning",
            "plano carmim do ancoradouro",
            ["paraphrase"],
            ["O plano carmim fixa o calado em quatro metros."],
        ),
        (
            "paraphrase",
            "pt",
            "acceptance",
            "desenho carmim do ancoradouro",
            ["paraphrase"],
            ["O plano carmim fixa o calado em quatro metros."],
        ),
        (
            "paraphrase",
            "en",
            "tuning",
            "crimson berth plan",
            ["paraphrase"],
            ["The crimson berth plan fixes the draft at four meters."],
        ),
        (
            "paraphrase",
            "en",
            "acceptance",
            "crimson berth drawing",
            ["paraphrase"],
            ["The crimson berth plan fixes the draft at four meters."],
        ),
        (
            "multihop",
            "pt",
            "tuning",
            "mapa do cais norte",
            ["hop-source", "hop-target"],
            ["O calado operacional medido é quatro metros e meio."],
        ),
        (
            "multihop",
            "pt",
            "acceptance",
            "carta do cais norte",
            ["hop-source", "hop-target"],
            ["O calado operacional medido é quatro metros e meio."],
        ),
        (
            "multihop",
            "en",
            "tuning",
            "north quay map",
            ["hop-source", "hop-target"],
            ["The measured operating draft is four and a half meters."],
        ),
        (
            "multihop",
            "en",
            "acceptance",
            "north quay chart",
            ["hop-source", "hop-target"],
            ["The measured operating draft is four and a half meters."],
        ),
        (
            "temporal",
            "pt",
            "tuning",
            "estado do farol em 2026",
            ["farol-2026"],
            ["Em 2026 o farol opera em verde."],
        ),
        (
            "temporal",
            "pt",
            "acceptance",
            "estado do farol em 2023",
            ["farol-2023"],
            ["Em 2023 o farol opera em âmbar."],
        ),
        (
            "temporal",
            "en",
            "tuning",
            "lighthouse state in 2026",
            ["farol-2026"],
            ["In 2026 the lighthouse shows green."],
        ),
        (
            "temporal",
            "en",
            "acceptance",
            "lighthouse state in 2023",
            ["farol-2023"],
            ["In 2023 the lighthouse shows amber."],
        ),
        (
            "decision",
            "pt",
            "tuning",
            "decisão do canal norte",
            ["decision"],
            ["Decisão: o canal norte permanece aberto no inverno."],
        ),
        (
            "decision",
            "pt",
            "acceptance",
            "decisão registrada do canal norte",
            ["decision"],
            ["Decisão: o canal norte permanece aberto no inverno."],
        ),
        (
            "decision",
            "en",
            "tuning",
            "north channel decision",
            ["decision"],
            ["Decision: the north channel stays open in winter."],
        ),
        (
            "decision",
            "en",
            "acceptance",
            "recorded north channel decision",
            ["decision"],
            ["Decision: the north channel stays open in winter."],
        ),
        (
            "contradiction",
            "pt",
            "tuning",
            "panorama do canal sul",
            ["contra-closed", "contra-open"],
            [
                "O canal sul está fechado para dragas.",
                "O canal sul está aberto para barcaças.",
            ],
        ),
        (
            "contradiction",
            "pt",
            "acceptance",
            "retrato do canal sul",
            ["contra-closed", "contra-open"],
            [
                "O canal sul está fechado para dragas.",
                "O canal sul está aberto para barcaças.",
            ],
        ),
        (
            "contradiction",
            "en",
            "tuning",
            "picture of the south channel",
            ["contra-closed", "contra-open"],
            [
                "The south channel is closed to dredgers.",
                "The south channel is open to barges.",
            ],
        ),
        (
            "contradiction",
            "en",
            "acceptance",
            "view of the south channel",
            ["contra-closed", "contra-open"],
            [
                "The south channel is closed to dredgers.",
                "The south channel is open to barges.",
            ],
        ),
    ]
    # The extra direct acceptance wording shares its type prefix with the first
    # acceptance row. Disambiguate ids by query slug order below.
    questions = []
    seen: dict[str, int] = {}
    for kind, language, split, query, relevant, spans in rows:
        seen[f"{kind}-{language}-{split}"] = (
            seen.get(f"{kind}-{language}-{split}", 0) + 1
        )
        suffix = seen[f"{kind}-{language}-{split}"]
        questions.append(
            {
                "id": f"{project_id}-{kind}-{language}-{split}-{suffix}",
                "split": split,
                "language": language,
                "type": kind,
                "project_id": project_id,
                "query": query,
                "relevant_ids": relevant,
                "relevant_paths": [
                    f"projects/{project_id}/wiki/concepts/z-{page_id}.md"
                    for page_id in relevant
                ],
                "evidence": [
                    {
                        "page_id": relevant[-1] if kind != "contradiction" else None,
                        "path": (
                            f"projects/{project_id}/wiki/concepts/z-{relevant[-1]}.md"
                            if kind != "contradiction"
                            else None
                        ),
                        "span": span,
                    }
                    for span in spans
                ],
            }
        )
    # Contradiction evidence lives on both pages. Patch those rows.
    for question in questions:
        if question["type"] != "contradiction":
            continue
        spans = [item["span"] for item in question["evidence"]]
        pages = ["contra-closed", "contra-open"]
        question["evidence"] = [
            {
                "page_id": page_id,
                "path": f"projects/{project_id}/wiki/concepts/z-{page_id}.md",
                "span": span,
            }
            for page_id, span in zip(pages, spans, strict=True)
        ]
    no_answer = _no_answer_queries(project_id)
    for (language, split), query in no_answer.items():
        questions.append(
            {
                "id": f"{project_id}-no-answer-{language}-{split}-1",
                "split": split,
                "language": language,
                "type": "no-answer",
                "project_id": project_id,
                "query": query,
                "relevant_ids": [],
                "relevant_paths": [],
                "evidence": [],
            }
        )
    return questions


def question_bank() -> list[dict[str, Any]]:
    bank = [question for project in PROJECTS for question in _questions_for(project)]
    identifiers = [question["id"] for question in bank]
    if len(identifiers) != len(set(identifiers)):
        raise MemoryError("duplicate_question_id")
    return bank


def build_manifest(base_sha: str = DEFINITION_BASE_SHA) -> dict[str, Any]:
    if not isinstance(base_sha, str) or len(base_sha) < 7:
        raise MemoryError("invalid_base_sha")
    return {
        "schema": "second-brain-f0-v1",
        "base_sha": base_sha,
        "seed": SEED,
        "scales": list(SCALES),
        "projects": list(PROJECTS),
        "shared": "sanitized-synthetic",
        "token_estimator": TOKEN_ESTIMATOR,
        "homonym_display_name": "Atlas",
        "questions": question_bank(),
        "notes": [
            "Synthetic pages only. Labels are the planted evidence spans.",
            "Tuning and acceptance are disjoint question ids.",
            "No-answer questions are excluded from recall and MRR.",
        ],
    }


def _render_fact(project_id: str, spec: dict[str, Any]) -> str:
    body = f"# {spec['title']}\n\n{spec['body'].rstrip()}\n"
    return render_page(
        {
            "type": "concept",
            "created": "2026-01-15",
            "updated": spec["updated"],
            "sources": [],
            "tags": spec["tags"],
            "aliases": spec["aliases"],
            "reviewed": False,
            "page_id": f"{project_id}--{spec['id']}",
            "project_id": project_id,
            "schema_version": 1,
        },
        body,
    )


def _distractor_text(project_id: str) -> str:
    queries = [
        question["query"]
        for question in _questions_for(project_id)
        if question["type"] != "no-answer"
    ]
    # Keep each query once so distractors tie with facts on distinct terms
    # without copying the unique evidence span.
    unique = list(dict.fromkeys(queries))
    return (
        "# Nota operacional\n\n"
        "Rascunho que repete termos de consulta e não contém o fato rotulado.\n\n"
        + "\n".join(unique)
        + "\n"
    )


def fixed_page_count() -> int:
    facts = len(page_specs(PROJECTS[0])) * len(PROJECTS)
    distractors = DISTRACTORS_PER_PROJECT * len(PROJECTS)
    shared_notes = 2
    indexes = len(PROJECTS) + 1
    return facts + distractors + shared_notes + indexes


def materialize(root: Path, scale: int, seed: int = SEED) -> dict[str, Any]:
    if scale not in SCALES:
        raise MemoryError("unsupported_corpus_scale")
    if seed != SEED:
        raise MemoryError("unexpected_corpus_seed")
    fixed = fixed_page_count()
    fillers = scale - fixed
    if fillers < 0:
        raise MemoryError(f"scale_below_fixed_pages:{scale}")
    root.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    def write(relative: str, content: str) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(relative)

    for project_id in PROJECTS:
        marker = f"projects/{project_id}/.oraculo-project.json"
        (root / marker).parent.mkdir(parents=True, exist_ok=True)
        (root / marker).write_text(
            '{"project_id": "' + project_id + '"}', encoding="utf-8"
        )
        write(
            f"projects/{project_id}/wiki/index.md",
            "# Projeto Atlas\n\nÍndice sintético. Nomes iguais não unem projetos.\n",
        )
        for spec in page_specs(project_id):
            write(
                f"projects/{project_id}/wiki/concepts/z-{spec['id']}.md",
                _render_fact(project_id, spec),
            )
        distractor = _distractor_text(project_id)
        for index in range(DISTRACTORS_PER_PROJECT):
            write(
                f"projects/{project_id}/wiki/concepts/a-{index:04d}.md",
                render_page(
                    {
                        "type": "concept",
                        "created": "2026-01-15",
                        "updated": "2026-01-15",
                        "sources": [],
                        "tags": ["rascunho"],
                        "aliases": [f"nota operacional {index:04d}"],
                        "reviewed": False,
                        "page_id": f"{project_id}--distractor-{index:04d}",
                        "project_id": project_id,
                        "schema_version": 1,
                    },
                    distractor,
                ),
            )
    (root / "shared/.oraculo-project.json").parent.mkdir(parents=True, exist_ok=True)
    (root / "shared/.oraculo-project.json").write_text(
        '{"project_id": "shared"}', encoding="utf-8"
    )
    write(
        "shared/wiki/index.md",
        "# Notas sanitizadas\n\nTexto geral sem fatos de projeto.\n",
    )
    write(
        "shared/wiki/concepts/shared--registro.md",
        render_page(
            {
                "type": "concept",
                "created": "2026-01-15",
                "updated": "2026-01-15",
                "sources": [],
                "tags": ["sanitizado"],
                "aliases": ["nota geral"],
                "reviewed": True,
                "page_id": "shared--registro",
                "project_id": "shared",
                "schema_version": 1,
            },
            (
                "# Nota geral\n\n"
                "Um registro geral guarda a opção escolhida e a data, "
                "sem nomes de clientes, identificadores ou segredos.\n"
            ),
        ),
    )
    write(
        "shared/wiki/concepts/shared--homonimos.md",
        render_page(
            {
                "type": "concept",
                "created": "2026-01-15",
                "updated": "2026-01-15",
                "sources": [],
                "tags": ["sanitizado"],
                "aliases": ["homonimos"],
                "reviewed": True,
                "page_id": "shared--homonimos",
                "project_id": "shared",
                "schema_version": 1,
            },
            (
                "# Homônimos\n\n"
                "O mesmo nome em dois namespaces não autoriza leitura cruzada.\n"
            ),
        ),
    )
    harbor_fillers = fillers // 2
    ridge_fillers = fillers - harbor_fillers
    for project_id, count in (
        ("atlas-harbor", harbor_fillers),
        ("atlas-ridge", ridge_fillers),
    ):
        for index in range(count):
            write(
                f"projects/{project_id}/wiki/concepts/m-{index:04d}.md",
                render_page(
                    {
                        "type": "concept",
                        "created": "2026-01-15",
                        "updated": "2026-01-15",
                        "sources": [],
                        "tags": ["apontamento"],
                        "aliases": [],
                        "reviewed": False,
                        "page_id": f"{project_id}--filler-{index:04d}",
                        "project_id": project_id,
                        "schema_version": 1,
                    },
                    (
                        f"# Apontamento {seed}-{index:04d}\n\n"
                        "Ficha administrativa sintética sem fato rotulado.\n"
                    ),
                ),
            )
    if len(written) != scale:
        raise MemoryError(f"scale_page_count:{len(written)}")
    return {"pages": len(written), "seed": seed, "scale": scale}


def recall_at_10(ranked: list[str], relevant: list[str]) -> float:
    if not relevant:
        raise MemoryError("recall_requires_relevant_pages")
    found = set(ranked[:10]).intersection(relevant)
    return len(found) / len(relevant)


def reciprocal_rank(ranked: list[str], relevant: list[str]) -> float:
    if not relevant:
        raise MemoryError("mrr_requires_relevant_pages")
    relevant_set = set(relevant)
    for index, path in enumerate(ranked, start=1):
        if path in relevant_set:
            return 1.0 / index
    return 0.0


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        raise MemoryError("empty_percentile")
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return float(ordered[index])


def _selected_bytes(bundle: dict[str, Any]) -> int:
    return sum(
        len(document["content"].encode("utf-8")) for document in bundle["documents"]
    )


def _metric_ranking(
    bundle: dict[str, Any], ranked: list[str]
) -> tuple[list[str], str]:
    """Score the candidate list, not the fixed index preamble.

    ``legacy_bundle`` sets ``index_first`` and prefixes both wiki indexes.
    Those pages are not ranked hits. Recall and MRR use the remainder.
    """

    if bundle.get("index_first") is True:
        return (
            [path for path in ranked if not path.endswith("/wiki/index.md")],
            "candidates_without_index_preamble",
        )
    return list(ranked), "bundle_order"


def measure_question(
    vault: Path, question: dict[str, Any], *, ranker: str | None
) -> dict[str, Any]:
    """Time one real call. ``ranker is None`` uses ``Context.bundle`` legacy."""
    started = time.perf_counter()
    context = Context(vault)
    if ranker is None:
        bundle = context.bundle(question["project_id"], question["query"], 20)
        if "selection" in bundle:
            raise MemoryError("baseline_must_call_legacy_bundle")
        ranked = [document["path"] for document in bundle["documents"]]
        abstained = False
        pages_read = None
        bytes_read = None
    else:
        bundle = context.bundle(
            question["project_id"],
            question["query"],
            20,
            mode="budgeted",
            strict_abstain=True,
            ranker=ranker,
        )
        selection = bundle["selection"]
        ranked = list(selection["ranked_paths"])
        abstained = bool(selection["abstained"])
        pages_read = selection["pages_read"]
        bytes_read = selection["bytes_read"]
        if selection["tokens"]["detail"] > selection["budget"]["detail_tokens"]:
            raise MemoryError("detail_budget_exceeded")
        if selection["tokens"]["brief"] > selection["budget"]["brief_tokens"]:
            raise MemoryError("brief_budget_exceeded")
    ranked, ranking_convention = _metric_ranking(bundle, ranked)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    relevant = question["relevant_paths"]
    record: dict[str, Any] = {
        "id": question["id"],
        "type": question["type"],
        "language": question["language"],
        "split": question["split"],
        "project_id": question["project_id"],
        "abstained": abstained,
        "ranked_paths": ranked,
        "ranking_convention": ranking_convention,
        "selected_pages": len(bundle["documents"]),
        "selected_bytes": _selected_bytes(bundle),
        "document_paths": [document["path"] for document in bundle["documents"]],
        "pages_read": pages_read,
        "bytes_read": bytes_read,
        "latency_ms": elapsed_ms,
        "false_hit": (not relevant) and (not abstained) and bool(bundle["documents"]),
        "relevant_paths": list(relevant),
    }
    if relevant:
        record["recall_at_10"] = recall_at_10(ranked, relevant)
        record["reciprocal_rank"] = reciprocal_rank(ranked, relevant)
    return record


def _ranking_convention(records: list[dict[str, Any]]) -> str | list[str] | None:
    conventions = []
    for row in records:
        convention = row.get("ranking_convention")
        if convention not in conventions:
            conventions.append(convention)
    if not conventions:
        return None
    if len(conventions) == 1:
        return conventions[0]
    return conventions


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return float(sum(values) / len(values))


def aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    answerable = [row for row in records if "recall_at_10" in row]
    unanswered = [row for row in records if "recall_at_10" not in row]
    negatives = [row["id"] for row in answerable if row["recall_at_10"] == 0]

    def pack(rows: list[dict[str, Any]]) -> dict[str, Any]:
        recalls = [row["recall_at_10"] for row in rows]
        ranks = [row["reciprocal_rank"] for row in rows]
        selected = [float(row["selected_bytes"]) for row in rows]
        latencies = [row["latency_ms"] for row in rows]
        detail_hits = [
            1.0
            if row.get("document_paths") is not None
            and set(row["relevant_paths"]).issubset(row["document_paths"])
            else 0.0
            for row in rows
            if row.get("document_paths") is not None
        ]
        result = {
            "count": len(rows),
            "recall_at_10": _mean(recalls),
            "detail_hit_rate": _mean(detail_hits),
            "mrr": _mean(ranks),
            "median_selected_bytes": (
                float(statistics.median(selected)) if selected else None
            ),
            "median_selected_pages": (
                float(statistics.median([row["selected_pages"] for row in rows]))
                if rows
                else None
            ),
            "negative_results": [row["id"] for row in rows if row["recall_at_10"] == 0],
        }
        if latencies:
            result["latency_ms_p50"] = percentile(latencies, 0.50)
            result["latency_ms_p95"] = percentile(latencies, 0.95)
        return result

    by_language = {}
    by_type = {}
    for language in ("pt", "en"):
        by_language[language] = pack(
            [row for row in answerable if row["language"] == language]
        )
    for kind in (
        "direct",
        "paraphrase",
        "multihop",
        "temporal",
        "decision",
        "contradiction",
    ):
        by_type[kind] = pack([row for row in answerable if row["type"] == kind])
    no_answer_bytes = [float(row["selected_bytes"]) for row in unanswered]
    return {
        "count": len(records),
        "answerable": pack(answerable),
        "recall_at_10": pack(answerable)["recall_at_10"],
        "mrr": pack(answerable)["mrr"],
        "median_selected_bytes": pack(answerable)["median_selected_bytes"],
        "median_selected_pages": pack(answerable)["median_selected_pages"],
        "latency_ms_p50": pack(answerable).get("latency_ms_p50"),
        "latency_ms_p95": pack(answerable).get("latency_ms_p95"),
        "negative_results": negatives,
        "ranking_convention": _ranking_convention(records),
        "by_language": by_language,
        "by_type": by_type,
        "no_answer": {
            "count": len(unanswered),
            "abstained": sum(1 for row in unanswered if row["abstained"]),
            "false_hits": sum(1 for row in unanswered if row["false_hit"]),
            "median_selected_bytes": (
                float(statistics.median(no_answer_bytes)) if no_answer_bytes else None
            ),
        },
    }


def language_regressed(
    baseline: dict[str, Any], candidate: dict[str, Any]
) -> list[str]:
    regressed = []
    for language in ("pt", "en"):
        base = baseline["by_language"][language]["recall_at_10"]
        cand = candidate["by_language"][language]["recall_at_10"]
        if base is None or cand is None:
            continue
        if cand < base - LANGUAGE_REGRESSION:
            regressed.append(language)
    return regressed


def meets_gate(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """F1 gate. Descriptive only: it does not modify weights or labels."""
    base = baseline["recall_at_10"]
    cand = candidate["recall_at_10"]
    regressed = language_regressed(baseline, candidate)
    if base is None or cand is None:
        return {
            "passed": False,
            "reason": "missing_recall",
            "language_regression": regressed,
        }
    if base > 0.90:
        shrunk = (
            candidate["median_selected_bytes"]
            <= baseline["median_selected_bytes"] * 0.70
        )
        preserved = cand + 1e-12 >= base
        return {
            "passed": bool(preserved and shrunk and not regressed),
            "reason": "baseline_above_0.90_preserve_quality_and_shrink_context",
            "quality_preserved": preserved,
            "median_context_reduced_30pct": shrunk,
            "language_regression": regressed,
            "baseline_recall_at_10": base,
            "candidate_recall_at_10": cand,
        }
    gained = cand + 1e-12 >= base + 0.10 or cand + 1e-12 >= 0.90
    return {
        "passed": bool(gained and not regressed),
        "reason": "recall_plus_10pp_or_at_least_0.90",
        "recall_gain": cand - base,
        "language_regression": regressed,
        "baseline_recall_at_10": base,
        "candidate_recall_at_10": cand,
    }


def choose_ranker(
    split: str,
    baseline: dict[str, Any],
    variants: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if split != "tuning":
        raise MemoryError("ranker_choice_requires_tuning_split")
    comparisons = []
    selected = None
    for name in RANKER_SIMPLICITY:
        if name not in variants:
            raise MemoryError("missing_ranker_variant")
        gate = meets_gate(baseline, variants[name])
        comparisons.append({"ranker": name, **gate})
        if gate["passed"] and selected is None:
            selected = name
    return {
        "split": split,
        "scale": TUNING_SCALE,
        "selected": selected,
        "frozen_constant": FROZEN_RANKER,
        "constant_matches": selected == FROZEN_RANKER,
        "comparisons": comparisons,
        "pre_registered": {
            "field_weights": {"title": 5, "alias": 4, "tag": 3, "body": 1},
            "bm25": {"k1": 1.2, "b": 0.75},
            "language_regression_max_drop": LANGUAGE_REGRESSION,
            "choice_scale": TUNING_SCALE,
        },
    }


def evaluate_split(
    vault: Path, questions: list[dict[str, Any]], *, ranker: str | None
) -> dict[str, Any]:
    records = [
        measure_question(vault, question, ranker=ranker) for question in questions
    ]
    summary = aggregate(records)
    summary["method"] = "legacy_bundle" if ranker is None else ranker
    return summary


def corpus_scope_stats(vault: Path) -> dict[str, dict[str, int]]:
    stats = {}
    for project_id in PROJECTS:
        files = Context(vault).files(project_id)
        stats[project_id] = {
            "pages_read": len(files),
            "bytes_read": sum(
                len(content.encode("utf-8")) for content in files.values()
            ),
            "detail_tokens_estimate": sum(
                estimate_tokens(content) for content in files.values()
            ),
        }
    return stats


def run_benchmark(
    work: Path,
    *,
    scales: tuple[int, ...] = SCALES,
    base_sha: str = DEFINITION_BASE_SHA,
) -> dict[str, Any]:
    """Measure legacy ``Context.bundle`` and the frozen budgeted ranker.

    Variant comparison uses the tuning split at scale 50 before any acceptance
    number is computed.
    """
    manifest = build_manifest(base_sha)
    questions = manifest["questions"]
    tuning = [question for question in questions if question["split"] == "tuning"]
    acceptance = [
        question for question in questions if question["split"] == "acceptance"
    ]
    choice_vault = work / f"scale-{TUNING_SCALE}"
    materialize(choice_vault, TUNING_SCALE)
    baseline_tuning = evaluate_split(choice_vault, tuning, ranker=None)
    variants = {
        name: evaluate_split(choice_vault, tuning, ranker=name)
        for name in RANKER_SIMPLICITY
    }
    choice = choose_ranker("tuning", baseline_tuning, variants)
    measured_ranker = choice["selected"] or FROZEN_RANKER
    scale_reports = []
    for scale in scales:
        vault = choice_vault if scale == TUNING_SCALE else work / f"scale-{scale}"
        if scale != TUNING_SCALE:
            materialize(vault, scale)
        scope = corpus_scope_stats(vault)
        scale_reports.append(
            {
                "scale": scale,
                "scope": scope,
                "tuning": {
                    "legacy_bundle": (
                        baseline_tuning
                        if scale == TUNING_SCALE
                        else evaluate_split(vault, tuning, ranker=None)
                    ),
                    "budgeted": evaluate_split(vault, tuning, ranker=measured_ranker),
                },
                "acceptance": {
                    "legacy_bundle": evaluate_split(vault, acceptance, ranker=None),
                    "budgeted": evaluate_split(
                        vault, acceptance, ranker=measured_ranker
                    ),
                },
            }
        )
        acceptance_gate = meets_gate(
            scale_reports[-1]["acceptance"]["legacy_bundle"],
            scale_reports[-1]["acceptance"]["budgeted"],
        )
        tuning_gate = meets_gate(
            scale_reports[-1]["tuning"]["legacy_bundle"],
            scale_reports[-1]["tuning"]["budgeted"],
        )
        scale_reports[-1]["acceptance_gate"] = acceptance_gate
        scale_reports[-1]["tuning_gate"] = tuning_gate
    return {
        "manifest": {
            "schema": manifest["schema"],
            "base_sha": manifest["base_sha"],
            "seed": manifest["seed"],
            "question_count": len(questions),
            "tuning": len(tuning),
            "acceptance": len(acceptance),
        },
        "choice": choice,
        "tuning_variants_scale_50": {
            "legacy_bundle": baseline_tuning,
            **variants,
        },
        "measured_ranker": measured_ranker,
        "scales": scale_reports,
    }
