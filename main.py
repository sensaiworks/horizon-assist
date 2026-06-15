"""
Entry point for horizon-assist.

A consent-based, local-first personal assistant for working inside Horizon/VDI
sessions you own or are authorized to use. Nothing is captured in the background or
on a timer; every capture and query is an explicit, user-initiated action.

Commands:
  python main.py tray               # system-tray launcher (start a consented session)
  python main.py session            # interactive consented session (capture/ask/view)
  python main.py ask "..."          # one-shot question over notes already stored
  python main.py view               # list what is stored locally
  python main.py purge              # delete all stored data
  python main.py remote <action>    # opt-in remote control + code-editing bridge

Configuration is loaded from config.toml in the project root.
API keys are loaded from .env (copy from .env.example).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - older interpreters
    import tomli as tomllib

import click
from dotenv import load_dotenv

load_dotenv()

CONFIG_PATH = Path(__file__).parent / "config.toml"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise click.ClickException(
            f"{CONFIG_PATH.name} not found — copy config.example.toml to config.toml and edit it"
        )
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


def require_api_key() -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise click.ClickException(
            "ANTHROPIC_API_KEY not set — copy .env.example to .env and fill it in"
        )
    return api_key


def authorization_banner() -> None:
    """One-line authorization reminder shown when an action could touch the remote."""
    click.echo(
        "horizon-assist: use only on systems you own or are authorized to use; "
        "you are responsible for complying with their policies.",
        err=True,
    )


@click.group()
def cli():
    pass


@cli.command()
def tray():
    """Launch the system-tray session launcher (start a consented assist session)."""
    config = load_config()
    api_key = require_api_key()
    authorization_banner()
    from src.tray import AssistantTray
    AssistantTray(config, api_key).run()


@cli.command()
def session():
    """Start an interactive, consented assist session in the terminal.

    Shows the consent/scope screen, then accepts commands:
      capture        capture the remote screen once and store what it reads
      ask <question>  answer a question over notes captured this session
      view           list what is stored
      exit / end     end the session (clears its data under session-only retention)
    """
    config = load_config()
    api_key = require_api_key()
    authorization_banner()

    from src import consent
    from src.session import AssistSession

    if not consent.cli_consent(config):
        return

    sess = AssistSession(config, api_key)
    sess.connect()
    click.echo(f"\nSession {sess.session_id} started. "
               f"Commands: capture | ask <q> | view | exit\n")
    try:
        while True:
            try:
                line = input("assist> ").strip()
            except (KeyboardInterrupt, EOFError):
                line = "exit"
            if not line:
                continue
            cmd, _, rest = line.partition(" ")
            cmd = cmd.lower()
            if cmd in ("exit", "quit", "end"):
                break
            elif cmd == "capture":
                new, locked = asyncio.run(sess.capture())
                if locked:
                    click.echo("  remote screen is locked — nothing to read")
                else:
                    click.echo(f"  captured {len(new)} new note(s) ({sess.count()} stored)")
            elif cmd == "ask":
                if not rest.strip():
                    click.echo("  usage: ask <question>")
                    continue
                sess.ask(rest.strip())
                click.echo("")
            elif cmd == "view":
                _print_notes(sess.list_notes(limit=50), sess.count())
            else:
                click.echo("  unknown command — use: capture | ask <q> | view | exit")
    finally:
        removed = sess.end()
        if sess.session_only:
            click.echo(f"\nSession ended — cleared {removed} note(s) (session-only retention).")
        else:
            click.echo("\nSession ended — notes retained per retention setting.")


@cli.command()
@click.argument(
    "action",
    type=click.Choice(
        [
            "unlock", "launch", "activate", "run", "foreground", "reply",
            "read-file", "write-file", "open",
        ]
    ),
)
@click.argument("value", required=False, default="")
@click.option(
    "--out", default="",
    help="read-file: write the pulled text to this local file instead of stdout",
)
@click.option(
    "--save/--no-save", default=False,
    help="write-file: press Ctrl+S in the remote after pasting",
)
def remote(action: str, value: str, out: str, save: bool):
    """Drive the remote Horizon session (WRITE actions; requires [control].enabled).

    Examples:
      python main.py remote foreground
      python main.py remote launch "Microsoft Teams"
      python main.py remote unlock
      python main.py remote reply "on it, thanks"

    Code-editing bridge (pull a file out of the remote, edit locally, push back):
      python main.py remote open "src/app.py"            # focus a file in remote VS Code
      python main.py remote read-file --out data/pull.txt
      python main.py remote write-file data/pull.txt --save
    """
    config = load_config()
    ctl = config.get("control", {})
    if not ctl.get("enabled", False):
        raise click.ClickException(
            "Remote control disabled — set [control].enabled = true in config.toml"
        )
    authorization_banner()
    asyncio.run(_run_remote(config, action, value, out, save))


async def _run_remote(
    config: dict, action: str, value: str, out: str, save: bool
) -> None:
    from src.mcp_client import HorizonMCPClient
    from src.controller import RemoteController

    mcp_cfg = config["mcp"]
    ctl = config.get("control", {})

    async with HorizonMCPClient(mcp_cfg["server_path"], mcp_cfg["command"]) as client:
        c = RemoteController(
            client,
            focus_target=ctl.get("focus_target", "PVDI"),
            launch_wait=ctl.get("launch_wait_seconds", 1.5),
            clipboard_sync=ctl.get("clipboard_sync_seconds", 0.6),
            copy_timeout=ctl.get("copy_timeout_seconds", 6.0),
        )
        print(f"remote: {action} {value!r}", flush=True)
        if action == "foreground":
            await c.bring_to_front()
        elif action == "unlock":
            password = os.environ.get("HORIZON_PASSWORD", "")
            if not password:
                raise click.ClickException("HORIZON_PASSWORD not set in .env")
            await c.unlock(password)
        elif action in ("launch", "activate"):
            if not value:
                raise click.ClickException(f"'{action}' needs an app name")
            await c.launch_or_activate(value)
        elif action == "run":
            if not value:
                raise click.ClickException("'run' needs a command")
            await c.run_command(value)
        elif action == "reply":
            if not value:
                raise click.ClickException("'reply' needs message text")
            await c.send_reply(value)
        elif action == "open":
            if not value:
                raise click.ClickException("'open' needs a file path")
            await c.open_file(value)
        elif action == "read-file":
            text = await c.copy_from_remote()
            if out:
                out_path = Path(out)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(text, encoding="utf-8")
                print(f"remote: pulled {len(text)} chars -> {out}", flush=True)
            else:
                print(text)
        elif action == "write-file":
            if not value:
                raise click.ClickException("'write-file' needs a local file path")
            text = Path(value).read_text(encoding="utf-8")
            await c.paste_to_remote(text, replace_all=True, save=save)
            print(
                f"remote: pasted {len(text)} chars from {value}"
                f"{' and saved' if save else ''}",
                flush=True,
            )
        print("remote: done", flush=True)


@cli.command()
@click.argument("question", required=False, default="")
def ask(question: str):
    """Answer a question over notes already stored locally (no new capture).

    With the default session-only retention, notes exist only during a live
    session, so use `session` to capture and ask together. `ask` is for when a
    longer retention is configured and notes persist across runs.
    """
    config = load_config()
    api_key = require_api_key()

    from src.rag import RAGPipeline
    from src.agent import QueryAgent

    rag_cfg = config["rag"]
    rag = RAGPipeline(
        db_path=rag_cfg["db_path"],
        collection_name=rag_cfg["collection_name"],
        embedding_provider=rag_cfg["embedding_provider"],
        voyage_api_key=os.environ.get("VOYAGE_API_KEY") or None,
        top_k=rag_cfg["top_k"],
    )
    rag.connect()
    agent_obj = QueryAgent(
        api_key=api_key,
        model=config["claude"]["query_model"],
        rag=rag,
        max_tokens=config["claude"]["max_tokens"],
    )
    if question:
        agent_obj.query(question)
    else:
        agent_obj.repl()


@cli.command()
@click.option("--limit", default=50, help="How many notes to show (newest first)")
def view(limit: int):
    """List what is currently stored locally — full transparency, not a black box."""
    config = load_config()
    from src.store import EventStore

    store = EventStore(config["rag"].get("events_db", "./data/events.db"))
    store.connect()
    try:
        _print_notes(store.recent(limit=limit), store.count())
    finally:
        store.close()


@cli.command()
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt")
def purge(yes: bool):
    """Delete ALL stored data (SQLite notes + vector embeddings). Irreversible."""
    config = load_config()
    if not yes:
        if not click.confirm("Delete ALL notes this tool has ever stored?"):
            click.echo("Cancelled — nothing deleted.")
            return

    from src.store import EventStore
    from src.rag import RAGPipeline

    store = EventStore(config["rag"].get("events_db", "./data/events.db"))
    store.connect()
    removed = store.purge_all()
    store.close()

    rag_cfg = config["rag"]
    rag = RAGPipeline(
        db_path=rag_cfg["db_path"],
        collection_name=rag_cfg["collection_name"],
        embedding_provider=rag_cfg["embedding_provider"],
        voyage_api_key=os.environ.get("VOYAGE_API_KEY") or None,
        top_k=rag_cfg["top_k"],
    )
    rag.connect()
    rag.purge_all()
    click.echo(f"Purged {removed} stored note(s) and cleared the vector store.")


def _print_notes(notes: list[dict], total: int) -> None:
    click.echo(f"{total} note(s) stored locally:")
    if not notes:
        click.echo("  (nothing stored)")
        return
    for n in notes:
        when = n.get("chat_time") or (n.get("observed_at", "")[:16])
        ch = f" «{n.get('channel')}»" if n.get("channel") else ""
        click.echo(f"  [{when}] {n.get('app','')}{ch} {n.get('speaker','')}: {n.get('message','')}")


if __name__ == "__main__":
    cli()
