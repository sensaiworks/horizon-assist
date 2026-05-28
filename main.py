"""
Entry point for horizon-monitor.

Commands:
  python main.py monitor           # start monitoring loop
  python main.py monitor --dry-run # test poll loop without LLM
  python main.py query "..."       # one-shot question
  python main.py agent             # interactive REPL

Configuration is loaded from config.toml in the project root.
API keys are loaded from .env (copy from .env.example).
"""

from __future__ import annotations

import asyncio
import sys
import tomllib
from pathlib import Path

import click
from dotenv import load_dotenv
import os

load_dotenv()

CONFIG_PATH = Path(__file__).parent / "config.toml"


def load_config() -> dict:
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


@click.group()
def cli():
    pass


@cli.command()
@click.option("--dry-run", is_flag=True, help="Poll without calling LLM or notifying")
def monitor(dry_run: bool):
    """Start the monitoring loop."""
    config = load_config()
    asyncio.run(_run_monitor(config, dry_run))


async def _run_monitor(config: dict, dry_run: bool) -> None:
    from src.mcp_client import HorizonMCPClient
    from src.poller import Poller

    mcp_cfg = config["mcp"]
    poll_cfg = config["polling"]
    win_cfg = config["windows"]

    async with HorizonMCPClient(
        server_path=mcp_cfg["server_path"],
        command=mcp_cfg["command"],
    ) as client:
        poller = Poller(
            client=client,
            interval=poll_cfg["interval_seconds"],
            change_threshold=poll_cfg["change_threshold"],
            screen_index=poll_cfg["screen_index"],
            monitor_titles=win_cfg["monitor_titles"],
            focus_before_shot=poll_cfg["focus_before_shot"],
        )

        async def on_change(state, png: bytes) -> None:
            print(f"CHANGED  {state.screenshot_hash}  [{state.window_title}]")

        print(f"Starting monitor (dry_run={dry_run}, interval={poll_cfg['interval_seconds']}s) — Ctrl+C to stop")
        await poller.run(on_change=on_change, dry_run=dry_run)


@cli.command()
@click.argument("question")
def query(question: str):
    """Ask a question about recorded conversations."""
    config = load_config()
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    # TODO: connect RAGPipeline, instantiate QueryAgent, call agent.query(question)
    print("query not yet implemented — see CLAUDE.md Step 5")


@cli.command()
def agent():
    """Start interactive query REPL."""
    config = load_config()
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    # TODO: connect RAGPipeline, instantiate QueryAgent, call agent.repl()
    print("agent not yet implemented — see CLAUDE.md Step 6")


if __name__ == "__main__":
    cli()
