"""
Windows system-tray launcher for horizon-assist.

This is a launcher, NOT a daemon. It never captures anything on its own. The user
starts a consented session, then takes explicit actions:

  Start assist session   → shows the consent/scope screen; nothing is captured until
                           the user accepts it.
  Capture now            → grabs ONE screenshot on demand and reads it into notes.
  Ask…                   → answers a question over the notes captured this session.
  View stored data       → lists what is currently stored (not a black box).
  Stop / clear session    → ends the session and, with session-only retention,
                           deletes its data.
  Purge all stored data  → wipes everything this tool has ever stored.

Optional, separately-gated remote-control actions (unlock / launch app / bring to
front) appear only when [control].enabled is set — they are user-triggered, never
automatic. See src/controller.py.
"""

from __future__ import annotations

import asyncio
import threading

from PIL import Image, ImageDraw
import pystray

from . import consent
from .controller import RemoteController
from .mcp_client import HorizonMCPClient
from .session import AssistSession

_STATUS_COLORS = {
    "idle":   (107, 114, 128),
    "active": (34, 197, 94),
}


def _make_icon(status: str) -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = _STATUS_COLORS.get(status, _STATUS_COLORS["idle"])
    d.ellipse([2, 2, size - 2, size - 2], fill=(20, 20, 30, 255))
    d.ellipse([8, 8, size - 8, size - 8], fill=(*color, 255))
    # A simple "A" (assist) glyph.
    d.line([(20, 44), (32, 18), (44, 44)], fill=(255, 255, 255, 230), width=4)
    d.line([(25, 34), (39, 34)], fill=(255, 255, 255, 230), width=4)
    return img


