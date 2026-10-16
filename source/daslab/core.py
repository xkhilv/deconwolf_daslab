"""
deconwolf pipeline:

steps for each ND2 file:
1. read the metadata and the image
2. save each channel as its own TIFF
3. make a PSF for each channel with dw_bw
4. deconvolve each channel with dw
5. combine the channels into one OME-TIFF with the original names, colors and pixel size
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import tifffile

APP_NAME = "Deconwolf - Das Lab"
APP_VERSION = "1.0"


# folders and settings


def app_data_dir() -> Path:
    """dolder for settings, the wavelength table and saved PSFs"""

    if sys.platform.startswith("win"):
        base = Path(
            os.environ.get("LOCALAPPDATA")
            or os.environ.get("APPDATA")
            or Path.home()
        )
    else:
        base = Path(
            os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
        )
    d = base / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def psf_cache_dir() -> Path:
    d = app_data_dir() / "psf_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


# channel name to emission wavelength (nm)

DEFAULT_WAVELENGTH_TABLE = {
    "_help": (
        "Channel name and emission wavelength in nm, used to make the PSF. "
        "Case, spaces and symbols are ignored. A key also matches a longer name "
        "that contains it (for example 'CY5' matches 'Cy5-647'). "
        "Use the dye's emission peak or the center of the emission filter. "
        "Save this file, then click 'Reload wavelength table' in the app."
    ),
    "channels": {
        "GFP": 526,
        "TRITC": 609,
        "CY5": 680,
    },
}


def wavelength_table_path() -> Path:
    return app_data_dir() / "wavelengths.json"


def load_wavelength_table() -> dict[str, float]:
    p = wavelength_table_path()
    if not p.exists():
        p.write_text(
            json.dumps(DEFAULT_WAVELENGTH_TABLE, indent=2), encoding="utf-8"
        )
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        chans = data.get("channels", {})
        return {str(k): float(v) for k, v in chans.items()}
    except Exception as e:
        raise ValueError(f"Could not read {p}: {e}") from e


def _norm(s: str) -> str:
    """lowercase and keep only letters and numbers"""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def lookup_wavelength(
    channel_name: str, table: dict[str, float]
) -> Optional[tuple[str, float]]:
    """find a channel in the table; returns (key, nm) or None"""
    n = _norm(channel_name)
    if not n:
        return None
    norm_table = {_norm(k): (k, v) for k, v in table.items() if _norm(k)}
    if n in norm_table:
        return norm_table[n]
    # if no exact match: use the longest key found inside the name
    hits = [(len(k), kv) for k, kv in norm_table.items() if k in n]
    if hits:
        hits.sort(key=lambda t: -t[0])
        return hits[0][1]
    return None


# data


@dataclass
class ChannelInfo:
    index: int
    name: str
    color_rgb: tuple[int, int, int] = (255, 255, 255)
    meta_emission_nm: Optional[float] = None  # value in the ND2 file
    emission_nm: Optional[float] = None  # value we will use
    source: str = ""  # where emission_nm came from: "table", "file" or "user"
    warning: str = ""


@dataclass
class FileJob:
    path: str
    size_x: int = 0
    size_y: int = 0
    size_z: int = 0
    objective: str = ""
    na: Optional[float] = None
    ni: Optional[float] = None
    dxy_nm: Optional[float] = None
    dz_nm: Optional[float] = None
    dz_source: str = ""
    channels: list[ChannelInfo] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: Optional[str] = None  # set if the file can't be processed at all
    status: str = "Ready"
    output: Optional[str] = None

    @property
    def name(self) -> str:
        return Path(self.path).name

    def problems(self) -> list[str]:
        """things that must be fixed before this file can run"""
        p = []
        if self.error:
            return [self.error]
        if not self.na or self.na <= 0:
            p.append("NA missing")
        if not self.ni or self.ni < 1:
            p.append("immersion refractive index missing")
        if self.na and self.ni and self.na >= self.ni:
            p.append(
                f"NA ({self.na}) must be smaller than immersion index ({self.ni})"
            )
        if not self.dxy_nm or self.dxy_nm <= 0:
            p.append("XY pixel size missing")
        if not self.dz_nm or self.dz_nm <= 0:
            p.append("Z step missing")
        for c in self.channels:
            if not c.emission_nm or c.emission_nm <= 0:
                p.append(f"emission wavelength missing for channel '{c.name}'")
        return p

    def notes(self) -> list[str]:
        """things the user should check, but these don't stop the run"""
        n = list(self.warnings)
        lams = [c.emission_nm for c in self.channels if c.emission_nm]
        dupes = sorted({x for x in lams if lams.count(x) > 1})
        if dupes:
            n.append(
                "several channels use the same wavelength: "
                + ", ".join(f"{d:g}" for d in dupes)
            )
        return n


