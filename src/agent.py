"""
Query agent — natural language Q&A over the notes the user captured.

It only ever sees notes that the user explicitly captured in a consented session;
there is no background collection behind it. A query sends the user's question plus
the small set of locally-retrieved snippets to Claude Sonnet (claude-sonnet-4-6) for
answer quality — nothing else leaves the machine.

Applies cache_control to the system prompt — it's stable across all queries so the
first call writes it to cache; every subsequent call reads it cheaply.

Two modes:
  1. One-shot: `python main.py ask "what did John say about the outage?"`
  2. Interactive REPL: `python main.py ask`
"""

from __future__ import annotations

from typing import Callable

import anthropic

from .rag import RAGPipeline

SYSTEM_PROMPT = """\
You are a personal assistant for the user's own remote-desktop work session. The
user captures screenshots when they want help, and those captures are turned into
the notes you are given as context. You only ever see what the user chose to capture.

Answer questions about these notes accurately and concisely. When referencing a
message, include the speaker name and approximate time. If the information is not in
the provided context, say so plainly — do not guess or invent.
"""


class QueryAgent:
    def __init__(self, api_key: str, model: str, rag: RAGPipeline, max_tokens: int = 1024) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._rag = rag
        self._max_tokens = max_tokens

    def query(
        self,
        question: str,
        on_chunk: Callable[[str], None] | None = None,
        session_id: str | None = None,
    ) -> str:
        """
        Retrieve note context, stream an answer, return full response text.
        If on_chunk is provided, each streamed token is passed to it instead
        of being printed to stdout (used by the tray Ask dialog).
        Pass session_id to keep the answer scoped to one session's notes.
        """
        results = self._rag.query(question, session_id=session_id)
        context = self._rag.format_context(results)

        if context:
            user_content = f"Context (notes you captured):\n{context}\n\nQuestion: {question}"
        else:
            user_content = f"Question: {question}\n\n(No relevant notes found in what you have captured.)"

        full_text = ""
        with self._client.messages.stream(
            model=self._model,
            max_tokens=self._max_tokens,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_content}],
        ) as stream:
            for text in stream.text_stream:
                if on_chunk:
                    on_chunk(text)
                else:
                    print(text, end="", flush=True)
                full_text += text
        if not on_chunk:
            print()
        return full_text

    def repl(self) -> None:
        """Interactive REPL loop. Type 'exit' or Ctrl+C to quit."""
        count = self._rag.count()
        print(f"horizon-assist — {count} captured note(s) available. Type 'exit' to quit.\n")
        while True:
            try:
                question = input("You: ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nGoodbye.")
                break
            if not question:
                continue
            if question.lower() in ("exit", "quit", "bye"):
                print("Goodbye.")
                break
            print("Agent: ", end="", flush=True)
            self.query(question)
            print()
