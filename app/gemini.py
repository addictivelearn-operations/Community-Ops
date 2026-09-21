"""
Shared Gemini caller — structured (JSON-schema-constrained) generation, used
by both categorize.py (label a ticket) and extraction.py (fill missing
fields / write a summary). One place for the key-rotation-on-quota logic
(port of 14_Categorize.gs's geminiJson_) so both callers get it identically.
"""

import json

import httpx

from .config import settings

GEMINI_URL_BASE = "https://generativelanguage.googleapis.com/v1beta"


class GeminiError(RuntimeError):
    pass


def keys() -> list[str]:
    return [k.strip() for k in settings.gemini_api_key.split(",") if k.strip()]


_key_index = 0


def call_json(system_text: str, user_text: str, schema: dict):
    """One Gemini call, JSON-mode, constrained to `schema` (Gemini's OpenAPI-
    subset dialect — types are UPPERCASE strings: OBJECT/STRING/NUMBER/ARRAY/
    INTEGER, and optional fields use `nullable: true` rather than a JSON
    Schema `anyOf`).

    Keys are tried in order and the working one is remembered for the rest of
    the process, so an exhausted key costs one wasted request, not one per
    call. A 429 (quota) moves to the NEXT key; any other error raises
    immediately, since retrying a bad model/request on a different key would
    just repeat the same failure."""
    global _key_index
    all_keys = keys()
    if not all_keys:
        raise GeminiError("GEMINI_API_KEY is not set.")

    payload = {
        "systemInstruction": {"parts": [{"text": system_text}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                             "responseSchema": schema},
    }

    body = None
    last_quota_error = ""
    for attempt in range(len(all_keys)):
        idx = (_key_index + attempt) % len(all_keys)
        r = httpx.post(f"{GEMINI_URL_BASE}/models/{settings.gemini_model}:generateContent",
                       params={"key": all_keys[idx]}, json=payload, timeout=60)
        parsed = r.json()
        if isinstance(parsed, dict) and parsed.get("error") and r.status_code == 429:
            last_quota_error = parsed["error"].get("message", "quota exhausted")
            continue
        _key_index = idx
        body = parsed
        break

    if body is None:
        raise GeminiError(f"All {len(all_keys)} Gemini key(s) are out of quota. Last message: {last_quota_error[:400]}")
    if isinstance(body, dict) and body.get("error"):
        raise GeminiError(f"Gemini error: {body['error'].get('message', body['error'])} "
                          f"(model \"{settings.gemini_model}\")")

    candidates = body.get("candidates") or []
    parts = (candidates[0].get("content", {}).get("parts") if candidates else None) or []
    text = ""
    for p in parts:
        if p.get("text") and not text:
            text = p["text"]
    if not text:
        finish = candidates[0].get("finishReason") if candidates else None
        raise GeminiError(f"Gemini returned no text (finishReason={finish})")

    try:
        return json.loads(text)
    except ValueError as e:
        raise GeminiError(f"Could not parse Gemini JSON: {e} | raw: {text[:400]}") from e
