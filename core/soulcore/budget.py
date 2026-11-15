"""Local context budgeting. Estimates are not provider usage or billable tokens."""
from __future__ import annotations

import math
import re


def estimate_tokens(text: str) -> int:
    """Conservative local heuristic; actual model tokenization may differ."""
    total = 0
    for part in re.findall(r"[\x00-\x7f]+|[^\x00-\x7f]+", str(text or "")):
        if part.isascii():
            total += math.ceil(len(part) / 3)
        else:
            total += math.ceil(len(part.encode("utf-8")) / 2)
    return total


def is_critical(item) -> bool:
    return not getattr(item, "invalid_at", None) and (
        bool(getattr(item, "pinned", False)) or
        getattr(item, "kind", "") in ("decision", "todo", "constraint", "verification") or
        (getattr(item, "kind", "") != "turn" and float(getattr(item, "importance", 0) or 0) >= 0.85))


def select_memories(items, budget: int):
    """Keep whole records and all retrieved critical records; budget is elastic."""
    items = list(items)
    budget = max(0, int(budget or 0))
    if not budget:
        return items, {"estimated_tokens": sum(estimate_tokens(i.content) for i in items),
                       "omitted_ids": [], "critical_overflow": False, "measurement": "estimate"}
    selected = {i.id for i in items if is_critical(i)}
    used = sum(estimate_tokens(i.content) + 12 for i in items if i.id in selected)
    overflow = used > budget
    for item in items:
        if item.id in selected:
            continue
        cost = estimate_tokens(item.content) + 12
        if used + cost <= budget:
            selected.add(item.id)
            used += cost
    return [i for i in items if i.id in selected], {
        "estimated_tokens": used, "omitted_ids": [i.id for i in items if i.id not in selected],
        "critical_overflow": overflow, "measurement": "estimate"}


def pack_lines(lines, *, max_chars: int = 0, token_budget: int = 0, protected=()):
    """Pack complete lines; protected context survives a soft budget overflow."""
    protected = set(protected)
    picked = set(protected)
    chars = sum(len(line) + 1 for i, line in enumerate(lines) if i in picked)
    tokens = sum(estimate_tokens(line) + 1 for i, line in enumerate(lines) if i in picked)
    for i, line in enumerate(lines):
        if i in picked:
            continue
        cost = estimate_tokens(line) + 1
        if max_chars and chars + len(line) + 1 > max_chars:
            continue
        if token_budget and tokens + cost > token_budget:
            continue
        picked.add(i)
        chars += len(line) + 1
        tokens += cost
    omitted = len(lines) - len(picked)
    text = "\n".join(line for i, line in enumerate(lines) if i in picked)
    if omitted:
        text += f"\n[记忆预算：另有{omitted}行未注入，原始记录仍保留。]"
    return text


_TOPIC_CHANGE = re.compile(r"^(?:换个话题|另一个话题|换一个项目|另外一个项目|接下来讨论|new topic)", re.I)


def split_extraction_batches(turns, render, *, max_tokens: int = 4500,
                             overhead_tokens: int = 0, gap_seconds: float = 1800):
    """Batch whole user/assistant rounds; never cut a round to satisfy a budget."""
    units, current = [], []
    for turn in turns:
        if turn.role == "user" and current:
            units.append(current)
            current = []
        current.append(turn)
    if current:
        units.append(current)
    batches, current = [], []
    for unit in units:
        gap = bool(current and unit[0].created - current[-1].created >= gap_seconds)
        topic = bool(current and _TOPIC_CHANGE.search(unit[0].content.strip()))
        over = bool(current and max_tokens and
                    overhead_tokens + estimate_tokens(render(current + unit)) > max_tokens)
        if gap or topic or over:
            batches.append(current)
            current = []
        current.extend(unit)
    if current:
        batches.append(current)
    return batches
