"""
Remote-control layer for the Horizon session — the *write* path.

Monitoring is read-only (screenshot/list_windows/focus_window). This module composes
horizon-mcp's input primitives into meaningful, user-triggered actions: unlock the
remote desktop, launch or un-minimise an app inside it, send a reply, scroll history.

WHY focus first, every time: horizon-mcp's click/type/key_combo go to whatever window
currently holds OS focus. Input only reaches the *remote* desktop when the local Horizon
client window is in the foreground, so every action calls ensure_foreground() first.

INSIDE vs OUTSIDE the remote: focus_window/list_windows only see *local* OS windows (the
Horizon client is one of them). Apps running *inside* the remote desktop — Teams, Symphony
— are just pixels in that client and are NOT separate OS windows, so focus_window cannot
reach them. To launch or un-minimise them we drive the remote's *own* Start menu via
key_combo(["Win"]) → type name → Enter (the Windows key needs real virtual-key codes,
which is exactly what key_combo provides and type_text/press_key cannot).

WHY this is opt-in ([control].enabled): these actions type into and click on a corporate
remote desktop and steal local focus. They must be user-triggered, never automatic.
"""

from __future__ import annotations

import asyncio
import json

from .mcp_client import HorizonMCPClient


class RemoteController:
    def __init__(
        self,
        client: HorizonMCPClient,
        focus_target: str = "PVDI",
        launch_wait: float = 1.5,
        clipboard_sync: float = 0.6,
        copy_timeout: float = 6.0,
    ) -> None:
        self._client = client
        self._focus_target = focus_target
        self._launch_wait = launch_wait
        # How long redirection takes to sync the clipboard between local and remote,
        # and how long to wait for a copied file to arrive before giving up.
        self._clipboard_sync = clipboard_sync
        self._copy_timeout = copy_timeout

    async def ensure_foreground(self) -> None:
        """Bring the local Horizon client to the foreground so input reaches the remote.

        horizon-mcp injects input into the LOCAL OS; it only reaches the remote
        desktop while the Horizon client window holds local foreground. Windows'
        foreground-lock rules mean focus_window does not always succeed (a local
        app can keep or re-steal the front), so we VERIFY the client is frontmost
        and refuse to proceed otherwise — sending keystrokes/clipboard ops blindly
        would leak them into whatever local window is focused (e.g. a local editor).
        """
        target = self._focus_target.lower()
        for _ in range(3):
            await self._client.focus_window(self._focus_target)
            await asyncio.sleep(0.4)
            try:
                fg = await self._client.get_foreground_window()
            except Exception:
                return  # older horizon-mcp without the tool — fall back to old behaviour
            if target in str(fg.get("Title", "")).lower():
                return
        fg_title = str(fg.get("Title", "")) or "<unknown>"
        raise RuntimeError(
            f"Horizon client '{self._focus_target}' is not the foreground window "
            f"(foreground is {fg_title!r}). Aborting so input is not sent to the "
            f"local machine — click the Horizon window to give it focus and retry."
        )

    # alias used by the tray / CLI when the user just wants the window up front
    async def bring_to_front(self) -> None:
        await self.ensure_foreground()

    async def unlock(self, password: str) -> None:
        """Send Ctrl+Alt+Del to the remote, then enter the password.

        Uses paste_text (more reliable than typing in a remote password field) and
        restores the prior clipboard afterward so the secret does not linger there.
        """
        await self.ensure_foreground()
        await self._client.key_combo(["Ctrl", "Alt", "Insert"])  # Horizon's Ctrl+Alt+Del
        await asyncio.sleep(2.5)                                 # wait for the login prompt
        if password:
            prior = ""
            try:
                prior = await self._client.get_clipboard()
            except Exception:
                pass
            try:
                await self._client.paste_text(password)
                await asyncio.sleep(0.3)
                await self._client.key_combo(["Enter"])
            finally:
                # Clear/restore the clipboard so the password does not persist.
                try:
                    await self._client.set_clipboard(prior or "")
                except Exception:
                    pass

    async def open_start(self) -> None:
        await self.ensure_foreground()
        await self._client.key_combo(["Win"])
        await asyncio.sleep(0.8)

    async def open_run(self) -> None:
        await self.ensure_foreground()
        await self._client.key_combo(["Win", "R"])
        await asyncio.sleep(0.8)

    async def launch_or_activate(self, app: str) -> None:
        """Open the remote Start menu, search for `app`, and launch (or focus) it.

        For single-instance chat apps (Teams, Symphony) this brings an already-running,
        minimised window to the foreground; for others it may start a new instance.
        """
        await self.open_start()
        await self._client.type_text(app)
        await asyncio.sleep(self._launch_wait)
        await self._client.key_combo(["Enter"])
        await asyncio.sleep(self._launch_wait)

    async def run_command(self, command: str) -> None:
        """Open the Run dialog and execute a command (e.g. an exe path or 'teams')."""
        await self.open_run()
        await self._client.type_text(command)
        await asyncio.sleep(0.4)
        await self._client.key_combo(["Enter"])
        await asyncio.sleep(self._launch_wait)

    async def send_reply(self, text: str, submit: bool = True) -> None:
        """Type `text` into whatever input currently has focus in the remote, then
        optionally press Enter. The caller is responsible for the chat input being
        focused (e.g. the user clicked it, or a prior click_at)."""
        await self.ensure_foreground()
        await self._client.type_text(text)
        if submit:
            await asyncio.sleep(0.2)
            await self._client.key_combo(["Enter"])

    async def click_at(self, x: int, y: int, button: str = "left") -> None:
        await self.ensure_foreground()
        await self._client.click(x, y, button)

    async def scroll_history(
        self, x: int, y: int, direction: str = "up", amount: int = 3
    ) -> None:
        """Scroll the chat pane at (x, y) to reveal earlier/later messages."""
        await self.ensure_foreground()
        await self._client.scroll(x, y, direction, amount)

    # ------------------------------------------------------- code-editing bridge
    # Pull a file's text out of the remote editor, edit it locally (with AI that
    # isn't available inside the VDI), and push the whole document back.
    #
    # The transport is the clipboard, NOT OCR. OCR loses indentation and only sees
    # the visible region; type_text corrupts code via the editor's autocomplete /
    # auto-indent. A Ctrl+A/Ctrl+C in the remote editor copies the *entire* file
    # exactly; Horizon clipboard redirection syncs it to the local clipboard, where
    # get_clipboard() reads it losslessly. The write path is the mirror image.
    #
    # PREREQUISITE: Horizon clipboard redirection must be enabled (it usually is).
    # copy_from_remote() detects when it is not and raises a clear error.
    # PREREQUISITE: the editor pane (not a sidebar/terminal) must hold focus inside
    # the remote so Ctrl+A selects the document — screenshot first if unsure.

    # Null bytes can't appear in a text file, so this never collides with content.
    _COPY_SENTINEL = "\x00__horizon_pull__\x00"

    async def copy_from_remote(self) -> str:
        """Select-all + copy in the focused remote editor; return the full text.

        Seeds the local clipboard with a sentinel, triggers the remote copy, then
        polls until clipboard redirection delivers the file. Raises RuntimeError if
        the clipboard never changes (redirection disabled, or nothing was focused).
        """
        await self.ensure_foreground()
        await self._client.set_clipboard(self._COPY_SENTINEL)
        await asyncio.sleep(self._clipboard_sync)
        await self._client.key_combo(["Ctrl", "A"])
        await asyncio.sleep(0.2)
        await self._client.key_combo(["Ctrl", "C"])

        waited = 0.0
        poll = 0.3
        while waited < self._copy_timeout:
            await asyncio.sleep(poll)
            waited += poll
            text = await self._client.get_clipboard()
            if text and text != self._COPY_SENTINEL:
                return text
        raise RuntimeError(
            "Clipboard did not update after Ctrl+C — Horizon clipboard redirection "
            "may be disabled, or no editor pane was focused in the remote session.\n"
            "If this VDI blocks clipboard copy-out, read with OCR instead "
            "(remote read-file --ocr)."
        )

    async def read_remote_ocr(
        self,
        *,
        region: tuple[int, int, int, int] | None = None,
        scroll_at: tuple[int, int] | None = None,
        max_pages: int = 1,
        scroll_amount: int = 10,
    ) -> str:
        """Read on-screen text from the remote with local Windows OCR — no clipboard.

        Locked-down VDIs often block clipboard copy-OUT, so the bridge cannot pull a
        file with Ctrl+C. This reads it the way a person would by hand instead:
        screenshot the remote and OCR it locally (free, offline — nothing leaves the
        machine). For content taller than the viewport, pass max_pages > 1 and
        scroll_at=(x, y) over the scrollable pane; each screen is OCR'd and stitched
        onto the previous one with the overlapping lines removed, stopping early once
        a page adds nothing new (bottom reached). region=(x, y, w, h) restricts OCR to
        the editor pane so surrounding UI chrome is not captured — on a multi-monitor
        setup pass the region where the Horizon client actually is.

        NOTE: OCR is not byte-exact for code (indentation/glyphs can drift). It is for
        reading, not a lossless pull; pair it with the paste-back write path, which IS
        exact, rather than trusting the OCR text verbatim.
        """
        await self.ensure_foreground()
        x, y, w, h = region or (None, None, None, None)
        acc: list[str] = []
        for page in range(max(1, max_pages)):
            raw = await self._client.ocr(x=x, y=y, width=w, height=h)
            before = len(acc)
            acc = self._stitch(acc, self._ocr_lines(raw))
            grew = len(acc) > before
            if page + 1 >= max_pages or scroll_at is None:
                break
            if page > 0 and not grew:
                break  # nothing new since the last page — reached the bottom
            await self._client.scroll(scroll_at[0], scroll_at[1], "down", scroll_amount)
            await asyncio.sleep(0.5)  # let the pane settle before the next capture
        return "\n".join(acc)

    @staticmethod
    def _ocr_lines(raw: str) -> list[str]:
        """Pull the ordered text lines out of horizon-mcp's ocr JSON ({text, lines})."""
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return (raw or "").splitlines()
        if isinstance(data, dict):
            lines = data.get("lines")
            if isinstance(lines, list):
                return [str(ln) for ln in lines]
            return str(data.get("text", "")).splitlines()
        return str(data).splitlines()

    @staticmethod
    def _stitch(acc: list[str], new: list[str]) -> list[str]:
        """Append `new` to `acc`, dropping the longest run of lines they share at the
        seam (the scroll overlap). Exact-match only — OCR noise between captures can
        still leave a little duplication, so prefer a scroll_amount near a full page."""
        if not acc:
            return list(new)
        if not new:
            return acc
        for k in range(min(len(acc), len(new)), 0, -1):
            if acc[-k:] == new[:k]:
                return acc + new[k:]
        return acc + new

    async def paste_to_remote(
        self, text: str, *, replace_all: bool = True, save: bool = False
    ) -> None:
        """Replace the focused remote editor's contents with `text`.

        Stages `text` on the local clipboard (redirection syncs it to the remote),
        selects all + pastes, optionally saves, then restores the prior clipboard.
        """
        await self.ensure_foreground()
        prior = ""
        try:
            prior = await self._client.get_clipboard()
        except Exception:
            pass
        try:
            await self._client.set_clipboard(text)
            await asyncio.sleep(self._clipboard_sync)  # let redirection reach the remote
            if replace_all:
                await self._client.key_combo(["Ctrl", "A"])
                await asyncio.sleep(0.15)
            await self._client.key_combo(["Ctrl", "V"])
            await asyncio.sleep(self._clipboard_sync)   # let the paste land
            if save:
                await self._client.key_combo(["Ctrl", "S"])
                await asyncio.sleep(0.3)
        finally:
            try:
                await self._client.set_clipboard(prior or "")
            except Exception:
                pass

    async def open_file(self, path: str) -> None:
        """Open `path` in the remote VS Code via the Quick Open palette (Ctrl+P)."""
        await self.ensure_foreground()
        await self._client.key_combo(["Ctrl", "P"])
        await asyncio.sleep(0.4)
        await self._client.type_text(path)
        await asyncio.sleep(0.5)
        await self._client.key_combo(["Enter"])
        await asyncio.sleep(0.4)
