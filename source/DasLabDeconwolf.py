import sys


def selftest() -> int:
    import tempfile
    from pathlib import Path

    import nd2  # noqa: F401
    import nd2.structures  # noqa: F401
    import numpy as np
    import tifffile

    from daslab import core

    job = core.FileJob(
        path="x.nd2", size_z=2, na=1.49, ni=1.515, dxy_nm=65, dz_nm=200
    )
    job.channels = [core.ChannelInfo(0, "CY5", (255, 0, 255), 609)]
    core.assign_wavelengths(job, {"CY5": 680})
    out = Path(tempfile.mkdtemp()) / "t.ome.tif"
    core.write_ome_tiff(
        out, np.zeros((1, 2, 4, 4), np.uint16), job, "selftest"
    )
    ok = tifffile.TiffFile(out).is_ome and job.channels[0].emission_nm == 680
    msg = f"nd2 {nd2.__version__}, tifffile {tifffile.__version__}: {'OK' if ok else 'FAILED'}"
    Path("selftest.txt").write_text(msg + "\n", encoding="utf-8")
    if sys.stdout:
        print(msg)
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    from daslab.gui import main

    main()