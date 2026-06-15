"""
Consent and authorization — the gate in front of every capture.

horizon-assist does nothing to a session until the user has (1) acknowledged the
responsibility notice once, and (2) accepted the per-session scope screen that states
exactly what will be accessed, what (if anything) leaves the machine, where data is
stored, the retention policy, and how to clear it.

This module is UI-agnostic: it builds the notice text and persists the one-time
acknowledgment. The CLI renders it with cli_consent(); the tray renders the same
text in a dialog (see src/tray.py). Nothing here captures anything.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

CONSENT_VERSION = 1

RESPONSIBILITY_NOTICE = (
    "RESPONSIBILITY NOTICE\n"
    "You are responsible for ensuring your use of this tool complies with the "
    "policies of the systems you access and with any applicable laws and "
    "regulations. It is intended only for systems you own or are explicitly "
    "authorized to use — for example your own infrastructure, a personal or home "
    "VDI, or an IT-sanctioned deployment.\n"
    "This tool does not hide its activity, does not run in the background, and is "
    "not a means of covertly extracting data from a controlled corporate "
    "environment. If you are not authorized to use AI assistance against the "
    "system you are connecting to, do not use this tool against it."
)


def _data_dir(config: dict) -> Path:
    """The local directory where all captured data lives."""
    events_db = config.get("rag", {}).get("events_db", "./data/events.db")
    return Path(events_db).resolve().parent


def _retention_label(config: dict) -> str:
    retention = str(config.get("session", {}).get("retention", "session")).strip()
    if retention in ("session", "session-only", ""):
        return "session-only (everything captured is deleted when the session ends)"
    if retention.endswith("d") and retention[:-1].isdigit():
        return f"{retention[:-1]} day(s), then captured notes expire automatically"
    return retention


def _capture_label(config: dict) -> str:
    mode = str(config.get("capture", {}).get("mode", "vision")).strip().lower()
    if mode == "ocr":
        return (
            "Local OCR only — captured screenshots are read on this machine and are "
            "NOT sent to any cloud service."
        )
    return (
        "Cloud vision — when you capture a screen, that single screenshot is sent to "
        "Anthropic (Claude Haiku) to read its text. No other content is sent."
    )


def scope_text(config: dict) -> str:
    """The per-session scope/consent screen, built from the active config."""
    data_dir = _data_dir(config)
    titles = ", ".join(config.get("windows", {}).get("monitor_titles", ["the Horizon window"]))
    return (
        "horizon-assist — session scope\n"
        "\n"
        "This tool will, ONLY when you explicitly trigger an action:\n"
        f"  • Capture a screenshot of your remote-desktop window ({titles}).\n"
        "  • Read clipboard text — only when you use the code-editing bridge.\n"
        "\n"
        "It will NOT:\n"
        "  • Capture anything on a timer or in the background.\n"
        "  • Watch for mentions of you or anyone else.\n"
        "  • Send anything you did not explicitly act on.\n"
        "\n"
        "What leaves this machine:\n"
        f"  • Capture: {_capture_label(config)}\n"
        "  • Ask: your question plus a few locally-retrieved snippets go to Anthropic\n"
        "    (Claude Sonnet) to compose an answer. Nothing else.\n"
        "\n"
        "Where your data is stored (local only — never uploaded):\n"
        f"  • {data_dir}\n"
        "\n"
        f"Retention: {_retention_label(config)}\n"
        "\n"
        "How to clear it: 'Stop / clear session' ends this session (and, with "
        "session-only retention, deletes its data); 'Purge all stored data' wipes "
        "everything this tool has ever stored."
    )


def _ack_path(config: dict) -> Path:
    return _data_dir(config) / ".consent_ack.json"


def needs_first_run_ack(config: dict) -> bool:
    """True until the user has acknowledged the responsibility notice (this version)."""
    path = _ack_path(config)
    if not path.exists():
        return True
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return int(data.get("version", 0)) < CONSENT_VERSION
    except Exception:
        return True


def record_first_run_ack(config: dict) -> None:
    """Persist that the user accepted the responsibility notice."""
    path = _ack_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": CONSENT_VERSION,
                "acknowledged_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )


def cli_consent(config: dict) -> bool:
    """Show the notice(s) on the terminal and require an explicit yes to proceed.

    Returns True only if the user consents. Records the one-time responsibility
    acknowledgment on first acceptance.
    """
    first_run = needs_first_run_ack(config)
    if first_run:
        print(RESPONSIBILITY_NOTICE)
        print()
    print(scope_text(config))
    print()
    try:
        answer = input("Type 'yes' to start a consented assist session: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print()
        return False
    if answer != "yes":
        print("No consent given — nothing was captured.")
        return False
    if first_run:
        record_first_run_ack(config)
    return True