# reading ND2 files


def _z_step_um(f) -> tuple[Optional[float], str]:
    """get the Z step in µm; tries the Z-stack setting, then stage positions, then calibration"""
    import nd2

    # 1. the Z step set in NIS-Elements
    try:
        for loop in f.experiment or []:
            if isinstance(loop, nd2.structures.ZStackLoop):
                step = float(loop.parameters.stepUm)
                if step > 0:
                    return step, "Z-stack loop step"
    except Exception:
        pass

    # 2. distance between the stage positions of the first two planes
    try:
        z_by_index = {}
        for i in range(f.attributes.sequenceCount):
            li = f.loop_indices[i]
            if li.get("Z") in (0, 1) and all(
                v == 0 for k, v in li.items() if k != "Z"
            ):
                fm = f.frame_metadata(i)
                z_by_index[li["Z"]] = fm.channels[0].position.stagePositionUm.z
            if len(z_by_index) == 2:
                break
        if len(z_by_index) == 2:
            step = abs(z_by_index[1] - z_by_index[0])
            if step > 0:
                return round(step, 4), "stage Z positions"
    except Exception:
        pass

    # 3. voxel calibration
    try:
        vz = float(f.voxel_size().z)
        if vz > 0 and vz != 1.0:
            return vz, "voxel calibration"
    except Exception:
        pass
    return None, ""


def read_nd2_metadata(path: str, table: dict[str, float]) -> FileJob:
    import nd2

    job = FileJob(path=str(path))
    try:
        with nd2.ND2File(path) as f:
            sizes = dict(f.sizes)
            job.size_x = int(sizes.get("X", 0))
            job.size_y = int(sizes.get("Y", 0))
            job.size_z = int(sizes.get("Z", 1))
            extra = {
                k: v
                for k, v in sizes.items()
                if k not in ("X", "Y", "Z", "C") and v > 1
            }
            if f.is_rgb:
                job.error = "RGB images are not supported"
            elif extra:
                dims = ", ".join(f"{k}={v}" for k, v in extra.items())
                job.error = f"extra dimensions ({dims}): multi-position and time-lapse files are not supported yet"
            elif job.size_z < 2:
                job.error = "not a Z-stack (deconwolf needs 3D images)"

            md_channels = (f.metadata.channels if f.metadata else None) or []
            if md_channels:
                mic = md_channels[0].microscope
                job.objective = mic.objectiveName or ""
                job.na = (
                    float(mic.objectiveNumericalAperture)
                    if mic.objectiveNumericalAperture
                    else None
                )
                job.ni = (
                    float(mic.immersionRefractiveIndex)
                    if mic.immersionRefractiveIndex
                    else None
                )

            try:
                vx = float(f.voxel_size().x)
                if vx > 0 and vx != 1.0:
                    job.dxy_nm = round(vx * 1000, 3)
            except Exception:
                pass

            dz, src = _z_step_um(f)
            if dz:
                job.dz_nm = round(dz * 1000, 3)
                job.dz_source = src

            n_c = int(sizes.get("C", 1))
            for i in range(n_c):
                ch = md_channels[i].channel if i < len(md_channels) else None
                name = (ch.name if ch else "") or f"C{i + 1}"
                rgb = (255, 255, 255)
                if ch is not None and ch.color is not None:
                    rgb = (int(ch.color.r), int(ch.color.g), int(ch.color.b))
                meta_em = (
                    float(ch.emissionLambdaNm)
                    if ch and ch.emissionLambdaNm
                    else None
                )
                job.channels.append(
                    ChannelInfo(
                        index=i,
                        name=name,
                        color_rgb=rgb,
                        meta_emission_nm=meta_em,
                    )
                )
    except Exception as e:
        job.error = f"could not read file: {e}"

    assign_wavelengths(job, table)
    if not job.error and job.problems():
        job.status = "Needs input"
    elif job.error:
        job.status = "Error"
    return job


