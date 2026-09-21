"""
Extraction pipeline: deterministic first, an AI fallback last. A direct port
of 04_Extraction.gs — same regexes, same priority order, same conservatism
(a value is returned only when it is unambiguous; anything doubtful is left
blank for the cheaper-than-AI fallbacks and finally the AI call).

The .gs source used Claude for this (extractWithClaude_). This port uses
Gemini instead (via gemini.py, the same caller categorize.py uses) — Kawal's
choice, 21 Sep 2026, to avoid needing a new API key when GEMINI_API_KEY was
already configured and working. Nothing about the task needs Claude
specifically: it's plain JSON-schema-constrained extraction/summarization,
which Gemini's `responseSchema` handles the same way. Revisit if quality on
real tickets doesn't hold up — the schema/prompt-building functions below are
provider-agnostic; only the wire call and schema dialect would need to change
back.

Deliberately NOT ported: image/OCR attachment handling
(fetchImageAttachments_ / compressImageIfNeeded_ in the .gs source). That was
an Apps-Script-specific trick (uploading to Drive to get a server-generated
thumbnail) with no direct Python equivalent, and only ever ran for tickets
that were both non-trivial AND still missing a field after every other
lookup — a narrow slice of traffic. The AI fallback still gets the full text
bundle; it just never sees inline screenshots. Revisit if that turns out to
matter.
"""

import re
from dataclasses import dataclass

from . import gemini
from .config import settings

# ---------------------------------------------------------------------------
# Part 1 — deterministic extraction (no API calls, no cost)
# ---------------------------------------------------------------------------

_EMAIL_RE = r"[\w.+\-]+@[\w.\-]+"


def is_internal_email(email: str) -> bool:
    """Port of isInternalEmail_ — tolerant of a display-name-wrapped address
    ('"Baisakhi Dey" <baisakhi@addictivelearn.com>') and subdomains."""
    if not email:
        return False
    m = re.search(r"[\w.+\-]+@([\w.\-]+)", email.lower())
    domain = re.sub(r"[.>\s]+$", "", m.group(1)) if m else ""
    if not domain:
        return False
    from .store import INTERNAL_DOMAINS
    for d in INTERNAL_DOMAINS:
        d = d.lower().lstrip("@")
        if domain == d or domain.endswith("." + d):
            return True
    return False


_BANNED_NAME_SUBSTRINGS = [
    "team", "support", "lawsikho", "skill arbitrage", "admin", "noreply",
    "no-reply", "helpdesk", "care", "info", "academy", "sent from", "iphone",
    "android", "outlook", "samsung", "whatsapp", "disclaimer",
]
_BANNED_NAME_WORDS = re.compile(r"\b(sir|madam|ma'?am|dear|learner|student|user|customer|member|all)\b")


def is_plausible_name(s: str) -> bool:
    s = (s or "").strip()
    if len(s) < 3 or len(s) > 60:
        return False
    if re.search(r"[@\d]", s):
        return False
    if not re.match(r"^[A-Za-z][A-Za-z .'\-]+$", s):
        return False
    lc = s.lower()
    if any(b in lc for b in _BANNED_NAME_SUBSTRINGS):
        return False
    if _BANNED_NAME_WORDS.search(lc):
        return False
    return bool(re.search(r"\s", s)) or len(s) >= 4


def title_case(s: str) -> str:
    return re.sub(r"\b[a-z]", lambda m: m.group(0).upper(), s)


_FWD_RE = re.compile(r"From:\s*\"?([A-Za-z][A-Za-z .'\-]{2,50}?)\"?\s*<\s*(" + _EMAIL_RE + r")\s*>")
_FWD_BARE_RE = re.compile(r"^From:\s*<?\s*(" + _EMAIL_RE + r")\s*>?", re.IGNORECASE | re.MULTILINE)
_TO_RE = re.compile(r"To:\s*<?\s*(" + _EMAIL_RE + r")\s*>?")
_ADDR_RE = re.compile(_EMAIL_RE)


