"""Pure, bounded human views; technical envelopes retain their full identity."""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath

from oraculo.protocol import CONTROL_MARKER, canonical_json

PHASES = {
    "received": ("Pedido recebido", "O Oráculo está preparando a tarefa."),
    "project_resolved": ("Projeto identificado", "Preparando memória e execução."),
    "memory_read": (
        "Consultando memória",
        "Buscando fontes e aprendizados do projeto.",
    ),
    "launch_pending": ("Aguardando execução", "A execução está sendo preparada."),
    "launch_acknowledged": (
        "Execução preparada",
        "Aguardando confirmação do ambiente de trabalho.",
    ),
    "executing": ("Em execução", "O agente está trabalhando na tarefa."),
    "validating": ("Em revisão", "Verificando resultado e evidências da execução."),
    "validation_submitted": (
        "Aguardando revisão",
        "O relatório foi entregue; o término ainda será confirmado.",
    ),
    "validation_recovered": (
        "Relatório recuperado",
        "Aguardando revalidação da execução.",
    ),
    "memory_pending": (
        "Registrando memória",
        "Salvando o resultado e os aprendizados.",
    ),
    "awaiting_approval": (
        "Aguardando aprovação",
        "O planejamento aguarda decisão da origem.",
    ),
    "plan_approved": (
        "Planejamento aprovado",
        "A origem pode encaminhar a implementação.",
    ),
    "pr_pending": (
        "Preparando publicação",
        "Trabalho e memória têm etapas distintas de publicação.",
    ),
    "pr_link_pending": (
        "Vinculando publicação",
        "Confirmando o vínculo da revisão com a conversa.",
    ),
    "pr_memory_pending": (
        "Registrando publicação",
        "Confirmando a memória da publicação.",
    ),
    "pr_open": ("Publicação em revisão", "A solicitação de integração está aberta."),
    "pr_waiting": (
        "Aguardando verificações",
        "Aguardando checks ou revisão da publicação.",
    ),
    "pr_ready": (
        "Pronta para integração",
        "Os gates observados permitem a próxima etapa.",
    ),
    "merged": (
        "Integração registrada",
        "A integração foi observada; implantação é uma etapa separada.",
    ),
    "settled": ("Encerrada", "O encerramento foi registrado."),
    "cancelled": ("Cancelada", "O cancelamento foi registrado."),
    "failed": ("Falhou", "O resultado não foi aprovado."),
    "stopped": ("Interrompida", "A execução foi interrompida."),
    "blocked": ("Bloqueada", "Uma condição impede a próxima etapa."),
}


BLOCKERS = {
    "memory_write_unconfirmed": "O salvamento da memória ainda não foi confirmado.",
    "terminal_execution_unproven": "O término da execução ainda não foi comprovado.",
    "terminal_execution_or_descendants_unproven": "Há execução ou revisão cujo término precisa ser confirmado.",
    "dirty_source_commit_selection_required": "A origem contém alterações que precisam de uma seleção explícita de commit.",
    "validated_commit_or_clean_workspace_required": "O commit e o ambiente precisam ser revalidados antes de concluir.",
    "managed_adapter_busy_retry_same_key": "O serviço está atendendo outra operação; a origem deve retomar a mesma intenção.",
}


def plain(value, limit=240) -> str:
    return " ".join(str(value or "").split())[:limit]


def markdown(value) -> str:
    # Quotes, links, HTML and embedded instructions stay literal display text.
    return re.sub(r"([\\`*_{}\[\]()#!|>~])", r"\\\1", html.escape(plain(value, 2000)))


def timestamp(value) -> str:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, timezone.utc).strftime(
                "%d/%m/%Y %H:%M UTC"
            )
        except (ValueError, OverflowError, OSError):
            return "desconhecida"
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if len(value) == 10:
                return parsed.strftime("%d/%m/%Y")
            if parsed.tzinfo is not None:
                return parsed.astimezone(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
        except ValueError:
            pass
    return "desconhecida"


def thread_url(value) -> str | None:
    return (
        "t3-thread://v1/" + value
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:%-]+", value)
        else None
    )


