"""Meta title and description generation.

One call per product, driven by the system prompt stored in settings. The
response must be JSON with exactly two fields; anything else is treated as
a failure the user can retry, never something that silently uploads.
"""

from __future__ import annotations

import json
import re

import httpx

from .config import Settings

# Used when the model field in settings is left blank. OpenRouter model ids
# take the provider/model form.
DEFAULT_MODEL = "anthropic/claude-sonnet-4.5"

LLM_TIMEOUT = 90  # Generous and separate from the scrape timeout.


class LlmError(RuntimeError):
    """Raised with a message suitable for showing in the interface."""


def _parse_response(text: str) -> dict:
    """Models wrap JSON in prose or code fences often enough to plan for it."""
    cleaned = re.sub(r"```(?:json)?", "", text).strip("` \n")
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise LlmError(f"The model did not return JSON. It said: {text[:200]}")
    try:
        data = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        raise LlmError(f"Could not parse the model's JSON: {exc}") from exc

    title = str(data.get("title") or "").strip()
    description = str(data.get("meta_description")
                      or data.get("description") or "").strip()
    if not title or not description:
        raise LlmError("The model's JSON is missing 'title' or "
                       "'meta_description'.")
    return {"title": title, "meta_description": description}


def generate_meta(name: str, description_text: str, price: str,
                  settings: Settings) -> dict:
    """Returns {'title': ..., 'meta_description': ...} or raises LlmError."""
    if not settings.llm_api_key:
        raise LlmError("No LLM API key. Add one in Settings.")

    model = settings.llm_model.strip() or DEFAULT_MODEL

    price_line = f"Price: £{price}\n" if price else ""
    user_message = (
        f"Product name: {name}\n{price_line}\n"
        f"Product description:\n{description_text[:6000]}"
    )

    try:
        with httpx.Client(timeout=LLM_TIMEOUT) as client:
            response = client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.llm_api_key}",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system",
                         "content": settings.llm_system_prompt},
                        {"role": "user", "content": user_message},
                    ],
                },
            )
            body = _explain_http(response)
            choices = body.get("choices") or []
            text = (choices[0].get("message", {}).get("content", "")
                    if choices else "")
    except httpx.HTTPError as exc:
        raise LlmError(f"Could not reach OpenRouter: {exc}") from exc

    if not text.strip():
        raise LlmError("The model returned an empty response.")
    return _parse_response(text)


def _explain_http(response: httpx.Response) -> dict:
    if response.status_code == 200:
        return response.json()
    detail = response.text[:300]
    try:
        body = response.json()
        detail = (body.get("error", {}) or {}).get("message") or detail
    except Exception:
        pass
    if response.status_code in (401, 403):
        raise LlmError(f"The API key was rejected ({response.status_code}). "
                       f"Check it in Settings. The API said: {detail}")
    raise LlmError(f"LLM request failed ({response.status_code}): {detail}")