def resolve_external_learner(description_text: str, conversations: list[dict]) -> dict:
    """Finds the real (external) learner behind an agent-originated ticket.
    Port of resolveExternalLearner_. Returns {"email": "", "name": ""}."""
    texts = [description_text or ""] + [c.get("text", "") for c in conversations]

    # 1. Forwarded header with a display name — gives BOTH.
    for t in texts:
        for m in _FWD_RE.finditer(t):
            em = m.group(2).lower()
            if not is_internal_email(em):
                name = m.group(1).strip()
                return {"email": em, "name": name if is_plausible_name(name) else ""}

    # 1b. Bare forwarded header without a display name.
    for t in texts:
        for m in _FWD_BARE_RE.finditer(t):
            em = m.group(1).lower()
            if not is_internal_email(em):
                return {"email": em, "name": ""}

    # 2. Recipients (To + Cc) of ANY email in the thread — direction ignored.
    for c in conversations:
        if c.get("type") != "email":
            continue
        recipients = f"{c.get('to', '')},{c.get('cc', '')}"
        for addr in re.split(r"[,;]", recipients):
            m = _ADDR_RE.search(addr)
            if m and not is_internal_email(m.group(0).lower()):
                return {"email": m.group(0).lower(), "name": ""}

    # 3. Bare "To: <email>" lines inside bodies (forwarded blocks).
    for t in texts:
        for m in _TO_RE.finditer(t):
            em = m.group(1).lower()
            if not is_internal_email(em):
                return {"email": em, "name": ""}

    return {"email": "", "name": ""}


_SAL_RE = re.compile(
    r"^(?:dear|hi|hello)\s+(?:mr\.?\s+|ms\.?\s+|mrs\.?\s+)?([A-Za-z][A-Za-z .]{2,40}?)\s*[,!]",
    re.IGNORECASE | re.MULTILINE)


def extract_salutation_name(description_text: str, conversations: list[dict], outgoing_only: bool) -> str:
    """Port of extractSalutationName_."""
    texts = []
    if not outgoing_only:
        texts.append(description_text or "")
    for c in conversations:
        if c.get("type") != "email":
            continue
        if outgoing_only and c.get("direction") != "out":
            continue
        texts.append(c.get("text", ""))
    for t in texts:
        m = _SAL_RE.search(t)
        if m and is_plausible_name(m.group(1)):
            return title_case(m.group(1).strip())
    return ""


_SIGNOFF_RE = re.compile(
    r"^(warm\s+regards|kind\s+regards|best\s+regards|regards|"
    r"thanks(?:\s+(?:and|&)\s+regards)?|thank\s+you|sincerely|best|cheers)[,.!]?$",
    re.IGNORECASE)


def last_line_name(text: str) -> str:
    lines = (text or "").split("\n")
    seen = 0
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        seen += 1
        if seen > 3:
            break
        if _SIGNOFF_RE.match(line):
            continue
        if re.search(r"\d|@|https?:|www\.", line, re.IGNORECASE):
            continue
        if len(line.split()) > 4:
            continue
        if is_plausible_name(line):
            return line
    return ""


def extract_learner_name(contact_name: str, learner_texts: list[str], learner_authors: list[str]) -> str:
    """Port of extractLearnerName_."""
    if is_plausible_name(contact_name):
        return contact_name.strip()
    for author in learner_authors:
        if is_plausible_name(author):
            return author.strip()
    for text in learner_texts:
        lines = text.split("\n")
        for i, line in enumerate(lines[:-1]):
            if _SIGNOFF_RE.match(line.strip()):
                seen = 0
                for cand in lines[i + 1:]:
                    cand = cand.strip()
                    if not cand:
                        continue
                    seen += 1
                    if is_plausible_name(cand):
                        return cand
                    if seen >= 2:
                        break
    for text in learner_texts:
        cand = last_line_name(text)
        if cand:
            return cand
    return ""


_PHONE_RE = re.compile(r"(?:\+?\s*\(?\s*91\s*\)?[\s\-.]*)?([6-9]\d{4}[\s\-.]?\d{5})\b")


