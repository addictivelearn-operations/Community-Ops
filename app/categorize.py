"""
Category labels from the learner's own words — port of 14_Categorize.gs.
A category is a free-form label of at most CATEGORY_MAX_WORDS words, produced
by Gemini from the ticket's requirement text.

CONSISTENCY: free-form labels drift ("WhatsApp group" / "Group joining" /
"Not added to group" for one issue). Every call is shown the labels already
used (read from the tickets table — a plain `SELECT DISTINCT`, simpler than
the sheet scan the .gs source needed), with instructions to reuse an existing
one when it fits and invent a new one only when it does not.
"""

import re
import time

from . import gemini
from .config import settings
from .db import get_conn

_CATEGORY_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {"id": {"type": "INTEGER"}, "category": {"type": "STRING"}},
        "required": ["id", "category"],
    },
}

CategorizeError = gemini.GeminiError
_gemini_json = gemini.call_json


def _category_system_prompt(existing_labels: list[str]) -> str:
    p = [
        "You label support tickets for LawSikho / SkillArbitrage, an online legal",
        "and professional education provider. Each input is what a LEARNER asked",
        "or reported.",
        "",
        f"For each item, return a short category describing the SUBJECT of the",
        f"request — at most {settings.category_max_words} words, normally two or three.",
        "Use Title Case. No trailing punctuation. Describe the issue, not the",
        "sentiment or urgency.",
        "",
        "Return one object per input, echoing back the id you were given.",
    ]
    if existing_labels:
        p.append("")
        p.append("CATEGORIES ALREADY IN USE — reuse one EXACTLY, character for")
        p.append("character, whenever it fits the item. Only write a new label when")
        p.append("none of these genuinely describes the request:")
        p.extend(f"  - {l}" for l in existing_labels)
    p.append("")
    p.append('If an item is empty or too vague to classify, return "Uncategorised".')
    return "\n".join(p)


def _normalise_category(label: str) -> str:
    s = re.sub(r"\s+", " ", label or "").strip()
    s = re.sub(r"[.;:,]+$", "", s)
    if not s:
        return ""
    words = s.split(" ")
    if len(words) > settings.category_max_words:
        s = " ".join(words[:settings.category_max_words])
    return s


def _apply_category_aliases(label: str) -> str:
    key = (label or "").strip().lower()
    if not key:
        return ""
    for k, v in settings.category_aliases.items():
        if k.strip().lower() == key:
            return v
    return label.strip()


def _keyword_regex(keyword: str):
    raw = (keyword or "").strip()
    if not raw:
        return None
    pattern = re.sub(r"[.*+?^${}()|\[\]\\]", lambda m: "\\" + m.group(0), raw)
    pattern = re.sub(r"\s+", r"[\\s-]*", pattern)
    return re.compile(r"\b" + pattern, re.IGNORECASE)


def _keyword_category(text: str) -> str:
    t = text or ""
    if not t:
        return ""
    for keyword, label in settings.category_keyword_rules:
        re_ = _keyword_regex(keyword)
        if re_ and re_.search(t):
            return label
    return ""


def _pre_category(text: str) -> str:
    if not (text or "").strip():
        return settings.category_empty_label
    return _keyword_category(text)


def categorize_batch(texts: list[str], existing_labels: list[str]) -> list[str]:
    """Port of categorizeBatch_ — one Gemini call for the whole batch."""
    out = [""] * len(texts)
    items = []
    for i, t in enumerate(texts):
        clean = re.sub(r"\s+", " ", (t or "")).strip()
        if clean:
            items.append((i, clean[:1200]))
    if not items:
        return out

    user_text = "\n\n".join(f"### id {i}\n{t}" for i, t in items)
    result = _gemini_json(_category_system_prompt(existing_labels), user_text, _CATEGORY_SCHEMA)
    for r in result or []:
        i = int(r.get("id", -1))
        if 0 <= i < len(out):
            out[i] = _normalise_category(r.get("category", ""))
    return out


def categorize_texts(texts: list[str], existing_labels: list[str]) -> list[str]:
    """Port of categorizeTexts_ — applies keyword/empty rules first, only
    calling Gemini for what's left."""
    out = [""] * len(texts)
    need_model, need_idx = [], []
    for i, t in enumerate(texts):
        pre = _pre_category(t)
        if pre:
            out[i] = pre
        else:
            need_model.append(t)
            need_idx.append(i)

    if need_model:
        labels = categorize_batch(need_model, existing_labels)
        for k, l in enumerate(labels):
            norm = _normalise_category(l)
            out[need_idx[k]] = _apply_category_aliases(norm) if norm else ""
    return out


def existing_category_labels() -> list[str]:
    """Distinct labels currently on tickets, most frequent first, capped —
    port of existingCategoryLabels_ (a SELECT instead of a sheet scan)."""
    with get_conn() as c:
        rows = c.execute(
            "SELECT category, COUNT(*) as n FROM tickets "
            "WHERE category != '' AND LOWER(category) != 'uncategorised' "
            "GROUP BY category ORDER BY n DESC LIMIT ?",
            (settings.category_max_existing_labels,)).fetchall()
    return [r["category"] for r in rows]


def categorize_new_rows(max_requests: int | None = None) -> dict:
    """Fills the category for tickets that have a requirement but no label
    yet — port of categorizeNewRows(). Never overwrites an existing label."""
    if not settings.gemini_api_key:
        return {"labelled": 0, "todo": 0, "skipped": "GEMINI_API_KEY not set"}

    request_cap = max_requests if max_requests else float("inf")
    requests_used = 0

    with get_conn() as c:
        rows = c.execute(
            "SELECT id, requirement FROM tickets WHERE category = ''").fetchall()

    todo = []
    written = 0
    for row in rows:
        pre = _pre_category(row["requirement"])
        if pre:
            with get_conn() as c:
                c.execute("UPDATE tickets SET category=? WHERE id=?", (pre, row["id"]))
            written += 1
            continue
        todo.append(row)

    if not todo:
        return {"labelled": written, "todo": 0}

    existing = existing_category_labels()
    started = time.time()
    budget = settings.category_time_budget_seconds

    for b in range(0, len(todo), settings.category_batch_size):
        if time.time() - started >= budget:
            break
        if requests_used >= request_cap:
            break
        requests_used += 1
        slice_ = todo[b:b + settings.category_batch_size]
        try:
            labels = categorize_texts([r["requirement"] for r in slice_], existing)
        except CategorizeError:
            break
        for l in labels:
            if l and l.lower() != "uncategorised" and l not in existing:
                existing.append(l)
        with get_conn() as c:
            for row, label in zip(slice_, labels):
                if label:
                    c.execute("UPDATE tickets SET category=? WHERE id=?", (label, row["id"]))
                    written += 1
        if b + settings.category_batch_size < len(todo) and settings.category_pause_ms > 0:
            time.sleep(settings.category_pause_ms / 1000)

    return {"labelled": written, "todo": len(todo)}
