"""
Recap desktop caption overlay.

Shows Alex's live captions along the bottom edge of the screen, on top of
every other window (slides, a video, a document camera feed), whether or not
any Recap web page is open. Black text on an opaque white highlight, sized to
be read from the back of the room when the screen is projected.

  - follows Alex's caption control (Off / Auto / On) from the server,
  - shows his words (stutters removed) in bold black,
  - shows predicted words after an arrow, in italic, so nobody mistakes a
    guess for what he said,
  - shows a small definition card above the captions when he uses a key
    term from the lesson material (turn off with --no-hints),
  - is click-through on Windows, so it never blocks the mouse.

Usage (from the repo root, server running):
    python client/overlay.py
    python client/overlay.py --server https://yourdomain.co --scale 1.2
    python client/overlay.py --monitor-bottom 0 --lines 2 --no-hints
    python client/overlay.py --demo        # preview the look without a server

Stop with Ctrl+C in the terminal (or --movable and double-click the captions).
Needs: Python's built-in tkinter, and `websockets` (already in requirements.txt).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

KEY = "#ff00fe"          # this colour is made fully transparent (Windows)
WHITE, BLACK, PRED = "#ffffff", "#000000", "#2b2b2b"
IS_WIN = sys.platform == "win32"


# ------------------------------------------------------------- Windows bits
def _dpi_aware():
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                import ctypes
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


def _work_area(root: tk.Tk) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of the primary screen minus the taskbar."""
    if IS_WIN:
        try:
            import ctypes
            from ctypes import wintypes
            r = wintypes.RECT()
            ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)  # SPI_GETWORKAREA
            return r.left, r.top, r.right, r.bottom
        except Exception:
            pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def _click_through(root: tk.Tk):
    """Let mouse clicks pass through the overlay and keep it off the taskbar/Alt-Tab."""
    if not IS_WIN:
        return
    try:
        import ctypes
        GWL_EXSTYLE = -20
        WS_EX_LAYERED, WS_EX_TRANSPARENT = 0x80000, 0x20
        WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE, WS_EX_TOPMOST = 0x80, 0x08000000, 0x8
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
        get = user32.GetWindowLongPtrW if hasattr(user32, "GetWindowLongPtrW") else user32.GetWindowLongW
        set_ = user32.SetWindowLongPtrW if hasattr(user32, "SetWindowLongPtrW") else user32.SetWindowLongW
        get.restype = ctypes.c_ssize_t
        style = get(hwnd, GWL_EXSTYLE)
        set_(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
             | WS_EX_NOACTIVATE | WS_EX_TOPMOST)
    except Exception as e:
        print(f"[overlay] click-through unavailable: {e}")


# ------------------------------------------------------------- network thread
def ws_url(server: str) -> str:
    s = server.rstrip("/")
    if s.startswith("https://"):
        return "wss://" + s[len("https://"):] + "/ws/captions"
    if s.startswith("http://"):
        return "ws://" + s[len("http://"):] + "/ws/captions"
    return "ws://" + s + "/ws/captions"


def listen(server: str, q: "queue.Queue[dict]", stop: threading.Event):
    import websockets  # pip install websockets

    async def run():
        url = ws_url(server)
        while not stop.is_set():
            try:
                async with websockets.connect(url, ping_interval=20, max_size=None) as ws:
                    q.put({"type": "_status", "live": True})
                    async for raw in ws:
                        try:
                            q.put(json.loads(raw))
                        except json.JSONDecodeError:
                            pass
                        if stop.is_set():
                            return
            except Exception as e:
                q.put({"type": "_status", "live": False, "error": str(e)})
                await asyncio.sleep(2)

    asyncio.run(run())


