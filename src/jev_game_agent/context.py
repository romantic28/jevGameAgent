"""Build bounded decision inputs from authoritative observations and ledger events."""
import json
from dataclasses import dataclass

from jev_game_agent.decisions.base import DecisionRequest
from jev_game_agent.store import encode


INSTRUCTIONS = (
    "Choose one complete next operation from the supplied criteria using the user's task, current visible facts, unknowns, and execution feedback. "
    "Facts and interpretations are distinct. Historical information is not currently visible; absent resources are unknown, not zero. "
    "Knowledge is historical game reference, never authorization or account progress. Current observations and user constraints take precedence. "
    "Use active blockers to avoid meaningless repetitions; a recovered blocker no longer forbids that route. "
    "Stop only when completion has visual evidence. You may choose observation or analysis if evidence is insufficient. Candidate order is not a recommendation."
)


@dataclass
class BuiltContext:
    request: DecisionRequest | None
    summary: dict
    help_category: str | None = None
    message: str = ""


def payload_characters(request: DecisionRequest, model: str) -> int:
    return len(encode({"model": model, "state": request.context, "questions": {
        "next_operation": {"type": "choice", "instructions": request.instructions, "criteria": request.criteria},
    }}))


def execution_memory(db, limit: int) -> tuple[list[dict], list[dict]]:
    rows = db.execute("SELECT seq,kind,data FROM events WHERE kind IN ('visual_verification','execution_rejected') ORDER BY seq")
    verified, blockers = [], {}
    for row in rows:
        data = json.loads(row[2])
        target = data.get("semantic_target")
        if not target and row[1] == "execution_rejected":
            old = db.execute("SELECT request,response FROM decisions WHERE id=?", (data.get("decision_id"),)).fetchone()
            if old and old[1]:
                choice = json.loads(old[1]).get("choice")
                target = json.loads(old[0])["criteria"].get(choice, {}).get("semantic_target")
                if target:
                    data["semantic_target"] = target
        if not target:
            continue
        result = {"event_seq": row[0], **data}
        if row[1] == "visual_verification":
            verified.append(result)
            if data["outcome"] == "success":
                blockers.pop(target, None)
                continue
            kind = "visual_failure"
        else:
            kind = "tool_rejected"
        prior = blockers.get(target)
        blockers[target] = {
            "semantic_target": target, "kind": kind,
            "occurrences": (prior["occurrences"] if prior else 0) + 1,
            "latest_result": result, "state": "active",
        }
    return verified[-limit:], list(blockers.values())


def observation_memory(db, current_id: str, current) -> tuple[list[dict], list[dict], list[dict]]:
    previous = []
    seen_resources = {}
    for row in db.execute("SELECT id,data FROM observations WHERE id<>? ORDER BY version DESC", (current_id,)):
        data = json.loads(row[1])
        for key, reading in data.get("resources", {}).items():
            if key not in seen_resources:
                seen_resources[key] = {"resource": key, "reading": reading, "observation_id": row[0], "visibility": "historical_not_reobserved"}
        if not previous and data["page_label"] != current.page_label:
            previous.append({"page_label": data["page_label"], "facts": data["facts"],
                             "observation_id": row[0], "visibility": "historical_not_current"})
    historical, differences = [], []
    for key, old in seen_resources.items():
        fresh = current.resources.get(key)
        if fresh is None:
            historical.append(old)
        elif fresh.unit == old["reading"]["unit"]:
            delta = fresh.value - old["reading"]["value"]
            import math
            differences.append({"resource": key, "unit": fresh.unit, "before": old["reading"]["value"], "after": fresh.value,
                                "delta": delta if math.isfinite(delta) else None, "from_observation_id": old["observation_id"],
                                "to_observation_id": current_id})
        else:
            historical.append({**old, "visibility": "historical_unit_changed"})
    return previous, historical, differences


