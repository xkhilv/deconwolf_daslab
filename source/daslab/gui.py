"""gui interface built with tkinter"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import core

COLS = ("status", "na", "ni", "xy", "z", "zn", "lam", "notes")
HEAD = {
    "status": ("Status", 110),
    "na": ("NA", 55),
    "ni": ("Immersion n", 95),
    "xy": ("XY pixel (nm)", 105),
    "z": ("Z step (nm)", 95),
    "zn": ("Z planes", 75),
    "lam": ("Emission λ (nm)", 120),
    "notes": ("Notes", 420),
}
FILE_EDIT = {"na": "na", "ni": "ni", "xy": "dxy_nm", "z": "dz_nm"}
FILE_LABEL = {
    "na": "NA",
    "ni": "immersion refractive index",
    "xy": "XY pixel size",
    "z": "Z step",
}


def _fmt(v) -> str:
    return "" if v is None else f"{v:g}"


def settings_path() -> Path:
    return core.app_data_dir() / "settings.json"


def open_path(p: Path) -> None:
    if sys.platform.startswith("win"):
        os.startfile(str(p))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)])


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{core.APP_NAME} {core.APP_VERSION}")
        self.geometry("1250x820")
        self.minsize(900, 600)
        self.jobs: dict[str, core.FileJob] = {}
        self.msgs: queue.Queue = queue.Queue()
        self.cancel_ev = threading.Event()
        self.worker: threading.Thread | None = None
        self.dw_info: dict = {}
        try:
            self.table = core.load_wavelength_table()
        except ValueError as e:
            messagebox.showerror(
                core.APP_NAME, f"{e}\n\nUsing built-in defaults."
            )
            self.table = {
                k: float(v)
                for k, v in core.DEFAULT_WAVELENGTH_TABLE["channels"].items()
            }

        self._build()
        self._load_settings()
        self._detect_dw(initial=True)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._pump)

    # layout
    def _build(self):
        pad = {"padx": 4, "pady": 3}
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=6, pady=(6, 0))
        for text, cmd in (
            ("Add ND2 files…", self._add_files),
            ("Add folder…", self._add_folder),
            ("Remove selected", self._remove),
            ("Clear list", self._clear),
        ):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", **pad)
        ttk.Button(
            bar, text="Reload wavelength table", command=self._reload_table
        ).pack(side="right", **pad)
        ttk.Button(
            bar, text="Edit wavelength table…", command=self._edit_table
        ).pack(side="right", **pad)

        mid = ttk.Frame(self)
        mid.pack(fill="both", expand=True, padx=6, pady=4)
        self.tree = ttk.Treeview(mid, columns=COLS, selectmode="extended")
        self.tree.heading("#0", text="File / channel")
        self.tree.column("#0", width=330, stretch=False)
        for c in COLS:
            self.tree.heading(c, text=HEAD[c][0])
            self.tree.column(
                c,
                width=HEAD[c][1],
                stretch=(c == "notes"),
                anchor="w" if c in ("status", "notes") else "center",
            )
        ys = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        xs = ttk.Scrollbar(mid, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        mid.rowconfigure(0, weight=1)
        mid.columnconfigure(0, weight=1)
        self.tree.tag_configure("bad", foreground="#c00000")
        self.tree.tag_configure("warn", foreground="#b35c00")
        self.tree.tag_configure("done", foreground="#1a7f37")
        self.tree.tag_configure("user", foreground="#0b5cad")
        self.tree.bind("<Double-1>", self._begin_edit)
        ttk.Label(
            self,
            foreground="#555",
            text="Double-click NA, Immersion n, XY, Z step or a channel's λ to edit it.  "
            "Red = must fix before running.  Orange = please check.  Blue = edited by you.",
        ).pack(anchor="w", padx=10)

        st = ttk.LabelFrame(self, text="Settings")
        st.pack(fill="x", padx=6, pady=4)
        self.v_dw = tk.StringVar()
        self.v_out = tk.StringVar()
        self.v_iter = tk.IntVar(value=40)
        self.v_gpu = tk.BooleanVar(value=False)
        self.v_tile = tk.StringVar()
        self.v_threads = tk.StringVar()
        self.v_over = tk.BooleanVar(value=False)
        self.v_keep = tk.BooleanVar(value=False)

        ttk.Label(st, text="Deconwolf folder").grid(
            row=0, column=0, sticky="w", **pad
        )
        ttk.Entry(st, textvariable=self.v_dw).grid(
            row=0, column=1, columnspan=5, sticky="ew", **pad
        )
        ttk.Button(st, text="Browse…", command=self._browse_dw).grid(
            row=0, column=6, **pad
        )
        ttk.Button(st, text="Detect", command=self._detect_dw).grid(
            row=0, column=7, **pad
        )
        self.l_dw = ttk.Label(st, text="", foreground="#555")
        self.l_dw.grid(row=1, column=1, columnspan=7, sticky="w", padx=4)

        ttk.Label(st, text="Output folder").grid(
            row=2, column=0, sticky="w", **pad
        )
        ttk.Entry(st, textvariable=self.v_out).grid(
            row=2, column=1, columnspan=5, sticky="ew", **pad
        )
        ttk.Button(st, text="Browse…", command=self._browse_out).grid(
            row=2, column=6, **pad
        )
        ttk.Label(
            st,
            text="(empty = a 'deconvolved' folder next to each ND2 file)",
            foreground="#555",
        ).grid(row=3, column=1, columnspan=7, sticky="w", padx=4)

        ttk.Label(st, text="Iterations").grid(
            row=4, column=0, sticky="w", **pad
        )
        ttk.Spinbox(
            st, from_=1, to=500, width=6, textvariable=self.v_iter
        ).grid(row=4, column=1, sticky="w", **pad)
        self.c_gpu = ttk.Checkbutton(st, text="Use GPU", variable=self.v_gpu)
        self.c_gpu.grid(row=4, column=2, sticky="w", **pad)
        ttk.Label(st, text="Tile size (blank = off)").grid(
            row=4, column=3, sticky="e", **pad
        )
        ttk.Entry(st, textvariable=self.v_tile, width=7).grid(
            row=4, column=4, sticky="w", **pad
        )
        ttk.Label(st, text="CPU threads (blank = auto)").grid(
            row=4, column=5, sticky="e", **pad
        )
        ttk.Entry(st, textvariable=self.v_threads, width=5).grid(
            row=4, column=6, sticky="w", **pad
        )
        ttk.Checkbutton(
            st, text="Overwrite existing outputs", variable=self.v_over
        ).grid(row=5, column=1, columnspan=2, sticky="w", **pad)
        ttk.Checkbutton(
            st,
            text="Keep intermediate per-channel TIFFs",
            variable=self.v_keep,
        ).grid(row=5, column=3, columnspan=3, sticky="w", **pad)
        st.columnconfigure(5, weight=1)

        run = ttk.Frame(self)
        run.pack(fill="x", padx=6, pady=2)
        self.b_start = ttk.Button(run, text="▶  Start", command=self._start)
        self.b_start.pack(side="left", padx=4)
        self.b_cancel = ttk.Button(
            run, text="Cancel", command=self._cancel, state="disabled"
        )
        self.b_cancel.pack(side="left", padx=4)
        ttk.Button(
            run, text="Open output folder", command=self._open_out
        ).pack(side="left", padx=4)
        self.pb_file = ttk.Progressbar(run, length=180, maximum=1.0)
        self.pb_file.pack(side="right", padx=4)
        ttk.Label(run, text="Current file").pack(side="right")
        self.pb_all = ttk.Progressbar(run, length=180, maximum=1.0)
        self.pb_all.pack(side="right", padx=(4, 14))
        ttk.Label(run, text="Overall").pack(side="right")
        self.l_status = ttk.Label(self, text="Add ND2 files to begin.")
        self.l_status.pack(anchor="w", padx=10)

        self.log = ScrolledText(
            self,
            height=11,
            font=("Consolas", 9),
            state="disabled",
            wrap="none",
        )
        self.log.pack(fill="both", expand=False, padx=6, pady=(2, 6))

    # settings
    def _load_settings(self):
        try:
            d = json.loads(settings_path().read_text(encoding="utf-8"))
        except Exception:
            return
        self.v_dw.set(d.get("dw_dir", ""))
        self.v_out.set(d.get("out_dir", ""))
        self.v_iter.set(int(d.get("iterations", 40)))
        self.v_gpu.set(bool(d.get("gpu", False)))
        self.v_tile.set(str(d.get("tilesize", "") or ""))
        self.v_threads.set(str(d.get("threads", "") or ""))
        self.v_over.set(bool(d.get("overwrite", False)))
        self.v_keep.set(bool(d.get("keep", False)))

    def _save_settings(self):
        d = {
            "dw_dir": self.v_dw.get(),
            "out_dir": self.v_out.get(),
            "iterations": self._int(self.v_iter.get(), 40),
            "gpu": self.v_gpu.get(),
            "tilesize": self.v_tile.get(),
            "threads": self.v_threads.get(),
            "overwrite": self.v_over.get(),
            "keep": self.v_keep.get(),
        }
        try:
            settings_path().write_text(
                json.dumps(d, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    @staticmethod
    def _int(v, default=0) -> int:
        try:
            return int(str(v).strip())
        except ValueError:
            return default

    # deconwolf
    def _detect_dw(self, initial=False):
        d = core.find_deconwolf_dir(self.v_dw.get().strip())
        if d is None:
            self.dw_info = {}
            self.l_dw.configure(
                text="Deconwolf not found. Install it, or Browse to the folder containing "
                "dw.exe and dw_bw.exe.",
                foreground="#c00000",
            )
            self.c_gpu.state(["disabled"])
            if not initial:
                messagebox.showwarning(
                    core.APP_NAME, "Could not find dw.exe and dw_bw.exe."
                )
            return
        self.v_dw.set(str(d))
        self.dw_info = core.deconwolf_info(d)
        if not self.dw_info.get("ok"):
            self.l_dw.configure(
                text=f"Found {d} but 'dw --version' failed: {self.dw_info.get('text')}. "
                "Is the Microsoft Visual C++ Redistributable installed?",
                foreground="#c00000",
            )
            return
        gpu = self.dw_info.get("gpu")
        self.l_dw.configure(
            foreground="#1a7f37",
            text=f"deconwolf {self.dw_info.get('version', '?')} found. GPU support: "
            f"{'yes' if gpu else 'no (CPU only)'}",
        )
        if gpu:
            self.c_gpu.state(["!disabled"])
        else:
            self.v_gpu.set(False)
            self.c_gpu.state(["disabled"])

    def _browse_dw(self):
        d = filedialog.askdirectory(
            title="Folder containing dw.exe and dw_bw.exe"
        )
        if d:
            self.v_dw.set(d)
            self._detect_dw()

    def _browse_out(self):
        d = filedialog.askdirectory(title="Output folder")
        if d:
            self.v_out.set(d)

    # file list
    def _add_files(self):
        paths = filedialog.askopenfilenames(
            title="Choose ND2 files",
            filetypes=[("Nikon ND2", "*.nd2"), ("All files", "*.*")],
        )
        self._add_paths(list(paths))

    def _add_folder(self):
        d = filedialog.askdirectory(
            title="Folder with ND2 files (subfolders included)"
        )
        if d:
            paths = sorted(
                str(p)
                for p in Path(d).rglob("*")
                if p.suffix.lower() == ".nd2"
            )
            if not paths:
                messagebox.showinfo(
                    core.APP_NAME, "No .nd2 files found in that folder."
                )
            self._add_paths(paths)

    def _add_paths(self, paths: list[str]):
        known = {os.path.normcase(j.path) for j in self.jobs.values()}
        new = [p for p in paths if os.path.normcase(p) not in known]
        if not new:
            return
        self._set_status(f"Reading metadata from {len(new)} file(s)…")
        table = dict(self.table)

        def work():
            for p in new:
                job = core.read_nd2_metadata(p, table)
                self.msgs.put(("added", job))
            self.msgs.put(
                ("status", "Check the parameters, then press Start.")
            )

        threading.Thread(target=work, daemon=True).start()

    def _remove(self):
        if self._running():
            return
        for iid in self.tree.selection():
            fid = iid.split(":")[0]
            if fid in self.jobs and self.tree.exists(fid):
                self.tree.delete(fid)
                del self.jobs[fid]

    def _clear(self):
        if self._running():
            return
        self.tree.delete(*self.tree.get_children())
        self.jobs.clear()

    def _insert_job(self, job: core.FileJob):
        fid = f"f{len(self.jobs) + 1}_{id(job)}"
        self.jobs[fid] = job
        self.tree.insert("", "end", iid=fid, text=job.name, open=True)
        for c in job.channels:
            self.tree.insert(fid, "end", iid=f"{fid}:c{c.index}")
        self._refresh(fid)
        self._log(self._describe(job))

    def _describe(self, job: core.FileJob) -> str:
        if job.error:
            return f"{job.name}: {job.error}"
        ch = ", ".join(
            f"{c.name} (file λ={_fmt(c.meta_emission_nm) or '?'} → {_fmt(c.emission_nm) or '?'})"
            for c in job.channels
        )
        return (
            f"{job.name}: {job.size_x}x{job.size_y}x{job.size_z}, {job.objective or 'objective ?'}, "
            f"NA={_fmt(job.na) or '?'}, n={_fmt(job.ni) or '?'}, XY={_fmt(job.dxy_nm) or '?'} nm, "
            f"Z={_fmt(job.dz_nm) or '?'} nm [{job.dz_source or 'not found'}]; channels: {ch}"
        )

    def _refresh(self, fid: str):
        job = self.jobs[fid]
        probs = job.problems()
        notes = job.notes()
        if not job.error and job.status in ("Ready", "Needs input"):
            job.status = "Needs input" if probs else "Ready"
        text = (
            "; ".join(probs + notes)
            if (probs or notes)
            else (job.objective or "")
        )
        tags = ()
        if job.status == "Done":
            tags = ("done",)
            text = job.output or text
        elif probs or job.status in ("Error", "Failed"):
            tags = ("bad",)
        elif notes or any(c.warning for c in job.channels):
            tags = ("warn",)
        self.tree.item(
            fid,
            values=(
                job.status,
                _fmt(job.na),
                _fmt(job.ni),
                _fmt(job.dxy_nm),
                _fmt(job.dz_nm),
                job.size_z or "",
                "",
                text,
            ),
            tags=tags,
        )
        for c in job.channels:
            ctags = (
                ("user",)
                if c.source == "user"
                else (
                    ("bad",)
                    if not c.emission_nm
                    else (("warn",) if c.warning else ())
                )
            )
            r, g, b = c.color_rgb
            self.tree.item(
                f"{fid}:c{c.index}",
                text=f"    C{c.index + 1}  {c.name}",
                values=(
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    _fmt(c.emission_nm),
                    c.warning
                    or f"colour #{r:02x}{g:02x}{b:02x}, from {c.source or '?'}",
                ),
                tags=ctags,
            )

    # editing
    def _begin_edit(self, ev):
        if self._running():
            return
        iid = self.tree.identify_row(ev.y)
        col_id = self.tree.identify_column(ev.x)
        if not iid or col_id == "#0":
            return
        col = COLS[int(col_id[1:]) - 1]
        is_chan = ":" in iid
        if (is_chan and col != "lam") or (
            not is_chan and col not in FILE_EDIT
        ):
            return
        fid = iid.split(":")[0]
        if self.jobs[fid].error:
            return
        bbox = self.tree.bbox(iid, col_id)
        if not bbox:
            return
        x, y, w, h = bbox
        e = ttk.Entry(self.tree, justify="center")
        e.insert(0, self.tree.set(iid, col))
        e.select_range(0, "end")
        e.place(x=x, y=y, width=max(w, 70), height=h)
        e.focus_set()
        done = {"v": False}

        def commit(_=None):
            if done["v"]:
                return
            done["v"] = True
            txt = e.get().strip()
            e.destroy()
            self._apply_edit(iid, col, txt)

        def cancel(_=None):
            done["v"] = True
            e.destroy()

        e.bind("<Return>", commit)
        e.bind("<KP_Enter>", commit)
        e.bind("<FocusOut>", commit)
        e.bind("<Escape>", cancel)

    def _apply_edit(self, iid: str, col: str, txt: str):
        try:
            val = float(txt)
            if val <= 0:
                raise ValueError
        except ValueError:
            if txt:
                messagebox.showerror(
                    core.APP_NAME, f"'{txt}' is not a positive number."
                )
            return
        fid = iid.split(":")[0]
        job = self.jobs[fid]
        if ":" in iid:
            ci = int(iid.split(":c")[1])
            ch = job.channels[ci]
            ch.emission_nm, ch.source, ch.warning = val, "user", ""
            others = [
                (f, c)
                for f, j in self.jobs.items()
                for c in j.channels
                if (f, c.index) != (fid, ci)
                and not j.error
                and core._norm(c.name) == core._norm(ch.name)
                and c.emission_nm != val
            ]
            if others and messagebox.askyesno(
                core.APP_NAME,
                f"Also set λ = {val:g} nm for the {len(others)} other '{ch.name}' channel(s) "
                "in the list?",
            ):
                for f, c in others:
                    c.emission_nm, c.source, c.warning = val, "user", ""
                    self._refresh(f)
        else:
            attr = FILE_EDIT[col]
            setattr(job, attr, val)
            if attr == "dz_nm":
                job.dz_source = "user"
            others = [
                f
                for f, j in self.jobs.items()
                if f != fid and not j.error and getattr(j, attr) != val
            ]
            if others and messagebox.askyesno(
                core.APP_NAME,
                f"Also set {FILE_LABEL[col]} = {val:g} for the {len(others)} other file(s)?",
            ):
                for f in others:
                    setattr(self.jobs[f], attr, val)
                    if attr == "dz_nm":
                        self.jobs[f].dz_source = "user"
                    self._refresh(f)
        self._refresh(fid)

    # wavelength table
    def _edit_table(self):
        core.load_wavelength_table()  # creates the file if missing
        p = core.wavelength_table_path()
        try:
            open_path(p)
        except Exception as e:
            messagebox.showinfo(
                core.APP_NAME,
                f"Open this file in a text editor:\n{p}\n\n({e})",
            )
            return
        self._set_status(
            f"Editing {p}. Save it, then press 'Reload wavelength table'."
        )

    def _reload_table(self):
        try:
            self.table = core.load_wavelength_table()
        except ValueError as e:
            messagebox.showerror(core.APP_NAME, str(e))
            return
        for fid, job in self.jobs.items():
            if job.status not in ("Done", "Running"):
                core.assign_wavelengths(job, self.table, keep_user=True)
                self._refresh(fid)
        self._set_status(
            "Wavelength table reloaded: "
            + ", ".join(f"{k}={v:g}" for k, v in self.table.items())
        )

    # running
    def _running(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def _start(self):
        if self._running():
            return
        if not self.dw_info.get("ok"):
            self._detect_dw()
            if not self.dw_info.get("ok"):
                return
        todo = [
            f
            for f, j in self.jobs.items()
            if j.status not in ("Done", "Error")
        ]
        if not todo:
            messagebox.showinfo(
                core.APP_NAME, "Nothing to process. Add ND2 files first."
            )
            return
        bad = [f for f in todo if self.jobs[f].problems()]
        good = [f for f in todo if f not in bad]
        if bad:
            names = "\n".join(
                f"• {self.jobs[f].name}: {'; '.join(self.jobs[f].problems())}"
                for f in bad[:10]
            )
            if not good:
                messagebox.showerror(
                    core.APP_NAME, f"These files need input first:\n\n{names}"
                )
                return
            if not messagebox.askyesno(
                core.APP_NAME,
                f"These files need input and will be skipped:\n\n{names}"
                f"\n\nProcess the other {len(good)} file(s)?",
            ):
                return
        warn = [
            f
            for f in good
            if self.jobs[f].notes()
            or any(c.warning for c in self.jobs[f].channels)
        ]
        if warn and not messagebox.askyesno(
            core.APP_NAME,
            f"{len(warn)} file(s) have orange notes (e.g. wavelengths that differ from the "
            "file's metadata). Did you check them? Continue?",
        ):
            return

        tile = self._int(self.v_tile.get(), 0)
        threads = self._int(self.v_threads.get(), 0)
        iters = self._int(self.v_iter.get(), 0)
        if iters < 1:
            messagebox.showerror(
                core.APP_NAME, "Iterations must be at least 1."
            )
            return
        s = core.RunSettings(
            dw_dir=self.v_dw.get().strip(),
            iterations=iters,
            gpu=self.v_gpu.get() and bool(self.dw_info.get("gpu")),
            tilesize=tile,
            threads=threads,
            out_dir=self.v_out.get().strip(),
            overwrite=self.v_over.get(),
            keep_intermediate=self.v_keep.get(),
        )
        self._save_settings()
        for f in good:
            self.jobs[f].status = "Queued"
            self._refresh(f)
        self.cancel_ev.clear()
        self.b_start.configure(state="disabled")
        self.b_cancel.configure(state="normal")
        self.worker = threading.Thread(
            target=self._work, args=(good, s), daemon=True
        )
        self.worker.start()

    def _work(self, fids: list[str], s: core.RunSettings):
        n = len(fids)
        ok = failed = 0
        for k, fid in enumerate(fids):
            job = self.jobs[fid]
            if self.cancel_ev.is_set():
                self.msgs.put(("job", fid, "Ready", None))
                continue
            self.msgs.put(("job", fid, "Running", None))
            self.msgs.put(("log", f"===== [{k + 1}/{n}] {job.name} ====="))

            def prog(frac, text, _k=k):
                self.msgs.put(
                    (
                        "progress",
                        (_k + frac) / n,
                        frac,
                        f"[{_k + 1}/{n}] {job.name}: {text}",
                    )
                )

            try:
                out = core.process_job(
                    job,
                    s,
                    lambda m: self.msgs.put(("log", m)),
                    prog,
                    self.cancel_ev,
                )
                ok += 1
                self.msgs.put(("job", fid, "Done", str(out)))
            except core.Cancelled:
                self.msgs.put(("log", "Cancelled."))
                self.msgs.put(("job", fid, "Ready", None))
            except FileExistsError as e:
                self.msgs.put(
                    (
                        "log",
                        f"Skipped: output already exists ({e}). Tick 'Overwrite' to replace it.",
                    )
                )
                self.msgs.put(("job", fid, "Skipped (exists)", None))
            except Exception as e:  # log it and move on to the next file
                failed += 1
                self.msgs.put(("log", f"FAILED: {e}"))
                self.msgs.put(("job", fid, "Failed", None))
        self.msgs.put(("finished", ok, failed, self.cancel_ev.is_set()))

    def _cancel(self):
        if self._running():
            self.cancel_ev.set()
            self._set_status("Cancelling…")

    def _open_out(self):
        p = self.v_out.get().strip()
        if not p:
            done = [j for j in self.jobs.values() if j.output]
            src = (
                Path(done[-1].output).parent
                if done
                else (
                    Path(next(iter(self.jobs.values())).path).parent
                    / "deconvolved"
                    if self.jobs
                    else None
                )
            )
            p = str(src) if src else ""
        if p and Path(p).exists():
            open_path(Path(p))
        else:
            messagebox.showinfo(core.APP_NAME, "No output folder yet.")

    # updates from the background thread
    def _pump(self):
        try:
            while True:
                m = self.msgs.get_nowait()
                kind = m[0]
                if kind == "log":
                    self._log(m[1])
                elif kind == "status":
                    self._set_status(m[1])
                elif kind == "added":
                    self._insert_job(m[1])
                elif kind == "progress":
                    self.pb_all["value"], self.pb_file["value"] = m[1], m[2]
                    self._set_status(m[3])
                elif kind == "job":
                    _, fid, status, out = m
                    if fid in self.jobs:
                        self.jobs[fid].status = status
                        if out:
                            self.jobs[fid].output = out
                        self._refresh(fid)
                elif kind == "finished":
                    _, ok, failed, cancelled = m
                    self.b_start.configure(state="normal")
                    self.b_cancel.configure(state="disabled")
                    self.pb_file["value"] = 0
                    msg = f"Finished: {ok} done, {failed} failed" + (
                        " (cancelled)" if cancelled else ""
                    )
                    self.pb_all["value"] = (
                        1.0 if not cancelled else self.pb_all["value"]
                    )
                    self._set_status(msg)
                    self._log(msg)
                    if failed:
                        messagebox.showwarning(
                            core.APP_NAME, msg + ". See the log for details."
                        )
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def _log(self, text: str):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 5000:
            self.log.delete("1.0", "1000.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_status(self, text: str):
        self.l_status.configure(text=text)

    def _on_close(self):
        if self._running():
            if not messagebox.askyesno(
                core.APP_NAME, "Processing is running. Cancel it and quit?"
            ):
                return
            self.cancel_ev.set()
            self.worker.join(timeout=10)
        self._save_settings()
        self.destroy()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()