"""
Claude Vision extraction — screenshot → (list[MessageEvent], is_lock_screen).

Turns a screenshot the user *explicitly captured* during a consented assist session
into structured notes. This is never run on a timer or in the background; it only
runs when the user triggers a capture. The PNG of that one frame is sent to Claude
Haiku (this is stated on the consent screen — see src/consent.py) and parsed into
MessageEvent objects, plus a bool for whether the screen was locked (nothing to read).

Model: claude-haiku-4-5-20251001 (fast, cheap, sufficient for UI parsing)
"""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone

import anthropic

from .models import MessageEvent

_MAX_TOKENS = 1024

EXTRACTION_PROMPT = """\
You are analyzing a single screenshot that the user captured of their own
remote-desktop session, to help them keep notes on what is on screen.

Return a JSON object ONLY, no explanation, no markdown:
{
  "lock_screen": true | false,
  "messages": [
    {
      "speaker": "Full Name or username",
      "message": "exact message text",
      "app": "teams" | "symphony" | "unknown",
      "channel": "conversation / channel / room / thread name shown in the UI",
      "time": "the message's on-screen timestamp exactly as shown"
    }
  ]
}

lock_screen is true if the screen shows a Windows lock screen (clock visible,
"Press Ctrl+Alt+Delete to unlock", dark/black screen with no content).

If lock_screen is true, messages must be [].

messages contains the visible chat messages from Microsoft Teams or Symphony.

channel is the name of the open conversation, channel, room, or thread (e.g.
"Deployments", "John Smith", "Trading Desk") — read it from the header or the
selected item in the sidebar. Use "" if you cannot tell.

time is the timestamp shown next to or above the message, copied verbatim (e.g.
"10:32 AM", "Yesterday 14:05", "Mon 09:14"). Use "" if no time is visible for it.

If no chat app is visible or no messages are present, return messages: [].
"""


class Extractor:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model

    async def extract(
        self, png_bytes: bytes, window_title: str = "", session_id: str = ""
    ) -> tuple[list[MessageEvent], bool]:
        """
        Send one user-captured screenshot to Claude Vision.
        Returns (events, is_lock_screen). Events are tagged with session_id.
        """
        b64 = base64.b64encode(png_bytes).decode()
        prompt = EXTRACTION_PROMPT

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
        items, is_lock_screen = self._parse_response(raw)
        now = datetime.now(timezone.utc)
        events = []
        for item in items:
            try:
                events.append(
                    MessageEvent(
                        timestamp=now,
                        speaker=item.get("speaker", "unknown"),
                        message=item.get("message", ""),
                        app=item.get("app", "unknown"),
                        channel=(item.get("channel") or "").strip(),
                        chat_time=(item.get("time") or "").strip(),
                        session_id=session_id,
                        window_title=window_title,
                    )
                )
            except Exception:
                pass
        return events, is_lock_screen

    def _parse_response(self, text: str) -> tuple[list[dict], bool]:
        """Parse model response into (messages, is_lock_screen)."""
        text = text.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data.get("messages", []), bool(data.get("lock_screen", False))
            if isinstance(data, list):
                # backward-compat with old prompt format
                return data, False
        except json.JSONDecodeError:
            pass
        return [], False