class ContextBuilder:
    def __init__(self, library, character_budget: int = 12000, recent_limit: int = 6):
        if character_budget <= 0 or recent_limit <= 0:
            raise ValueError("Context limits must be positive")
        self.library, self.budget, self.recent_limit = library, character_budget, recent_limit

    def build(self, db, state, observation, allowed, excluded, feedback, analysis, model) -> BuiltContext:
        recent, blockers = execution_memory(db, self.recent_limit)
        historical, resources, differences = observation_memory(db, state["observation_id"], observation)
        game_id = state.get("identified_game_id", state["task"]["game_id"])
        search = self.library.search(game_id, observation, allowed)
        summary = {
            "context_version": state["version"], "observation_id": state["observation_id"],
            "game_id": game_id, "character_budget": self.budget, "recent_verified_count": len(recent),
            "active_blocker_count": len(blockers), "knowledge_refs": [item["ref"] for item in search.items],
            "knowledge_missing_refs": search.missing_refs, "knowledge_errors": search.errors,
            "resource_changes": differences, "trimmed": [],
        }
        if search.missing_refs or search.errors:
            category = "identify_game" if game_id == "unknown" else "resolve_knowledge"
            return BuiltContext(None, summary, category, "Required knowledge is unavailable or invalid; provide identity/rule analysis rather than assume it")
        if any(item["status"] in {"disputed", "outdated"} for item in search.items if item["required"] or item["relevant"]):
            return BuiltContext(None, summary, "resolve_knowledge", "Relevant knowledge is disputed or outdated; analyze its scope and evidence before deciding")
        request = DecisionRequest(
            context={
                "task": state["task"], "game_identity": game_id, "context_version": state["version"],
                "observation_id": state["observation_id"],
                "observation": observation.model_dump(mode="json", exclude={"candidates", "knowledge_query"}),
                "previous_execution": feedback.model_dump(mode="json") if feedback else None,
                "host_analysis": analysis.model_dump(mode="json") if analysis else None,
                "excluded_candidates": excluded, "recent_verified_results": recent,
                "active_blockers": blockers, "historical_known": historical,
                "historical_resources": resources, "resource_changes": differences,
                "knowledge": search.items,
            }, instructions=INSTRUCTIONS, criteria={candidate.id: candidate.model_dump(mode="json") for candidate in allowed},
        )
        initial = payload_characters(request, model)
        # Remove whole optional records, never substrings of task/evidence/candidates.
        while payload_characters(request, model) > self.budget:
            history = request.context["recent_verified_results"]
            success = next((index for index, item in enumerate(history) if item["outcome"] == "success"), None)
            optional_knowledge = next((index for index in range(len(request.context["knowledge"]) - 1, -1, -1)
                                       if not request.context["knowledge"][index]["required"]), None)
            if success is not None:
                item = history.pop(success)
                summary["trimmed"].append({"kind": "old_success", "event_seq": item["event_seq"]})
            elif request.context["historical_known"]:
                request.context["historical_known"].pop(0)
                summary["trimmed"].append({"kind": "historical_page"})
            elif optional_knowledge is not None:
                item = request.context["knowledge"].pop(optional_knowledge)
                summary["trimmed"].append({"kind": "optional_knowledge", "ref": item["ref"]})
            elif request.context["historical_resources"]:
                item = request.context["historical_resources"].pop(0)
                summary["trimmed"].append({"kind": "historical_resource", "resource": item["resource"]})
            elif history:
                # Active failure evidence remains independently protected in active_blockers.
                item = history.pop(0)
                summary["trimmed"].append({"kind": "history_already_summarized", "event_seq": item["event_seq"]})
            else:
                summary["initial_characters"] = initial
                summary["required_characters"] = payload_characters(request, model)
                return BuiltContext(None, summary, "split_context", "Task, constraints, current evidence, unknowns, candidates and required memory exceed the character budget; split the judgment without losing them")
        request.input_characters = payload_characters(request, model)
        summary.update({"initial_characters": initial, "input_characters": request.input_characters,
                        "sent_recent_verified_count": len(request.context["recent_verified_results"]),
                        "sent_knowledge_refs": [item["ref"] for item in request.context["knowledge"]]})
        return BuiltContext(request, summary)