def normalise_phone(raw: str) -> str:
    """Port of normalisePhone_."""
    s = (raw or "").strip()
    if not s:
        return ""
    s = re.sub(r"[()\-.]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    m = re.match(r"^\+?(\d[\d ]{8,14}\d)$", s)
    if not m:
        return s
    digits = re.sub(r"\D", "", s)
    if s.startswith("+") or len(digits) > 10:
        country, local = digits[:-10], digits[-10:]
        return f"+{country} {local}" if country else local
    return digits


def extract_phone_number(learner_texts: list[str]) -> str:
    """Port of extractPhoneNumber_ — '' when zero or multiple DIFFERENT
    numbers are found (ambiguity → let fallbacks decide)."""
    found = set()
    for text in learner_texts:
        for m in _PHONE_RE.finditer(text):
            digits = re.sub(r"\D", "", m.group(1))
            if len(digits) == 10:
                found.add(digits)
    if len(found) != 1:
        return ""
    return normalise_phone(next(iter(found)))


_COURSE_PATTERNS = [
    re.compile(r"course\s*(?:name)?\s*[:\-]\s*([^\n,;.]{4,90})", re.IGNORECASE),
    re.compile(r"program(?:me)?\s*(?:name)?\s*[:\-]\s*([^\n,;.]{4,90})", re.IGNORECASE),
    re.compile(r"\b((?:advanced\s+|executive\s+)?(?:diploma|certificate(?:\s+course)?|"
              r"training\s+program(?:me)?|master\s*class)\s+(?:in|on)\s+[^\n,;.]{3,80})", re.IGNORECASE),
    re.compile(r"\benrolled\s+(?:in|for)\s+(?:the\s+)?([^\n,;.]{4,90}?(?:course|program(?:me)?|diploma|certificate))",
              re.IGNORECASE),
]


def extract_course_name(subject: str, learner_texts: list[str]) -> str:
    """Port of extractCourseName_ — '' unless exactly one distinct course is
    matched across all sources, never guesses."""
    sources = [subject or ""] + learner_texts
    found: dict[str, str] = {}
    for text in sources:
        for pattern in _COURSE_PATTERNS:
            m = pattern.search(text)
            if m and m.group(1):
                c = re.sub(r"\s+", " ", m.group(1)).strip()
                if len(c) >= 4:
                    found[c.lower()] = c
    keys = list(found.keys())
    return found[keys[0]] if len(keys) == 1 else ""


@dataclass
class Identity:
    email: str
    name: str
    phone: str
    course: str


def extract_deterministic(details: dict, conversations: list[dict]) -> Identity:
    """Port of extractDeterministic_.
    details: {subject, description_text, email, phone, contact_name, ...}
    conversations: zoho.conversations() shape."""
    contact_email = (details.get("email") or "").strip().lower()
    contact_is_internal = is_internal_email(contact_email)

    if contact_is_internal:
        identity = resolve_external_learner(details.get("description_text", ""), conversations)
    else:
        identity = {"email": contact_email, "name": ""}

    learner_texts = [details.get("description_text") or ""]
    learner_authors = []
    for c in conversations:
        if c.get("type") != "email":
            continue
        if c.get("direction") == "in":
            learner_texts.append(c.get("text") or "")
            if c.get("author") and not is_internal_email(c.get("from_email", "")):
                learner_authors.append(c["author"])
        elif contact_is_internal:
            learner_texts.append(c.get("text") or "")

    name = identity["name"]
    if not name:
        if contact_is_internal:
            name = extract_salutation_name(details.get("description_text", ""), conversations, False)
        else:
            name = (extract_learner_name(details.get("contact_name", ""), learner_texts, learner_authors)
                   or extract_salutation_name(details.get("description_text", ""), conversations, True))

    return Identity(
        email=identity["email"],
        name=name,
        phone=extract_phone_number(learner_texts),
        course=extract_course_name(details.get("subject", ""), learner_texts),
    )


def normalise_course_name(name: str) -> str:
    """Port of normaliseCourseName_ — applied to deterministic/AI-extracted
    names only; Course-master-sheet values bypass this."""
    trimmed = re.sub(r"\s+", " ", name or "").strip()
    if not trimmed:
        return ""
    lc = trimmed.lower()
    for alias, canonical in settings.course_aliases.items():
        if alias.lower() == lc:
            return canonical
    return trimmed


# ---------------------------------------------------------------------------
# Part 2 — AI fallback (Gemini; one call: missing fields + summary; text only)
# ---------------------------------------------------------------------------

_FIELD_DESCRIPTIONS = {
    "learner_name": "Learner's full name (never a support agent's). Return null unless clearly confident.",
    "learner_phone": "Learner's phone number exactly as found (any format). Return null unless clearly confident.",
    "course_name": "Exact course/program name the ticket concerns. Return null unless clearly confident — NEVER guess.",
}


def _build_schema(missing_fields: list[str], want_summary: bool) -> dict:
    """Gemini's OpenAPI-subset responseSchema dialect (UPPERCASE types,
    `nullable: true` for "return null unless confident" instead of a JSON
    Schema `anyOf`)."""
    properties: dict = {}
    required = []
    for f in missing_fields:
        properties[f] = {"type": "STRING", "nullable": True, "description": _FIELD_DESCRIPTIONS[f]}
        required.append(f)
    if want_summary:
        properties["requirement_summary"] = {
            "type": "STRING",
            "description": ("Concise summary of what the LEARNER asked or reported — their "
                           "requirement only. No subject line, no timestamps, no email headers, "
                           "no ticket status, no agent replies."),
        }
        required.append("requirement_summary")
    properties["confidence"] = {
        "type": "NUMBER",
        "description": ("Your overall confidence in this response, from 0 (very unsure) to 1 "
                        "(certain). Consider evidence quality across all requested fields and the summary."),
    }
    required.append("confidence")
    return {"type": "OBJECT", "properties": properties, "required": required}


def _build_system_prompt(missing_fields: list[str], want_summary: bool, known: dict) -> str:
    from .store import INTERNAL_DOMAINS
    p = [
        "You are a meticulous support-operations analyst for LawSikho / Skill Arbitrage,",
        "processing a Zoho Desk ticket raised by a learner. You receive the complete",
        "ticket: metadata, the original description, and every email in the conversation",
        "(incoming and outgoing) and internal comments.",
        "",
        "ACCURACY OVER COMPLETENESS: for any requested field, if you are not clearly",
        "confident, return null. Never guess, never infer beyond the evidence, never",
        "fabricate. A null is always better than a plausible-but-unverified value.",
        "",
        "IMPORTANT: email addresses on these internal domains belong to SUPPORT",
        f"AGENTS, never learners: {', '.join(sorted(INTERNAL_DOMAINS))}.",
        "When a ticket was sent or forwarded by an agent on a learner's behalf,",
        "identify the real learner from the recipient (To) address, forwarded",
        "headers (\"From: Name <email>\"), or the salutation (\"Dear <name>,\").",
        "",
    ]
    known_lines = []
    if known.get("name"):
        known_lines.append(f"Learner name: {known['name']}")
    if known.get("phone"):
        known_lines.append(f"Learner phone: {known['phone']}")
    if known.get("course"):
        known_lines.append(f"Course: {known['course']}")
    if known_lines:
        p.append("Already confirmed from other systems (do NOT re-derive; use for context):")
        p.extend(f"  - {l}" for l in known_lines)
        p.append("")

    if missing_fields:
        p.append("Determine the following, preferring the most reliable source when they")
        p.append("disagree (metadata > email body > signature > conversation):")
        if "learner_name" in missing_fields:
            p.append("  - learner_name: the LEARNER's name only. null if not confident.")
        if "learner_phone" in missing_fields:
            p.append("  - learner_phone: from description, bodies, or signatures.")
            p.append("    Keep the format as found. null if none clearly belongs to the learner.")
        if "course_name" in missing_fields:
            p.append("  - course_name: the EXACT course/program name concerned. null unless")
            p.append("    reasonably certain.")
        p.append("")

    if want_summary:
        p.append("requirement_summary: state ONLY what the learner asked for or")
        p.append("reported — their requirement in plain words, drawn from the learner's")
        p.append("own messages across the ticket. Strictly exclude: the subject line,")
        p.append("dates/times, forwarded or quoted email headers (From/Date/Subject/To),")
        p.append("ticket status, agent replies, greetings, signatures, confidentiality")
        p.append("notices, and disclaimers.")
        p.append("")

    p.append("confidence: report your overall confidence (0 to 1) in this response,")
    p.append("reflecting evidence quality — not optimism. Low evidence => low score.")
    return "\n".join(p)


_QUOTE_START = [
    re.compile(r"^On .{5,80}(wrote|writes):?\s*$", re.IGNORECASE),
    re.compile(r"^-{2,}\s*(original|forwarded)\s+message\s*-{2,}", re.IGNORECASE),
    re.compile(r"^_{5,}\s*$"),
    re.compile(r"^From:\s.+", re.IGNORECASE),
]
_DISCLAIMER_HINT = re.compile(
    r"(confidential|disclaimer|privileged|intended recipient|do not disseminate|"
    r"if you (have )?received this (e-?mail|message) in error|views expressed|virus[- ]free)",
    re.IGNORECASE)


def clean_email_text(text: str, seen_paragraphs: set) -> str:
    """Port of cleanEmailText_ — strips quoted chains, disclaimers, and
    paragraphs already seen elsewhere in the same ticket, to cut tokens.
    Deterministic extraction already ran on the RAW text, so nothing
    extraction-relevant is lost here."""
    if not text:
        return ""
    kept = []
    for line in text.split("\n"):
        t = line.strip()
        if any(p.match(t) for p in _QUOTE_START):
            break
        if t.startswith(">"):
            continue
        kept.append(line)

    paragraphs = re.split(r"\n{2,}|\n(?=\S)", "\n".join(kept))
    out = []
    for p in paragraphs:
        norm = re.sub(r"\s+", " ", p).strip().lower()
        if not norm:
            continue
        if _DISCLAIMER_HINT.search(norm) and len(norm) > 120:
            continue
        if len(norm) >= 40:
            if norm in seen_paragraphs:
                continue
            seen_paragraphs.add(norm)
        out.append(p.strip())
    return "\n".join(out).strip()


def truncate(s: str, max_chars: int) -> str:
    s = s or ""
    return s if len(s) <= max_chars else s[:max_chars] + "\n…[truncated]"


def build_ticket_bundle(details: dict, conversations: list[dict]) -> str:
    """Port of buildTicketBundle_ (text-only — no image handling, see the
    module docstring)."""
    parts = []
    seen: set = set()
    parts.append("=== TICKET METADATA ===")
    parts.append(f"Subject: {details.get('subject', '')}")
    parts.append(f"Contact name (metadata): {details.get('contact_name') or '(none)'}")
    parts.append(f"Contact email: {details.get('email') or '(none)'}")
    parts.append(f"Contact phone (metadata): {details.get('phone') or '(none)'}")
    parts.append(f"Created: {details.get('created_time', '')} | Status: {details.get('status', '')}")
    parts.append("")
    parts.append("=== ORIGINAL TICKET DESCRIPTION ===")
    parts.append(clean_email_text(details.get("description_text", ""), seen) or "(empty)")
    parts.append("")
    parts.append("=== FULL CONVERSATION HISTORY (chronological, cleaned) ===")

    for i, c in enumerate(conversations):
        if c.get("type") == "email":
            label = "INCOMING EMAIL (learner)" if c.get("direction") == "in" else "OUTGOING EMAIL (support)"
        else:
            label = "INTERNAL COMMENT" if c.get("direction") == "private" else "PUBLIC COMMENT"
        cleaned = clean_email_text(truncate(c.get("text", ""), settings.max_text_chars_per_thread), seen)
        if not cleaned:
            continue
        parts.append(f"--- [{i + 1}] {label} | {c.get('author', '')} | {c.get('time', '')} ---")
        parts.append(cleaned)
        attachments = c.get("attachments") or []
        if attachments:
            parts.append(f"[Attachments: {', '.join(attachments)}]")
        parts.append("")

    return truncate("\n".join(parts), settings.max_total_text_chars)


def _clean_nullable(v) -> str:
    return "" if v is None else str(v).strip()


def _clamp_confidence(v) -> str | float:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return ""
    return max(0.0, min(1.0, n))


ExtractionError = gemini.GeminiError


def extract_with_ai(details: dict, conversations: list[dict], missing_fields: list[str],
                    want_summary: bool, known: dict) -> dict:
    """One AI call per ticket, text only — port of extractWithClaude_, now
    calling Gemini instead (see the module docstring)."""
    empty = {"learner_name": "", "learner_phone": "", "course_name": "",
             "requirement_summary": "", "confidence": ""}
    if not missing_fields and not want_summary:
        return empty

    parsed = gemini.call_json(
        _build_system_prompt(missing_fields, want_summary, known),
        build_ticket_bundle(details, conversations),
        _build_schema(missing_fields, want_summary))

    return {
        "learner_name": _clean_nullable(parsed.get("learner_name")),
        "learner_phone": normalise_phone(_clean_nullable(parsed.get("learner_phone"))),
        "course_name": _clean_nullable(parsed.get("course_name")),
        "requirement_summary": _clean_nullable(parsed.get("requirement_summary")),
        "confidence": _clamp_confidence(parsed.get("confidence")),
    }


_HEADER_LINE = re.compile(
    r"^(?:-{2,}\s*Forwarded message\s*-{2,}.*|From:\s.*|Date:\s.*|Sent:\s.*|Subject:.*|To:\s.*|Cc:\s.*)$",
    re.IGNORECASE)


def strip_forwarded_headers(text: str) -> str:
    """Port of stripForwardedHeaders_."""
    if not text:
        return ""
    kept = [line for line in text.split("\n") if line.strip() and not _HEADER_LINE.match(line.strip())]
    out = "\n".join(kept)
    out = re.sub(r"-{2,}\s*Forwarded message\s*-{2,}", " ", out, flags=re.IGNORECASE)
    out = re.sub(r"From:\s*[^<>@\n]{0,60}<\s*" + _EMAIL_RE + r"\s*>", " ", out, flags=re.IGNORECASE)
    out = re.sub(r"From:\s*" + _EMAIL_RE, " ", out, flags=re.IGNORECASE)
    out = re.sub(r"Date:\s*\w{3},?\s*\d{1,2}\s+\w{3,9},?\s*\d{4}\s*(?:at\s*)?(?:,\s*)?\d{1,2}:\d{2}\s*(?:am|pm)?",
                 " ", out, flags=re.IGNORECASE)
    out = re.sub(r"Subject:\s*[\s\S]{0,120}?(?=(?:From|To|Cc|Date):|$)", " ", out, flags=re.IGNORECASE)
    out = re.sub(
        r"(?:To|Cc):\s*(?:[^,<\n]{0,60}?)?(?:<\s*" + _EMAIL_RE + r"\s*>|" + _EMAIL_RE + r")"
        r"(?:\s*<\s*" + _EMAIL_RE + r"\s*>)?(?:\s*,\s*(?:[^,<\n]{0,60}?)?"
        r"(?:<\s*" + _EMAIL_RE + r"\s*>|" + _EMAIL_RE + r")(?:\s*<\s*" + _EMAIL_RE + r"\s*>)?)*",
        " ", out, flags=re.IGNORECASE)
    return re.sub(r"[ \t]+", " ", out).strip()


def build_deterministic_summary(details: dict, conversations: list[dict]) -> str:
    """Port of buildDeterministicSummary_ — used for trivial tickets, or
    whenever AI produced no summary."""
    text = strip_forwarded_headers(details.get("description_text", ""))
    if not text:
        for c in conversations:
            text = strip_forwarded_headers(c.get("text", ""))
            if text:
                break
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    return text if len(text) <= 400 else text[:400] + "…"
