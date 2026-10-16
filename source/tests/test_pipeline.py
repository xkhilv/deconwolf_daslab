"""Test the full pipeline on a fake 3-channel image using real dw and dw_bw.

Usage: python tests/test_pipeline.py <folder containing dw and dw_bw>

this file was made using claude cuz im too lazy to make a test myself
"""

import sys
import tempfile
import threading
from pathlib import Path

import numpy as np
import tifffile
from scipy.ndimage import gaussian_filter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daslab import core

dw_dir = sys.argv[1]
tmp = Path(tempfile.mkdtemp())

# Fake image: blurred dots plus noise
rng = np.random.default_rng(0)
C, Z, Y, X = 3, 12, 96, 128
truth = np.zeros((C, Z, Y, X), np.float32)
for c in range(C):
    idx = rng.integers([2, 5, 5], [Z - 2, Y - 5, X - 5], size=(40, 3))
    truth[c, idx[:, 0], idx[:, 1], idx[:, 2]] = 3000
blur = np.stack(
    [
        gaussian_filter(t, sigma=(2.5, 1.5 + 0.3 * c, 1.5 + 0.3 * c))
        for c, t in enumerate(truth)
    ]
)
data = (
    (blur + 100 + rng.normal(0, 5, blur.shape))
    .clip(0, 65535)
    .astype(np.uint16)
)

fake_nd2 = tmp / "Sodium arsenite test image1.nd2"
fake_nd2.write_bytes(b"")  # empty file; read_stack is replaced below
core.read_stack = lambda p: (data.copy(), ["Z", "C", "Y", "X"])

table = core.load_wavelength_table()
job = core.FileJob(
    path=str(fake_nd2),
    size_x=X,
    size_y=Y,
    size_z=Z,
    objective="Apo TIRF 100x Oil DIC N2",
    na=1.49,
    ni=1.515,
    dxy_nm=65,
    dz_nm=200,
    dz_source="Z-stack loop step",
)
job.channels = [
    core.ChannelInfo(0, "CY5", (255, 0, 255), meta_emission_nm=609),
    core.ChannelInfo(1, "TRITC", (255, 0, 0), meta_emission_nm=609),
    core.ChannelInfo(2, "GFP", (0, 255, 0), meta_emission_nm=526),
]
core.assign_wavelengths(job, table)
for c in job.channels:
    print(c.name, c.emission_nm, c.source, "|", c.warning)
assert [c.emission_nm for c in job.channels] == [680, 609, 526]
assert "609" in job.channels[0].warning
assert job.problems() == []

s = core.RunSettings(dw_dir=dw_dir, iterations=10, threads=4)
prog = []
out = core.process_job(
    job,
    s,
    log=lambda m: print("LOG", m),
    progress=lambda f, t: prog.append((f, t)),
    cancel=threading.Event(),
)
print("output:", out)

with tifffile.TiffFile(out) as tf:
    assert tf.is_ome
    arr = tf.asarray()
    ome = tf.ome_metadata
print("shape", arr.shape, arr.dtype)
assert arr.shape == (C, Z, Y, X) and arr.dtype == np.uint16
for s_ in [
    'Name="CY5"',
    'Name="TRITC"',
    'Name="GFP"',
    'PhysicalSizeX="0.065"',
    'PhysicalSizeZ="0.2"',
    'EmissionWavelength="680.0"',
    'Color="-16711681"',
]:
    assert s_ in ome, s_
# Dots should be sharper after deconvolution
for c in range(C):
    before = data[c].max() / data[c].mean()
    after = arr[c].max() / arr[c].mean()
    print(f"channel {c}: peak/mean before {before:.1f} after {after:.1f}")
    assert after > before
assert prog[-1][0] == 1.0
assert any("iteration 10/10" in t for _, t in prog), [t for _, t in prog]
assert not any(
    p.name.startswith(".") for p in out.parent.iterdir()
), "work folder not cleaned up"

# Running again should refuse to overwrite the output
try:
    core.process_job(job, s, print, lambda *a: None, threading.Event())
    raise AssertionError("should refuse to overwrite")
except FileExistsError:
    pass

# Cancel should stop the run
ev = threading.Event()
ev.set()
s.overwrite = True
try:
    core.process_job(job, s, lambda m: None, lambda *a: None, ev)
    raise AssertionError("should cancel")
except core.Cancelled:
    pass
print("ALL OK")