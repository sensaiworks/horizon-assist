"""
Claude Vision extraction — screenshot → list[MessageEvent].

Sends the PNG screenshot to Claude Haiku with a structured prompt.
Returns parsed MessageEvent objects.

Model: claude-haiku-4-5-20251001 (fast, cheap, sufficient for UI parsing)

PROMPT STRATEGY:
  - Ask for JSON only (no explanation) — easier to parse, fewer tokens
  - Include user's display name so it can detect directed_at_user accurately
  - Instruct it to return [] for lock screens / non-chat content
  - Use image/png media type with base64 data

COST ESTIMATE:
  A 1920x1080 screenshot is ~500KB PNG → ~3KB base64 → ~700 input tokens
  At Haiku pricing (~$0.25/MTok input): ~$0.0002 per screenshot
  At 1 change/minute: ~$0.01/hour, ~$7/month — very affordable

TODO (Step 2): implement `extract()`.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime

import anthropic

from .models import MessageEvent

_MAX_TOKENS = 1024

EXTRACTION_PROMPT = """\
You are analyzing a screenshot of a Windows remote desktop showing a chat application
(Microsoft Teams or Symphony). Extract all visible chat messages that are NEW or
recent — ignore old/scrolled-away content if you can tell the difference.

Return a JSON array ONLY, no explanation, no markdown:
[
  {{
    "speaker": "Full Name or username",
    "message": "exact message text",
    "app": "teams" | "symphony" | "unknown",
    "directed_at_user": true | false
  }}
]

directed_at_user is true if: the message @mentions "{user}", uses their first name
"{user}" directly in conversation, or is a direct/private message thread to them.

If the screen shows a lock screen, desktop, or no chat app: return [].
If no messages are visible: return [].
"""


class Extractor:
    def __init__(self, api_key: str, model: str, user_display_name: str) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model
        self._user = user_display_name

    async def extract(self, png_bytes: bytes, window_title: str = "") -> list[MessageEvent]:
        """Send screenshot to Claude Vision, parse response into MessageEvent list."""
        b64 = base64.b64encode(png_bytes).decode()
        prompt = self._build_prompt()

        response = await self._client.messages.create(
            model=self._model,
            max_tokens=_MAX_TOKENS,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )

        raw = response.content[0].text
        items = self._parse_response(raw)
        now = datetime.utcnow()
        events = []
        for item in items:
            try:
                events.append(
                    MessageEvent(
                        timestamp=now,
                        speaker=item.get("speaker", "unknown"),
                        message=item.get("message", ""),
                        app=item.get("app", "unknown"),
                        directed_at_user=bool(item.get("directed_at_user", False)),
                        window_title=window_title,
                    )
                )
            except Exception:
                pass
        return events

    def _parse_response(self, text: str) -> list[dict]:
        """Extract JSON array from model response, handling markdown fences."""
        text = text.strip()
        # Strip markdown code fences if present
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        try:
            data = json.loads(text)
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []

    def _build_prompt(self) -> str:
        return EXTRACTION_PROMPT.format(user=self._user)