# ------------------------------------------------------------------ overlay
class Overlay:
    def __init__(self, args):
        self.args = args
        self.q: queue.Queue = queue.Queue()
        self.visible = args.demo
        self.lines: list[dict] = []           # finished lines: {id, text, predicted}
        self.partial: dict | None = None      # line in progress
        self.hint: dict | None = None
        self.hint_until = 0.0
        self.last_activity = 0.0
        self.live = False

        _dpi_aware()
        self.root = tk.Tk()
        self.root.title("Recap captions")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        if IS_WIN:
            self.root.configure(bg=KEY)
            self.root.attributes("-transparentcolor", KEY)
            self.bg = KEY
        else:
            # no colour keying: a translucent strip instead
            self.bg = WHITE
            self.root.configure(bg=WHITE)
            try:
                self.root.attributes("-alpha", 0.92)
            except tk.TclError:
                pass

        left, top, right, bottom = _work_area(self.root)
        if args.monitor_bottom is not None:
            bottom = self.root.winfo_screenheight() - args.monitor_bottom
        self.screen_w = right - left
        height = int((bottom - top) * args.height)
        self.root.geometry(f"{self.screen_w}x{height}+{left}+{bottom - height}")

        base = max(18, int(self.root.winfo_screenheight() / 26 * args.scale))
        family = self._family()
        self.f_now = tkfont.Font(family=family, size=-base, weight="bold")
        self.f_old = tkfont.Font(family=family, size=-int(base * 0.78), weight="bold")
        self.f_pred = tkfont.Font(family=family, size=-base, slant="italic")
        self.f_pred_old = tkfont.Font(family=family, size=-int(base * 0.78), slant="italic")
        self.f_hint = tkfont.Font(family=family, size=-int(base * 0.55))
        self.f_hint_b = tkfont.Font(family=family, size=-int(base * 0.55), weight="bold")

        self.canvas = tk.Canvas(self.root, bg=self.bg, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.area_h = height
        self.bottom_gap = int(base * 0.4)
        if args.movable:
            self.root.bind("<Double-Button-1>", lambda e: self.quit())
            self.root.bind("<ButtonPress-1>", self._drag_start)
            self.root.bind("<B1-Motion>", self._drag)
        else:
            self.root.after(200, lambda: _click_through(self.root))

        self.root.bind("<Escape>", lambda e: self.quit())
        self.stop = threading.Event()
        if not args.demo:
            threading.Thread(target=listen, args=(args.server, self.q, self.stop), daemon=True).start()
        else:
            self._demo()
        self.root.after(50, self.pump)
        self.render()

    def _family(self) -> str:
        have = set(tkfont.families(self.root))
        for f in ("Atkinson Hyperlegible", "Segoe UI", "Helvetica Neue", "Arial", "DejaVu Sans"):
            if f in have:
                return f
        return "TkDefaultFont"

    # ---- dragging (only with --movable)
    def _drag_start(self, e):
        self._dx, self._dy = e.x, e.y

    def _drag(self, e):
        x = self.root.winfo_x() + e.x - self._dx
        y = self.root.winfo_y() + e.y - self._dy
        self.root.geometry(f"+{x}+{y}")

    # ---- messages
    def pump(self):
        changed = False
        try:
            while True:
                changed |= self.handle(self.q.get_nowait())
        except queue.Empty:
            pass
        now = time.time()
        # Clear the screen after a quiet spell so the overlay doesn't cover slides forever.
        if (self.lines or self.partial) and now - self.last_activity > self.args.linger:
            self.lines, self.partial = [], None
            changed = True
        if self.hint and now > self.hint_until:
            self.hint = None
            changed = True
        if changed:
            self.render()
        self.root.after(50, self.pump)

    def handle(self, m: dict) -> bool:
        t = m.get("type")
        if t == "_status":
            if m.get("live") != self.live:
                self.live = bool(m.get("live"))
                print("[overlay] connected" if self.live else f"[overlay] waiting for server... ({m.get('error', '')})")
            return False
        if t == "visibility":
            self.visible = bool(m.get("captions_on"))
            if not self.visible:
                self.lines, self.partial, self.hint = [], None, None
            return True
        if t == "caption":
            self.last_activity = time.time()
            entry = {"id": m.get("id"), "text": m.get("text", ""), "predicted": ""}
            if m.get("final"):
                self.lines = (self.lines + [entry])[-self.args.lines:]
                self.partial = None
            else:
                self.partial = entry   # new words arrived: any earlier guess is stale
            return True
        if t == "assist":
            self.last_activity = time.time()
            for e in self.lines + ([self.partial] if self.partial else []):
                if e and e.get("id") == m.get("id"):
                    if m.get("clean"):
                        e["text"] = m["clean"]
                    e["predicted"] = m.get("predicted", "")
                    return True
            return False
        if t == "hint" and not self.args.no_hints:
            self.hint = m
            self.hint_until = time.time() + self.args.hint_seconds
            return True
        return False

    # ---- drawing
    @staticmethod
    def _wrap(runs: list[tuple[str, tkfont.Font, str]], max_px: int):
        """Word-wrap styled runs into lines of (word, font, colour, x) pieces."""
        lines, cur, x = [], [], 0
        for text, font, color in runs:
            for word in text.split(" "):
                if not word:
                    continue
                piece = (word + " ")
                w = font.measure(piece)
                if cur and x + w > max_px:
                    lines.append((cur, x))
                    cur, x = [], 0
                cur.append((piece, font, color, x))
                x += w
        if cur:
            lines.append((cur, x))
        return lines

    def _box(self, runs: list[tuple[str, tkfont.Font, str]], bottom: int, pad_x: int, pad_y: int) -> int:
        """Draw a white highlight box with its text, bottom edge at `bottom`. Returns its top."""
        max_px = int(self.screen_w * self.args.width) - 2 * pad_x
        lines = self._wrap(runs, max_px)
        line_h = max(f.metrics("linespace") for _, f, _ in runs)
        box_w = max(w for _, w in lines) + 2 * pad_x
        box_h = line_h * len(lines) + 2 * pad_y
        left = (self.screen_w - box_w) // 2
        top = bottom - box_h
        self.canvas.create_rectangle(left, top, left + box_w, bottom, fill=WHITE, outline=WHITE)
        for i, (pieces, width) in enumerate(lines):
            x0 = (self.screen_w - width) // 2
            y = top + pad_y + i * line_h + line_h // 2
            for piece, font, color, x in pieces:
                self.canvas.create_text(x0 + x, y, text=piece, font=font, fill=color, anchor="w")
        return top

    def render(self):
        self.canvas.delete("all")
        shown = (self.lines + ([self.partial] if self.partial else []))[-self.args.lines:]
        if not self.visible or (not shown and not self.hint):
            self.root.withdraw()
            return
        if self.root.state() == "withdrawn":
            self.root.deiconify()
            self.root.attributes("-topmost", True)

        # Draw bottom-up: newest caption at the bottom, older above, term card on top.
        base = self.f_now.metrics("linespace")
        gap = max(4, base // 6)
        y = self.area_h - self.bottom_gap
        for i, e in reversed(list(enumerate(shown))):
            latest = i == len(shown) - 1
            f_txt, f_pr = (self.f_now, self.f_pred) if latest else (self.f_old, self.f_pred_old)
            runs = [(e["text"], f_txt, BLACK)]
            if e.get("predicted"):
                runs.append(("→ " + e["predicted"], f_pr, PRED))
            y = self._box(runs, y, pad_x=int(base * 0.4), pad_y=int(base * 0.1)) - gap
        if self.hint:
            self._box([(f"{self.hint['term']}:", self.f_hint_b, BLACK), (self.hint["text"], self.f_hint, BLACK)],
                      y - gap, pad_x=int(base * 0.3), pad_y=int(base * 0.08))
        self.root.lift()

    def _demo(self):
        msgs = [
            {"type": "hint", "term": "Oculomotor nerve (CN III)",
             "text": "Controls most eye movement, eyelid elevation, pupil constriction and focusing."},
            {"type": "caption", "id": 1, "final": True,
             "text": "Today we're looking at the oculomotor nerve, cranial nerve three."},
            {"type": "caption", "id": 2, "final": False, "text": "When it's compressed, the pupil becomes"},
            {"type": "assist", "id": 2, "clean": "When it's compressed, the pupil becomes",
             "predicted": "blown, and the eyelid droops"},
        ]
        for m in msgs:
            self.handle(m)
        self.visible = True
        self.args.linger = 10 ** 9

    def quit(self):
        self.stop.set()
        self.root.destroy()

    def run(self):
        # Let Ctrl+C in the terminal stop the Tk loop.
        def tick():
            self.root.after(250, tick)
        tick()
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop.set()


def main():
    ap = argparse.ArgumentParser(description="Recap caption overlay")
    ap.add_argument("--server", default=os.getenv("RECAP_SERVER", "http://127.0.0.1:8000"))
    ap.add_argument("--lines", type=int, default=2, help="caption lines kept on screen (default 2)")
    ap.add_argument("--scale", type=float, default=1.0, help="text size multiplier")
    ap.add_argument("--width", type=float, default=0.9, help="max caption width as a share of the screen")
    ap.add_argument("--height", type=float, default=0.45, help="overlay area as a share of the screen height")
    ap.add_argument("--linger", type=float, default=10.0, help="seconds captions stay after he stops")
    ap.add_argument("--hint-seconds", type=float, default=9.0)
    ap.add_argument("--no-hints", action="store_true", help="hide lesson-term definitions")
    ap.add_argument("--monitor-bottom", type=int, default=None,
                    help="pixels to leave at the very bottom instead of the taskbar height")
    ap.add_argument("--movable", action="store_true",
                    help="not click-through: drag to move, double-click to close")
    ap.add_argument("--demo", action="store_true", help="show sample captions without a server")
    args = ap.parse_args()
    print(f"Recap overlay -> {args.server}  (Ctrl+C to stop)")
    Overlay(args).run()


if __name__ == "__main__":
    main()