class AssistantTray:
    def __init__(self, config: dict, api_key: str) -> None:
        self._config = config
        self._api_key = api_key
        self._control_enabled = bool(config.get("control", {}).get("enabled", False))
        self._session: AssistSession | None = None
        self._icon: pystray.Icon | None = None

    # ------------------------------------------------------------------ menu
    def _menu_items(self):
        items = [pystray.MenuItem("horizon-assist", None, enabled=False)]

        if self._session is None:
            items += [
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Start assist session", self._on_start_session),
                pystray.MenuItem("View stored data", self._on_view_data),
                pystray.MenuItem("Purge all stored data", self._on_purge_all),
            ]
        else:
            count = self._safe_count()
            items += [
                pystray.MenuItem("● Session active", None, enabled=False),
                pystray.MenuItem(f"Notes captured: {count}", None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Capture now", self._on_capture, default=True),
                pystray.MenuItem("Ask…", self._on_ask),
                pystray.MenuItem("View stored data", self._on_view_data),
            ]
            if self._control_enabled:
                items += [
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("Bring Horizon to front", self._on_bring_to_front),
                    pystray.MenuItem("Launch / activate app on remote…", self._on_launch_app),
                    pystray.MenuItem("Unlock remote desktop", self._on_unlock),
                ]
            items += [
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Stop / clear session", self._on_stop_session),
                pystray.MenuItem("Purge all stored data", self._on_purge_all),
            ]

        items += [pystray.Menu.SEPARATOR, pystray.MenuItem("Quit", self._on_quit)]
        return tuple(items)

    def _refresh(self) -> None:
        if not self._icon:
            return
        status = "active" if self._session else "idle"
        self._icon.icon = _make_icon(status)
        self._icon.title = f"horizon-assist — {status}"
        self._icon.menu = pystray.Menu(self._menu_items)

    def _safe_count(self) -> int:
        try:
            return self._session.count() if self._session else 0
        except Exception:
            return 0

    # --------------------------------------------------------- session start
    def _on_start_session(self, icon=None, item=None) -> None:
        threading.Thread(target=self._start_session_worker, daemon=True).start()

    def _start_session_worker(self) -> None:
        if not self._consent_dialog():
            print("No consent given — session not started.", flush=True)
            return
        self._session = AssistSession(self._config, self._api_key)
        try:
            self._session.connect()
        except Exception as exc:
            print(f"Session: failed to open local stores: {exc}", flush=True)
        print(f"Assist session started: {self._session.session_id}", flush=True)
        self._refresh()

    def _consent_dialog(self) -> bool:
        """Modal scope/consent screen. Returns True only on explicit Accept."""
        import tkinter as tk
        from tkinter import scrolledtext

        result = {"ok": False}
        first_run = consent.needs_first_run_ack(self._config)

        root = tk.Tk()
        root.title("horizon-assist — consent")
        root.geometry("640x560")

        body = ""
        if first_run:
            body += consent.RESPONSIBILITY_NOTICE + "\n\n" + ("-" * 60) + "\n\n"
        body += consent.scope_text(self._config)

        txt = scrolledtext.ScrolledText(root, font=("Segoe UI", 10), wrap=tk.WORD)
        txt.insert(tk.END, body)
        txt.config(state=tk.DISABLED)
        txt.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        btns = tk.Frame(root)
        btns.pack(fill=tk.X, padx=10, pady=(0, 10))

        def accept() -> None:
            result["ok"] = True
            if first_run:
                consent.record_first_run_ack(self._config)
            root.destroy()

        def decline() -> None:
            result["ok"] = False
            root.destroy()

        tk.Button(btns, text="Decline", width=12, command=decline).pack(side=tk.RIGHT)
        tk.Button(btns, text="Accept & start", width=16, command=accept).pack(
            side=tk.RIGHT, padx=(0, 8)
        )
        root.mainloop()
        return result["ok"]

    # ------------------------------------------------------------- actions
    def _on_capture(self, icon=None, item=None) -> None:
        if not self._session:
            return
        threading.Thread(target=self._capture_worker, daemon=True).start()

    def _capture_worker(self) -> None:
        print("Capture: taking one screenshot…", flush=True)
        try:
            new, is_locked = asyncio.run(self._session.capture())
        except Exception as exc:
            self._info("Capture failed", str(exc))
            return
        if is_locked:
            self._info("Capture", "The remote screen is locked — nothing to read.")
        else:
            self._info("Capture", f"Captured {len(new)} new note(s).")
        self._refresh()

    def _on_ask(self, icon=None, item=None) -> None:
        if not self._session:
            return
        threading.Thread(target=self._ask_dialog_thread, daemon=True).start()

    def _on_view_data(self, icon=None, item=None) -> None:
        threading.Thread(target=self._view_data_thread, daemon=True).start()

    def _on_stop_session(self, icon=None, item=None) -> None:
        sess = self._session
        if not sess:
            return

        def worker() -> None:
            try:
                removed = sess.end()
            except Exception as exc:
                print(f"Stop session: {exc}", flush=True)
                removed = 0
            self._session = None
            if sess.session_only:
                self._info("Session ended", f"Cleared {removed} note(s) from this session.")
            else:
                self._info("Session ended", "Notes retained per your retention setting.")
            self._refresh()

        threading.Thread(target=worker, daemon=True).start()

    def _on_purge_all(self, icon=None, item=None) -> None:
        def worker() -> None:
            if not self._confirm("Purge all stored data",
                                 "Delete ALL notes this tool has ever stored? This cannot be undone."):
                return
            sess = self._session or AssistSession(self._config, self._api_key)
            try:
                removed = sess.purge_all()
            except Exception as exc:
                self._info("Purge failed", str(exc))
                return
            self._info("Purged", f"Deleted {removed} stored note(s).")
            self._refresh()

        threading.Thread(target=worker, daemon=True).start()

    def _on_quit(self, icon, item) -> None:
        if self._session:
            try:
                self._session.end()
            except Exception:
                pass
            self._session = None
        icon.stop()

    # ------------------------------------------------------------- dialogs
    def _ask_dialog_thread(self) -> None:
        import tkinter as tk
        from tkinter import scrolledtext

        sess = self._session
        if not sess:
            return

        root = tk.Tk()
        root.title("Ask horizon-assist")
        root.geometry("540x400")

        frame = tk.Frame(root)
        frame.pack(fill=tk.X, padx=10, pady=(10, 4))
        entry = tk.Entry(frame, font=("Segoe UI", 11))
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
        btn = tk.Button(frame, text="Ask", width=6)
        btn.pack(side=tk.LEFT, padx=(6, 0))
        entry.focus()

        txt = scrolledtext.ScrolledText(root, font=("Segoe UI", 10), wrap=tk.WORD, state=tk.DISABLED)
        txt.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        def _append(text: str) -> None:
            txt.config(state=tk.NORMAL)
            txt.insert(tk.END, text)
            txt.see(tk.END)
            txt.config(state=tk.DISABLED)

        def ask() -> None:
            q = entry.get().strip()
            if not q:
                return
            entry.delete(0, tk.END)
            txt.config(state=tk.NORMAL)
            txt.delete(1.0, tk.END)
            txt.config(state=tk.DISABLED)
            _append(f"Q: {q}\n\nA: ")
            btn.config(state=tk.DISABLED)

            def run() -> None:
                try:
                    sess.ask(q, on_chunk=lambda t: root.after(0, lambda t=t: _append(t)))
                    root.after(0, lambda: _append("\n"))
                except Exception as exc:
                    root.after(0, lambda: _append(f"\n[Error: {exc}]"))
                finally:
                    root.after(0, lambda: btn.config(state=tk.NORMAL))

            threading.Thread(target=run, daemon=True).start()

        btn.config(command=ask)
        entry.bind("<Return>", lambda e: ask())
        root.mainloop()

    def _view_data_thread(self) -> None:
        import tkinter as tk
        from tkinter import scrolledtext

        sess = self._session or AssistSession(self._config, self._api_key)
        try:
            sess.connect()
            notes = sess.list_notes(limit=200)
            total = sess.count()
        except Exception as exc:
            self._info("View stored data", f"Could not read stored data: {exc}")
            return

        root = tk.Tk()
        root.title("horizon-assist — stored data")
        root.geometry("640x460")

        header = tk.Label(
            root,
            text=f"{total} note(s) stored locally. Showing up to 200, newest first.",
            font=("Segoe UI", 10, "bold"),
            anchor="w",
        )
        header.pack(fill=tk.X, padx=10, pady=(10, 4))

        txt = scrolledtext.ScrolledText(root, font=("Consolas", 9), wrap=tk.WORD)
        if notes:
            for n in notes:
                when = n.get("chat_time") or (n.get("observed_at", "")[:16])
                ch = f" «{n.get('channel')}»" if n.get("channel") else ""
                txt.insert(
                    tk.END,
                    f"[{when}] {n.get('app','')}{ch} {n.get('speaker','')}: {n.get('message','')}\n",
                )
        else:
            txt.insert(tk.END, "Nothing stored.")
        txt.config(state=tk.DISABLED)
        txt.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        root.mainloop()

    def _info(self, title: str, message: str) -> None:
        import tkinter as tk
        from tkinter import messagebox

        def show() -> None:
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo(title, message, parent=root)
            root.destroy()

        threading.Thread(target=show, daemon=True).start()

    def _confirm(self, title: str, message: str) -> bool:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        ok = messagebox.askyesno(title, message, parent=root)
        root.destroy()
        return bool(ok)

    # --------------------------------------------------- remote control
    def _prompt_text(self, title: str, label: str) -> str:
        import tkinter as tk
        from tkinter import simpledialog

        root = tk.Tk()
        root.withdraw()
        value = simpledialog.askstring(title, label, parent=root)
        root.destroy()
        return (value or "").strip()

    def _controller(self, client: HorizonMCPClient) -> RemoteController:
        ctl = self._config.get("control", {})
        return RemoteController(
            client,
            focus_target=ctl.get("focus_target", "PVDI"),
            launch_wait=ctl.get("launch_wait_seconds", 1.5),
        )

    def _run_control(self, label, action) -> None:
        if not self._control_enabled:
            print("Remote control disabled — set [control].enabled=true in config.toml", flush=True)
            return

        def worker() -> None:
            async def go() -> None:
                cfg = self._config
                async with HorizonMCPClient(cfg["mcp"]["server_path"], cfg["mcp"]["command"]) as client:
                    await action(self._controller(client))
            print(f"Remote: {label}…", flush=True)
            try:
                asyncio.run(go())
                print(f"Remote: {label} — done", flush=True)
            except Exception as exc:
                print(f"Remote: {label} failed: {exc}", flush=True)

        threading.Thread(target=worker, daemon=True).start()

    def _on_bring_to_front(self, icon=None, item=None) -> None:
        self._run_control("bring Horizon to front", lambda c: c.bring_to_front())

    def _on_launch_app(self, icon=None, item=None) -> None:
        def worker() -> None:
            app = self._prompt_text("Launch / activate app on remote",
                                    "App name (as in the remote Start menu):")
            if app:
                self._run_control(f"launch/activate '{app}'", lambda c: c.launch_or_activate(app))
        threading.Thread(target=worker, daemon=True).start()

    def _on_unlock(self, icon=None, item=None) -> None:
        import os

        def worker() -> None:
            password = os.environ.get("HORIZON_PASSWORD", "")
            if not password:
                self._info("Unlock", "HORIZON_PASSWORD not set in .env — cannot unlock.")
                return
            self._run_control("unlock remote desktop", lambda c: c.unlock(password))
        threading.Thread(target=worker, daemon=True).start()

    # -------------------------------------------------------------- entry
    def run(self) -> None:
        """Show the tray icon in the idle state. Nothing is captured until the user
        starts a session and accepts the consent screen."""
        self._icon = pystray.Icon(
            name="horizon-assist",
            icon=_make_icon("idle"),
            title="horizon-assist — idle",
            menu=pystray.Menu(self._menu_items),
        )
        self._icon.run()