def task_view(task: dict) -> dict:
    request = task.get("request") or {}
    project = task.get("project") or {}
    title = plain(task.get("title") or request.get("title")) or "Tarefa do projeto"
    name = plain(task.get("project_title") or project.get("title"))
    if not name:
        name = (
            PurePosixPath(project.get("project_path") or "").name
            or "Projeto não identificado"
        )
    phase = task.get("phase")
    label, activity = PHASES.get(
        phase,
        ("Estado não reconhecido", "Consulte os detalhes técnicos para investigar."),
    )
    events = task.get("events") or []
    latest = events[-1] if events else {}
    if latest.get("phase") == phase and isinstance(
        (latest.get("proof") or {}).get("progress"), str
    ):
        activity = plain(latest["proof"]["progress"], 600)
    work = task.get("work_status")
    if work in {"failed", "stopped", "cancelled"}:
        label, activity = PHASES[work]
    blocked = phase == "blocked"
    action = (
        "A origem deve verificar o impedimento e retomar a mesma tarefa."
        if blocked
        else "A origem deve revisar o resultado negativo e definir a retomada."
        if work in {"failed", "stopped"}
        else "A origem deve revisar o planejamento."
        if phase == "awaiting_approval"
        else "Nenhuma ação necessária."
    )
    child = task.get("child") or {}
    origin = task.get("origin") or {}
    if task.get("deliverable_kind") == "plan" and work == "succeeded":
        result = (
            "Planejamento aprovado; implementação ainda é uma etapa separada."
            if task.get("approval_state") == "approved"
            else "Planejamento entregue; aprovação ainda pendente."
        )
    else:
        result = (
            "Resultado negativo registrado."
            if work in {"failed", "stopped"}
            else "Cancelamento registrado."
            if work == "cancelled"
            else "Resultado ainda não verificado."
            if work != "succeeded"
            else "Trabalho validado; publicação e implantação têm estados próprios."
        )
    return dict(
        title=title,
        project=name,
        phase=label,
        activity=activity,
        result=result,
        blocker=BLOCKERS.get(
            task.get("blocked_reason"),
            "Há um impedimento registrado; consulte a origem.",
        )
        if blocked
        else None,
        next_action=action,
        updated_at=timestamp(task.get("observed_at")),
        url=thread_url(child.get("childThreadId") or origin.get("thread_id")),
        task_id=task.get("id"),
        snapshot_revision=task.get("snapshot_revision"),
    )


def summary_markdown(view: dict) -> str:
    lines = [
        f"**{markdown(view['title'])} · {markdown(view['project'])}**",
        f"**{markdown(view['phase'])}** — {markdown(view['activity'])}",
        markdown(view.get("blocker") or view["result"]),
        f"Próxima ação: {markdown(view['next_action'])}",
        f"Última atualização observada: {markdown(view['updated_at'])}",
    ]
    if view.get("url"):
        lines.append(f"[Abrir conversa da tarefa]({view['url']})")
    else:
        lines.append("Link da conversa indisponível.")
    return "\n\n".join(lines)


def return_message(delivery: dict) -> str:
    """A cached old delivery stays pure JSON; retries never change its format."""
    raw = canonical_json(delivery)
    if delivery.get("return_format") != "readable":
        return raw
    view = delivery["presentation"]
    notice = (
        "Registro histórico; consulte o estado atual."
        if delivery.get("historical")
        else "Observação no envio; a origem deve atualizar o estado antes de apresentá-lo."
    )
    # canonical JSON can contain backticks from untrusted metadata. Escape only
    # its HTML container, losslessly decoded by parse_return_message.
    return (
        summary_markdown(view)
        + "\n\n"
        + notice
        + '\n\n<details><summary>Detalhes técnicos do Oráculo</summary>\n<pre data-oraculo-control="v1">'
        + html.escape(raw)
        + "</pre>\n</details>"
    )


def parse_return_message(message: str) -> dict:
    try:
        value = json.loads(message)
    except json.JSONDecodeError:
        match = re.search(r'<pre data-oraculo-control="v1">(.*?)</pre>', message, re.S)
        if not match:
            raise ValueError("control_envelope_missing") from None
        value = json.loads(html.unescape(match[1]))
    if not isinstance(value, dict) or value.get("marker") != CONTROL_MARKER:
        raise ValueError("control_envelope_invalid")
    return value