def assign_wavelengths(
    job: FileJob, table: dict[str, float], keep_user: bool = True
) -> None:
    """set each channel's wavelength from the table, or from the file if it's not in the table"""
    for c in job.channels:
        if keep_user and c.source == "user":
            continue
        c.warning = ""
        hit = lookup_wavelength(c.name, table)
        if hit:
            key, nm = hit
            c.emission_nm, c.source = nm, "table"
            if c.meta_emission_nm and abs(c.meta_emission_nm - nm) > 15:
                c.warning = f"file says {c.meta_emission_nm:g} nm; using table value {nm:g} nm ('{key}')"
        elif c.meta_emission_nm:
            c.emission_nm, c.source = c.meta_emission_nm, "file"
            c.warning = "not in wavelength table; using the value stored in the file, please check it"
        else:
            c.emission_nm, c.source = None, ""
            c.warning = "no wavelength found, please enter one"


# finding deconwolf


def _exe(d: Path, name: str) -> Optional[Path]:
    for cand in (d / f"{name}.exe", d / name):
        if cand.is_file():
            return cand
    return None


def find_deconwolf_dir(hint: str = "") -> Optional[Path]:
    """look for the folder with dw and dw_bw"""
    cands = []
    if hint:
        cands.append(Path(hint))
    w = shutil.which("dw")
    if w:
        cands.append(Path(w).parent)
    for env in (
        "ProgramFiles",
        "ProgramFiles(x86)",
        "ProgramW6432",
        "LOCALAPPDATA",
    ):
        v = os.environ.get(env)
        if v:
            cands += [
                Path(v) / "deconwolf",
                Path(v) / "Programs" / "deconwolf",
            ]
    for d in cands:
        if d.is_dir() and _exe(d, "dw") and _exe(d, "dw_bw"):
            return d
    return None


def _creationflags() -> int:
    """on windows, stop a console window from popping up"""
    return (
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if sys.platform.startswith("win")
        else 0
    )


