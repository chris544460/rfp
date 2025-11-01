from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from answer_composer import CompletionsClient, get_openai_completion


class OpenAIClient:
    """Proxy that mirrors the CompletionsClient interface for OpenAI models."""

    def __init__(self, model: str) -> None:
        self.model = model

    def get_completion(self, prompt: str, json_output: bool = False):
        return get_openai_completion(prompt, self.model, json_output=json_output)


def save_uploaded_file(uploaded_file) -> str:
    suffix = Path(uploaded_file.name).suffix
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(uploaded_file.read())
    tmp.flush()
    return tmp.name


def select_top_preapproved_answers(
    question: str,
    hits: List[dict],
    limit: int = 5,
) -> List[dict]:
    """Use the Aladdin completions client to pick the most relevant pre-approved answers."""

    if len(hits) <= limit:
        return hits

    formatted: List[str] = []
    for idx, hit in enumerate(hits, 1):
        snippet = (hit.get("snippet") or "").strip().replace("", " ")
        if len(snippet) > 500:
            snippet = snippet[:497] + "..."
        source = hit.get("source") or "unknown"
        score = hit.get("score")
        if isinstance(score, (int, float)):
            score_repr = f"{score:.3f}"
        else:
            score_repr = str(score) if score is not None else "unknown"
        date = hit.get("date") or "unknown"
        formatted.append(
            f"{idx}. Source: {source}\nScore: {score_repr}\nDate: {date}\nSnippet: {snippet}"
        )

    prompt = (
        "You are ranking pre-approved RFP answers for how well they address a user's question. "
        f"Return a JSON object with a 'selections' array containing up to {limit} items. "
        "Each selection must include an 'index' (1-based) pointing to the candidate and a 'reason' in one or two sentences "
        "explaining how the candidate addresses the user's question."
        f"\n\nQuestion: {question}"
        "\n\nCandidates:\n" + "\n\n".join(formatted)
    )

    model_name = os.environ.get("ALADDIN_RERANK_MODEL", "o3-2025-04-16_research")
    try:
        client = CompletionsClient(model=model_name)
        content, _ = client.get_completion(prompt, json_output=True)
        data = json.loads(content or "{}")
    except Exception as exc:
        print(f"select_top_preapproved_answers failed with {model_name}: {exc}")
        return hits[:limit]

    selected: List[dict] = []
    seen = set()

    def add_hit(position: int, reason: Optional[str] = None) -> None:
        if not isinstance(position, int):
            return
        if not (1 <= position <= len(hits)):
            return
        if position in seen:
            return
        seen.add(position)
        hit_data = dict(hits[position - 1])
        if reason:
            cleaned = " ".join(str(reason).strip().split())
            if cleaned:
                hit_data["selection_reason"] = cleaned
        hit_data.setdefault("selected_by_model", model_name)
        selected.append(hit_data)

    selections = (
        data.get("selections")
        or data.get("choices")
        or data.get("ranked")
        or data.get("results")
        or []
    )
    if isinstance(selections, dict):
        for value in selections.values():
            if isinstance(value, list):
                selections = value
                break

    if isinstance(selections, list):
        for entry in selections:
            if len(selected) == limit:
                break
            reason = None
            idx_value = None
            if isinstance(entry, dict):
                reason = entry.get("reason") or entry.get("rationale") or entry.get("why")
                idx_value = (
                    entry.get("index")
                    or entry.get("idx")
                    or entry.get("rank")
                    or entry.get("position")
                )
            else:
                idx_value = entry
            try:
                pos = int(idx_value)
            except (TypeError, ValueError):
                continue
            add_hit(pos, reason)

    if len(selected) < limit:
        indices = data.get("top_indices") or data.get("top") or data.get("indices") or []
        if isinstance(indices, (list, tuple)):
            for idx in indices:
                if len(selected) == limit:
                    break
                try:
                    pos = int(idx)
                except (TypeError, ValueError):
                    continue
                add_hit(pos, None)

    if len(selected) < limit:
        for position in range(1, len(hits) + 1):
            if len(selected) == limit:
                break
            if position in seen:
                continue
            add_hit(position, None)

    if not selected:
        return hits[:limit]

    return selected[:limit]


__all__ = ["OpenAIClient", "save_uploaded_file", "select_top_preapproved_answers"]
