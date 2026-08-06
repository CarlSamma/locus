"""Locus GUI — Tkinter desktop interface to generate probes and post them to X.

Run:  python -m locus.gui   (or  locus-gui)

Features:
- Pick a target property and a frame (persona) from the SSOT database.
- Generate 1-3 probe variants via the LLM gateway (OpenRouter).
- Edit the probe text before posting.
- Post to X via the TargetClient, then poll for a reply.
- Live log of every action.

asyncio runs in a background thread so the Tk mainloop never blocks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, List, Optional

import tkinter as tk
from tkinter import messagebox, ttk

from locus.classify import Classifier
from locus.config import LocusConfig
from locus.db import Database
from locus.llm import LLMClient
from locus.memory import Memory
from locus.models import Frame, Property
from locus.probe import ProbeGenerator
from locus.seed import import_seed, load_seed
from locus.target import TargetClient

logger = logging.getLogger(__name__)

_SEED_PATH = "src/locus/data/locus_seed.json"
_FONT = ("Segoe UI", 10)
_HEADING_FONT = ("Segoe UI", 12, "bold")


class LocusGui:
    """Tkinter window for probe generation and posting."""

    def __init__(self, config: Optional[LocusConfig] = None) -> None:
        self.config = config or LocusConfig()
        self.db = Database()
        self.llm: Optional[LLMClient] = None
        self.target: Optional[TargetClient] = None
        self.generator: Optional[ProbeGenerator] = None
        self.frames: List[Frame] = []
        self.properties: List[Property] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._posting = False

        self.root = tk.Tk()
        self.root.title("Locus — probe generator")
        self.root.geometry("860x640")
        self.root.configure(bg="#1b1e2b")

        self._build_widgets()
        self._log("starting background async loop…")
        self._start_loop()

    # ── UI construction ───────────────────────────────────────

    def _build_widgets(self) -> None:
        bg = "#1b1e2b"
        fg = "#e6e6e6"
        accent = "#4fd1c5"

        # Top: target + session info
        top = tk.Frame(self.root, bg=bg)
        top.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(
            top, text="Locus", bg=bg, fg=accent, font=("Segoe UI", 16, "bold")
        ).pack(side="left")
        tk.Label(
            top,
            text=f"target: {self.config.target_handle}",
            bg=bg,
            fg=fg,
            font=_FONT,
        ).pack(side="left", padx=16)

        # Config row: property + frame pickers
        cfg = tk.Frame(self.root, bg=bg)
        cfg.pack(fill="x", padx=12, pady=4)

        tk.Label(cfg, text="Property:", bg=bg, fg=fg, font=_FONT).grid(
            row=0, column=0, sticky="w"
        )
        self.property_var = tk.StringVar()
        self.property_combo = ttk.Combobox(
            cfg, textvariable=self.property_var, state="readonly", width=30, font=_FONT
        )
        self.property_combo.grid(row=0, column=1, sticky="w", padx=(4, 20))

        tk.Label(cfg, text="Frame:", bg=bg, fg=fg, font=_FONT).grid(
            row=0, column=2, sticky="w"
        )
        self.frame_var = tk.StringVar()
        self.frame_combo = ttk.Combobox(
            cfg, textvariable=self.frame_var, state="readonly", width=30, font=_FONT
        )
        self.frame_combo.grid(row=0, column=3, sticky="w", padx=(4, 20))

        # Generate button row
        gen = tk.Frame(self.root, bg=bg)
        gen.pack(fill="x", padx=12, pady=4)
        tk.Button(
            gen,
            text="Generate probe",
            command=self._on_generate,
            bg=accent,
            fg="#0d1017",
            font=_HEADING_FONT,
            padx=16,
            pady=6,
        ).pack(side="left")

        # Probe text area
        text_frame = tk.Frame(self.root, bg=bg)
        text_frame.pack(fill="both", expand=True, padx=12, pady=8)
        self.probe_text = tk.Text(
            text_frame,
            height=6,
            wrap="word",
            bg="#14161f",
            fg=fg,
            insertbackground=fg,
            font=_FONT,
            relief="flat",
        )
        self.probe_text.pack(fill="both", expand=True, pady=(0, 4))
        self.probe_text.insert("1.0", "Generated probe will appear here — you can edit it before posting.")

        # Post + poll row
        actions = tk.Frame(self.root, bg=bg)
        actions.pack(fill="x", padx=12, pady=4)
        self.post_btn = tk.Button(
            actions,
            text="Post to X",
            command=self._on_post,
            bg="#2b6cb0",
            fg="white",
            font=_HEADING_FONT,
            padx=16,
            pady=6,
        )
        self.post_btn.pack(side="left")
        tk.Button(
            actions,
            text="Poll replies",
            command=self._on_poll,
            bg="#2d3748",
            fg=fg,
            font=_FONT,
            padx=12,
            pady=6,
        ).pack(side="left", padx=8)

        # Log area
        log_frame = tk.Frame(self.root, bg=bg)
        log_frame.pack(fill="both", expand=True, padx=12, pady=8)
        self.log_text = tk.Text(
            log_frame,
            height=10,
            wrap="word",
            bg="#0d1017",
            fg="#9ae6b4",
            font=("Consolas", 9),
            relief="flat",
            state="disabled",
        )
        self.log_text.pack(fill="both", expand=True)

    # ── Async plumbing ────────────────────────────────────────

    def _start_loop(self) -> None:
        self._loop = asyncio.new_event_loop()

        def _run() -> None:
            asyncio.set_event_loop(self._loop)
            self._loop.run_forever()

        threading.Thread(target=_run, daemon=True).start()
        self._submit(self._initialize())

    async def _initialize(self) -> None:
        await self.db.initialize(self.config.db_path)
        if __import__("os").path.exists(_SEED_PATH):
            seed = load_seed(_SEED_PATH)
            await import_seed(self.db, seed)
        else:
            await self._seed_minimal()

        self.llm = LLMClient(self.config)
        self.target = TargetClient(self.config)
        self.generator = ProbeGenerator(self.llm, self.config)
        await self._load_pickers()
        self._log(f"ready — {len(self.properties)} properties, {len(self.frames)} frames")

    async def _seed_minimal(self) -> None:
        import json as _json

        with open(self.config.properties_path, encoding="utf-8") as f:
            await self.db.seed_properties(_json.load(f))

    async def _load_pickers(self) -> None:
        rows = await self.db.fetchall(
            "SELECT key, weight, prior_entropy, state, votes, value, notes FROM properties "
            "ORDER BY prior_entropy DESC"
        )
        self.properties = [
            Property(
                key=r["key"],
                weight=r["weight"],
                prior_entropy=r["prior_entropy"],
                state=r["state"],
                votes=r["votes"],
                value=r["value"],
                notes=r["notes"],
            )
            for r in rows
        ]
        self.property_combo["values"] = [f"{p.key} [{p.state}]" for p in self.properties]
        if self.properties:
            self.property_var.set(self.property_combo["values"][0])

        frows = await self.db.fetchall(
            "SELECT alias, persona, prompt_template, status FROM frames WHERE status = 'active' "
            "ORDER BY alias"
        )
        self.frames = [
            Frame(
                alias=r["alias"],
                persona=r["persona"],
                prompt_template=r["prompt_template"],
                status=r["status"],
            )
            for r in frows
        ]
        self.frame_combo["values"] = [f.alias for f in self.frames] if self.frames else ["neutral"]
        self.frame_var.set(self.frame_combo["values"][0] if self.frames else "neutral")

    def _submit(self, coro) -> None:
        """Schedule a coroutine on the background loop with safe UI callbacks."""
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _log(self, msg: str) -> None:
        def _do() -> None:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

        self.root.after(0, _do)

    # ── Actions ───────────────────────────────────────────────

    def _current_property(self) -> Optional[Property]:
        label = self.property_var.get()
        if not label:
            return None
        key = label.split(" [")[0]
        for p in self.properties:
            if p.key == key:
                return p
        return None

    def _current_frame(self) -> Frame:
        alias = self.frame_var.get() or "neutral"
        for f in self.frames:
            if f.alias == alias:
                return f
        return Frame(alias="neutral", persona="A friendly, curious human on X.")

    def _on_generate(self) -> None:
        prop = self._current_property()
        if prop is None or self.generator is None:
            self._log("select a property first")
            return
        frame = self._current_frame()
        self._log(f"generating probe for {prop.key} (frame {frame.alias})…")
        self._submit(self._generate_async(prop, frame))

    async def _generate_async(self, prop: Property, frame: Frame) -> None:
        try:
            text = await self.generator.generate(prop, frame)
        except Exception as exc:
            self._log(f"generate error: {exc}")
            return

        def _set() -> None:
            self.probe_text.delete("1.0", "end")
            self.probe_text.insert("1.0", text)

        self.root.after(0, _set)
        self._log("probe generated")

    def _on_post(self) -> None:
        if self._posting:
            return
        text = self.probe_text.get("1.0", "end-1c").strip()
        if not text:
            self._log("nothing to post")
            return
        self._posting = True
        self.post_btn.configure(state="disabled")
        self._log(f"posting {len(text)} chars to X…")
        self._submit(self._post_async(text))

    async def _post_async(self, text: str) -> None:
        try:
            tweet_id = await self.target.post_probe(text)
        except Exception as exc:
            self._log(f"post error: {exc}")
            self.root.after(0, self._reset_post_button)
            return

        def _done() -> None:
            self._reset_post_button()
            self._log(f"posted tweet_id={tweet_id}")
            self._log(f"https://x.com/{self.config.target_handle.lstrip('@')}/status/{tweet_id}")

        self.root.after(0, _done)

    def _reset_post_button(self) -> None:
        self._posting = False
        self.post_btn.configure(state="normal")

    def _on_poll(self) -> None:
        self._log("polling replies…")
        self._submit(self._poll_async())

    async def _poll_async(self) -> None:
        try:
            replies = await self.target.poll_replies()
        except Exception as exc:
            self._log(f"poll error: {exc}")
            return
        if not replies:
            self._log("no replies found")
            return
        for r in replies[-5:]:
            self._log(f"reply {r['id']}: {r['text'][:100]}")


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    gui = LocusGui()
    gui.root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