def deconwolf_info(d: Path) -> dict:
    """run 'dw --version' to get the version and check for GPU support"""
    dw = _exe(d, "dw")
    try:
        r = subprocess.run(
            [str(dw), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=_creationflags(),
        )
        txt = (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return {"ok": False, "text": str(e), "gpu": False}
    gpu = bool(re.search(r"OpenCL:\s*YES", txt, re.I))
    m = re.search(r"deconwolf:\s*'?([\w.\-]+)", txt, re.I) or re.search(
        r"(\d+\.\d+\.\d+[\w\-]*)", txt
    )
    return {
        "ok": True,
        "text": txt.strip(),
        "gpu": gpu,
        "version": m.group(1) if m else "?",
    }


# running


class Cancelled(Exception):
    pass


@dataclass
class RunSettings:
    dw_dir: str
    iterations: int = 40
    gpu: bool = False
    tilesize: int = 0  # 0 means no tiling
    threads: int = 0  # 0 means let deconwolf decide
    out_dir: str = ""  # empty means a "deconvolved" folder next to each file
    overwrite: bool = False
    keep_intermediate: bool = False


LogFn = Callable[[str], None]
ProgressFn = Callable[
    [float, str], None
]  # (progress for this file from 0 to 1, message)

_ITER_RE = re.compile(r"Iteration\s+(\d+)\s*/\s*(\d+)")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")  # terminal color codes


def _run(
    cmd: list[str],
    log: LogFn,
    cancel: threading.Event,
    on_line: Optional[Callable[[str], None]] = None,
    watch: Optional[Path] = None,
) -> int:
    """run a command and send its output to the log.
    if 'watch' is set, new lines in that file go to on_line. dw writes its
    iteration progress to its log file, so we use this for the progress bar."""
    log("> " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        creationflags=_creationflags(),
        errors="replace",
    )
    q: list[str] = []
    watch_pos = 0

    def reader():
        for line in p.stdout:
            q.append(_ANSI_RE.sub("", line.rstrip()))

    def poll_watch():
        nonlocal watch_pos
        if not (watch and on_line and watch.exists()):
            return
        try:
            with open(watch, "r", errors="replace") as fh:
                fh.seek(watch_pos)
                chunk = fh.read()
                watch_pos = fh.tell()
            for ln in chunk.splitlines():
                on_line(ln)
        except OSError:
            pass

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    while True:
        while q:
            line = q.pop(0)
            m = _ITER_RE.search(line)
            if on_line and not watch:
                on_line(line)
            # only log every 10th iteration to keep the log short
            if m:
                i, n = int(m.group(1)), int(m.group(2))
                if i % 10 == 0 or i == n or i == 1:
                    log("   " + line.strip())
            elif line.strip() and not line.strip().startswith("Iterating"):
                log("   " + line.strip())
        poll_watch()
        if cancel.is_set():
            p.terminate()
            try:
                p.wait(5)
            except Exception:
                p.kill()
            raise Cancelled()
        if p.poll() is not None and not t.is_alive():
            while q:
                line = q.pop(0)
                if line.strip() and not _ITER_RE.search(line):
                    log("   " + line.strip())
            return p.returncode
        time.sleep(0.05)


def output_path_for(job: FileJob, s: RunSettings) -> Path:
    src = Path(job.path)
    out_dir = Path(s.out_dir) if s.out_dir else src.parent / "deconvolved"
    return out_dir / f"{src.stem}_dw.ome.tif"


def psf_path_for(job: FileJob, lam: float) -> Path:
    """the PSF file name includes all its settings, so it can be reused"""
    nslice = 2 * job.size_z - 1
    name = (
        f"psf_NA{job.na:g}_ni{job.ni:g}_xy{job.dxy_nm:g}_z{job.dz_nm:g}"
        f"_lambda{lam:g}_P{nslice}.tif"
    )
    return psf_cache_dir() / name


def make_psf(
    job: FileJob,
    lam: float,
    s: RunSettings,
    log: LogFn,
    cancel: threading.Event,
) -> Path:
    """make a PSF with dw_bw, or reuse a saved one"""
    p = psf_path_for(job, lam)
    if p.exists():
        log(f"Using saved PSF {p.name}")
        return p
    dw_bw = _exe(Path(s.dw_dir), "dw_bw")
    # write to a temporary name first so a half-written PSF is never reused
    tmp = p.with_suffix(".partial.tif")
    # PSF needs 2 * planes - 1 slices (deconwolf's recommendation)
    cmd = [
        str(dw_bw),
        "--NA",
        f"{job.na:g}",
        "--ni",
        f"{job.ni:g}",
        "--resxy",
        f"{job.dxy_nm:g}",
        "--resz",
        f"{job.dz_nm:g}",
        "--lambda",
        f"{lam:g}",
        "--nslice",
        str(2 * job.size_z - 1),
        "--threads",
        str(s.threads or os.cpu_count() or 1),
        "--overwrite",
        str(tmp),
    ]
    rc = _run(cmd, log, cancel)
    if rc != 0 or not tmp.exists():
        raise RuntimeError(
            f"dw_bw failed (exit code {rc}) for lambda={lam:g} nm"
        )
    tmp.replace(p)
    for extra in tmp.parent.glob(
        tmp.name + "*"
    ):  # also rename dw_bw's log file
        try:
            extra.replace(p.parent / extra.name.replace(tmp.name, p.name))
        except Exception:
            pass
    return p


def read_stack(path: str) -> tuple[np.ndarray, list[str]]:
    """Read the ND2 image as (channel, Z, Y, X)."""
    import nd2

    with nd2.ND2File(path) as f:
        arr = f.asarray()
        dims = list(f.sizes.keys())
    if "C" not in dims:
        arr = arr[np.newaxis]
        dims = ["C"] + dims
    order = [dims.index(a) for a in ("C", "Z", "Y", "X")]
    return np.ascontiguousarray(np.transpose(arr, order)), dims


def _ome_color(rgb: tuple[int, int, int]) -> int:
    """convert RGB to the signed 32-bit number OME uses for colors"""
    r, g, b = (int(v) & 0xFF for v in rgb)
    v = (r << 24) | (g << 16) | (b << 8) | 0xFF
    return v - (1 << 32) if v >= (1 << 31) else v


def write_ome_tiff(
    path: Path, data_czyx: np.ndarray, job: FileJob, description: str
) -> None:
    md = {
        "axes": "CZYX",
        "PhysicalSizeX": job.dxy_nm / 1000.0,
        "PhysicalSizeXUnit": "µm",
        "PhysicalSizeY": job.dxy_nm / 1000.0,
        "PhysicalSizeYUnit": "µm",
        "PhysicalSizeZ": job.dz_nm / 1000.0,
        "PhysicalSizeZUnit": "µm",
        "Channel": {
            "Name": [c.name for c in job.channels],
            "Color": [_ome_color(c.color_rgb) for c in job.channels],
            "EmissionWavelength": [float(c.emission_nm) for c in job.channels],
            "EmissionWavelengthUnit": ["nm"] * len(job.channels),
        },
        "Description": description,
    }
    tmp = path.with_name(path.name + ".partial")
    tifffile.imwrite(
        tmp,
        data_czyx,
        ome=True,
        photometric="minisblack",
        metadata=md,
        bigtiff=data_czyx.nbytes > 3_500_000_000,
    )
    tmp.replace(path)


def _scaling_from_log(p: Path) -> Optional[float]:
    """read the output scaling factor from dw's log file"""
    try:
        m = re.findall(
            r"scaling:\s*([0-9.eE+\-]+)", p.read_text(errors="replace")
        )
        return float(m[-1]) if m else None
    except Exception:
        return None


def process_job(
    job: FileJob,
    s: RunSettings,
    log: LogFn,
    progress: ProgressFn,
    cancel: threading.Event,
) -> Path:
    """run the full pipeline on one ND2 file; returns the output path"""
    probs = job.problems()
    if probs:
        raise RuntimeError("; ".join(probs))
    out = output_path_for(job, s)
    if out.exists() and not s.overwrite:
        raise FileExistsError(str(out))
    out.parent.mkdir(parents=True, exist_ok=True)
    work = Path(
        tempfile.mkdtemp(
            prefix=f".{Path(job.path).stem[:40]}_work_", dir=out.parent
        )
    )
    dw = _exe(Path(s.dw_dir), "dw")
    n_c = len(job.channels)
    steps = (
        1 + 2 * n_c + 1
    )  # read, then PSF and deconvolve per channel, then write
    done = 0

    def step(text: str, sub: float = 0.0):
        progress(min(1.0, (done + sub) / steps), text)

    try:
        # split channels into separate TIFFs
        step("Reading ND2")
        log(f"Reading {job.path}")
        data, _ = read_stack(job.path)
        if data.shape[0] != n_c:
            raise RuntimeError(
                f"expected {n_c} channels, found {data.shape[0]}"
            )
        in_dtype = data.dtype
        if data.dtype not in (np.uint8, np.uint16, np.float32):
            data = data.astype(np.float32)
        ch_files = []
        for c in job.channels:
            p = work / f"c{c.index + 1}.tif"
            tifffile.imwrite(p, data[c.index], photometric="minisblack")
            ch_files.append(p)
        shape = data.shape
        del data
        done += 1

        # PSF and deconvolution for each channel
        results, scalings, commands = [], [], []
        for c, inp in zip(job.channels, ch_files):
            if cancel.is_set():
                raise Cancelled()
            step(f"PSF for {c.name} ({c.emission_nm:g} nm)")
            psf = make_psf(job, c.emission_nm, s, log, cancel)
            done += 1

            outc = work / f"dw_c{c.index + 1}.tif"

            def dw_cmd(tilesize: int) -> list[str]:
                cmd = [
                    str(dw),
                    "--iter",
                    str(s.iterations),
                    "--overwrite",
                    "--out",
                    str(outc),
                ]
                if s.gpu:
                    cmd.append("--gpu")
                if tilesize:
                    cmd += ["--tilesize", str(tilesize)]
                if s.threads:
                    cmd += ["--threads", str(s.threads)]
                return cmd + [str(inp), str(psf)]

            def on_line(line: str, _name=c.name):
                m = _ITER_RE.search(line)
                if m:
                    i, n = int(m.group(1)), int(m.group(2))
                    step(
                        f"Deconvolving {_name}: iteration {i}/{n}",
                        i / max(n, 1),
                    )

            step(f"Deconvolving {c.name}")
            cmd = dw_cmd(s.tilesize)
            dwlog = Path(str(outc) + ".log.txt")
            dwlog.unlink(missing_ok=True)
            rc = _run(cmd, log, cancel, on_line, watch=dwlog)
            # a failure here is usually running out of memory, so try once more with tiles
            if (rc != 0 or not outc.exists()) and not s.tilesize:
                log(
                    f"dw failed (exit code {rc}). Trying again with --tilesize 1024 "
                    "(this usually means it ran out of memory)."
                )
                cmd = dw_cmd(1024)
                dwlog.unlink(missing_ok=True)
                rc = _run(cmd, log, cancel, on_line, watch=dwlog)
            if rc != 0 or not outc.exists():
                raise RuntimeError(
                    f"dw failed (exit code {rc}) on channel {c.name}; see the log above"
                )
            commands.append(" ".join(cmd))
            scalings.append(_scaling_from_log(dwlog))
            results.append(outc)
            done += 1

        # combine channels into one OME-TIFF
        step("Writing OME-TIFF")
        stack = np.empty(shape, dtype=np.uint16)
        for c, r in zip(job.channels, results):
            img = tifffile.imread(r)
            if img.shape != shape[1:]:
                raise RuntimeError(
                    f"output for {c.name} has shape {img.shape}, expected {shape[1:]}"
                )
            stack[c.index] = img

        lines = [
            f"Deconvolved with deconwolf using {APP_NAME} {APP_VERSION}",
            f"Source: {Path(job.path).name}",
            f"Objective: {job.objective}  NA={job.na:g}  ni={job.ni:g}",
            f"Pixel size: {job.dxy_nm:g} nm  Z step: {job.dz_nm:g} nm ({job.dz_source or 'user'})",
            f"Iterations: {s.iterations}  GPU: {s.gpu}  Tilesize: {s.tilesize or 'off'}",
        ]
        for c, sc in zip(job.channels, scalings):
            lines.append(
                f"Channel {c.index + 1} {c.name}: lambda={c.emission_nm:g} nm ({c.source}), "
                f"output scaling={sc if sc is not None else '?'}"
            )
        if in_dtype != np.uint16:
            lines.append(f"Input pixel type was {in_dtype}; output is uint16")
        desc = "\n".join(lines)
        write_ome_tiff(out, stack, job, desc)
        Path(str(out) + ".params.txt").write_text(
            desc + "\n\nCommands:\n" + "\n".join(commands) + "\n",
            encoding="utf-8",
        )
        done += 1
        step("Done")
        log(f"Saved {out}")
        return out
    finally:
        if s.keep_intermediate:
            log(f"Intermediate files kept in {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)