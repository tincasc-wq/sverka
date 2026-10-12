"""Отчёт по истории в JSON: хронология, первоисточник, рёбра, независимые сообщения (ФТ-7.2)."""

from app.core.scoring import GraphConfig
from app.pipeline.graph import Node
from app.pipeline.story import StoryResult


def _short(text: str | None, limit: int = 140) -> str | None:
    if not text:
        return None
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _source_name(node: Node) -> str:
    return node.source.name if node.source else node.group


def build_report(result: StoryResult, cfg: GraphConfig) -> dict:
    graph = result.graph
    nodes = {n.id: n for n in result.nodes}
    order = sorted(result.nodes, key=lambda n: (n.effective_at(cfg), n.id))
    similarity = {c.document.id: c.similarity for c in result.members}
    membership = {c.document.id: c.membership for c in result.members}
    membership[result.seed.id] = "core"
    has_incoming = {e.to_id for e in graph.edges}

    def role(node: Node) -> str:
        if node.id == graph.primary_id:
            return "primary"
        return "derived" if node.id in has_incoming else "independent"

    timeline = []
    for n in order:
        timeline.append(
            {
                "time": n.effective_at(cfg).isoformat(),
                "source": _source_name(n),
                "group": n.group,
                "url": n.url,
                "title": _short(n.title or n.text),
                "role": role(n),
                "is_seed": n.id == result.seed.id,
                "similarity": None if n.id == result.seed.id else similarity.get(n.id),
                "membership": membership[n.id],
                "date_unreliable": n.date_unreliable(cfg),
                "took_from": [
                    {
                        "url": nodes[e.from_id].url,
                        "source": _source_name(nodes[e.from_id]),
                        "kind": e.kind.value,
                        "confidence": e.confidence,
                    }
                    for e in graph.incoming(n.id)
                ],
                "attributes_outside_archive": graph.external_attributions.get(n.id, []),
            }
        )

    primary = nodes.get(graph.primary_id) if graph.primary_id else None
    notes = []
    if primary and graph.external_attributions.get(primary.id):
        names = ", ".join(graph.external_attributions[primary.id])
        notes.append(f"Самая ранняя публикация ссылается на {names} — в архиве не найден")
    core_primary = None
    if primary and membership[primary.id] != "core":
        notes.append("Принадлежность к событию не подтверждена (средняя близость) — проверит LLM")
        core_primary = next(
            (n for n in order if membership[n.id] == "core" and n.id not in has_incoming), None
        )

    return {
        "story_id": result.story_id,
        "seed": {"url": result.seed.url, "title": _short(result.seed.title or result.seed.text)},
        "publications": len(result.nodes),
        "primary": {
            "url": primary.url,
            "source": _source_name(primary),
            "time": primary.effective_at(cfg).isoformat(),
            "membership": membership[primary.id],
            "notes": notes,
            "earliest_core_root": {"url": core_primary.url, "source": _source_name(core_primary)}
            if core_primary
            else None,
        }
        if primary
        else None,
        "core_publications": sum(1 for n in result.nodes if membership[n.id] == "core"),
        # Более ранние публикации на ту же тему, принадлежность которых к событию не подтверждена.
        "earlier_related": [
            {"time": e["time"], "source": e["source"], "url": e["url"], "title": e["title"]}
            for e in timeline
            if e["membership"] == "probable"
            and e["time"] < next(x["time"] for x in timeline if x["membership"] == "core")
        ][:3],
        "independent_reports": len(graph.independent_groups),
        # То же, но только по ядру события — «вероятные» публикации могут быть о другом.
        "independent_core": len(
            {
                n.group
                for n in result.nodes
                if membership[n.id] == "core" and n.id not in has_incoming
            }
        ),
        "independent_groups": graph.independent_groups,
        "timeline": timeline,
        "borderline": [
            {
                "url": c.document.url,
                "title": _short(c.document.title or c.document.text),
                "similarity": round(c.similarity, 3),
            }
            for c in result.borderline
        ],
        "note": "Без LLM: утверждения и сверка не извлекались, индекс не считался.",
    }
