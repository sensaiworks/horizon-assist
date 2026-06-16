# horizon-assist

A consent-based, local-first personal AI assistant for working inside Horizon/VDI
sessions you own or are authorized to use.

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows-0078D6)
![License](https://img.shields.io/badge/license-MIT-green)

## What it is

`horizon-assist` is a Windows tool that helps you work inside a remote desktop running
in the Omnissa Horizon Client. It does two things, and only ever when you ask it to:

- **Capture & ask** — you capture the current screen on demand; the tool reads the chat
  on it into local notes and lets you ask questions about them in plain English.
- **Code-editing bridge** — it pulls a file out of an editor in the remote session,
  lets a local AI agent edit it, and pastes the whole document back — for VDIs that
  block AI assistants inside the session.

There is **no background monitoring**. Nothing is captured on a timer, nothing runs
silently, and the tool does not watch for mentions of you or anyone else. Every capture
and every query is a deliberate action you take.

It captures the screen through [`horizon-mcp`](#companion-project), a small companion
MCP server that exposes screenshot and window controls over stdio.

## The problem it solves

People working in locked-down VDI environments often have no AI assistance over their
own work: the corporate session has no API, no accessibility tree, and frequently no
licensed AI tooling, while the user has capable AI on their local machine. `horizon-assist`
bridges that gap **locally and transparently** — it brings your own local AI to the work
you are already doing in a session you are authorized to use, without sending your data
to a third-party service beyond the model calls you explicitly trigger.

## Scope & consent model

Consent is explicit and per-session:

- On first run you must acknowledge a **responsibility notice** (below).
- At the **start of every session** a scope screen states exactly what will be accessed,
  what (if anything) leaves the machine, where data is stored, the retention policy, and
  how to clear it. **Nothing is captured until you accept it.**
- A **Stop / clear session** control is always available, and there is a one-click
  **Purge all stored data**.

What the tool accesses, and only on an explicit action:

- A **screenshot** of the remote-desktop window you point it at, when you click *Capture now*.
- **Clipboard text**, only when you use the code-editing bridge.

What it will **not** do: capture on a timer, run in the background, watch for mentions,
or send anything you did not explicitly act on.

## Data handling

- **Local-first.** Captured notes live in local stores under `./data/` — a SQLite
  database (`events.db`) and a ChromaDB vector store (`chromadb/`). Captured content is
  **never uploaded**.
- **Retention, minimal by default.** The default is **session-only**: everything captured
  is deleted when the session ends. A configurable `"<N>d"` policy keeps notes for N days
  and then expires them automatically.
- **Purge.** `Purge all stored data` (tray) or `python main.py purge` deletes everything
  in both stores.
- **Transparency.** `View stored data` (tray) or `python main.py view` lists exactly what
  is stored — it is not a black box.

What leaves the machine, stated plainly:

| Action  | What is sent                                              | To        |
| ------- | --------------------------------------------------------- | --------- |
| Capture | the single screenshot you captured (in `vision` mode)     | Anthropic (Claude Haiku) |
| Ask     | your question + a few locally-retrieved note snippets     | Anthropic (Claude Sonnet) |

Setting `[capture].mode = "ocr"` reads captured screens **locally** with Windows OCR and
sends no image to the cloud (lower accuracy). Embeddings can also run fully offline
(`embedding_provider = "local"`).

## Intended users

People who **own or are authorized for** the systems involved: consultants on their own
infrastructure, researchers, personal or home VDI users, and IT-sanctioned enterprise
deployments. It is not a tool for covertly extracting data from controlled corporate
environments.

## Responsibility notice

You are responsible for ensuring your use of this tool complies with the policies of the
systems you access and with any applicable laws and regulations. It is intended only for
systems you own or are explicitly authorized to use. This tool does not hide its activity,
does not run in the background, and is not a means of bypassing employer controls. If you
are not authorized to use AI assistance against the system you are connecting to, do not
use this tool against it.

## Architecture & security

```
 You click an action (Capture / Ask / a remote command)
            │
            ▼
   Consent gate ── per session, before anything is captured
            │
            ▼
   Capture (one screenshot, on demand)  ──►  Extractor (Claude Haiku reads the text)
            │                                         │
            ▼                                         ▼
   Code-editing bridge (clipboard)        Local stores:  SQLite notes + ChromaDB vectors
                                                   │       (session-tagged, local only)
                                                   ▼
                                          Ask  ──►  Claude Sonnet over retrieved snippets
```

- **Local-first storage.** SQLite + ChromaDB on disk under `./data/` (gitignored).
  Each note is tagged with its session id so a session's data can be purged as a unit.
- **What is sent and when** is the table above — only on actions you take.
- **How to purge:** end the session (session-only retention clears it automatically) or
  run the explicit purge action.
- **Remote control is opt-in.** Write actions into the session (unlock, launch an app,
  the code-editing bridge) are gated behind `[control].enabled`, off by default, and only
  run when you trigger them. The tool does not conceal these actions.
- **Secrets.** `ANTHROPIC_API_KEY` and the optional `HORIZON_PASSWORD` live in `.env`
  (gitignored). The password is used only by the explicit unlock action, pasted into the
  login prompt, and cleared from the clipboard afterward.

## Requirements

- **Python 3.11+** (uses stdlib `tomllib`; the `tomli` backport installs automatically on
  older versions).
- **Node.js** — to run the companion `horizon-mcp` server.
- An **`ANTHROPIC_API_KEY`**.

## Setup

```powershell
git clone https://github.com/sensaiworks/horizon-assist.git
cd horizon-assist

python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

copy .env.example .env                 # then fill in ANTHROPIC_API_KEY
copy config.example.toml config.toml   # then adjust paths / settings
```

Both `.env` and `config.toml` are gitignored — keep secrets and machine-specific paths
out of version control.

## Usage

```powershell
# System-tray launcher: click "Start assist session", accept the consent screen,
# then use Capture now / Ask / View stored data / Stop / Purge.
.venv\Scripts\pythonw main.py tray
# ...or double-click "Start horizon-assist.bat"

# Interactive consented session in the terminal
python main.py session
#   assist> capture
#   assist> ask what did John say about the deployment?
#   assist> view
#   assist> exit            (clears session data under session-only retention)

# Inspect / clear stored data
python main.py view
python main.py purge

# Opt-in remote control + code-editing bridge (requires [control].enabled = true)
python main.py remote foreground
python main.py remote open "src/app.py"
python main.py remote read-file --out data/pull.txt        # pull a file out via the clipboard
python main.py remote read-file --ocr --out data/pull.txt  # ...or read it locally with OCR
python main.py remote write-file data/pull.txt --save      # edit locally, paste it back
```

### Code-editing bridge

Corporate VDIs often run an IDE (e.g. VS Code) but block AI assistants inside the session.
This bridge brings the AI to the code: it reads a file out of the remote editor, lets a
local agent edit it, and pastes the whole document back. Each direction uses whatever
transport the VDI permits:

- **Write (local → remote): the clipboard.** `write-file` stages the new text on the
  clipboard and pastes it into the focused remote editor. This path is exact.
- **Read (remote → local): the clipboard if copy-out is allowed, otherwise OCR.** Where
  Horizon clipboard redirection permits copy-out, `read-file` copies the file with
  `Ctrl+A`/`Ctrl+C` and captures it losslessly. Many locked-down VDIs allow paste-in but
  **block copy-out**; there, `read-file --ocr` reads the screen the way you would by hand —
  it screenshots the remote and runs local Windows OCR (free, offline, nothing leaves the
  machine), scrolling and stitching across screens with `--pages`/`--scroll-at` for files
  taller than the viewport. OCR is for reading, not a byte-exact pull, so it is paired with
  the exact paste-back write path. Use `--region x,y,w,h` to target the editor pane — and,
  on multi-monitor setups, the screen the Horizon client is on.

Before sending any input the bridge verifies the Horizon client actually holds the
foreground and refuses to act otherwise, so keystrokes and clipboard actions are never sent
to a local window by mistake.

## Configuration

All tunables live in `config.toml` (see `config.example.toml`): the MCP server path,
window titles to capture, capture mode (`vision`/`ocr`) and screen index, retention,
model IDs, and the embedding provider. API keys and the optional remote-desktop password
live in `.env`.

## Companion project

[`horizon-mcp`](https://github.com/sensaiworks/horizon-mcp) — the MCP server that provides
screenshot and window control over stdio. It is run, not modified, by this project.

## Author

**Mike Lezinsky** — SensAI Works ([hello@sensaiworks.com](mailto:hello@sensaiworks.com))

## License

Released under the [MIT License](LICENSE). © 2026 SensAI Works.
