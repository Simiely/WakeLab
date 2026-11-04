# -*- coding: utf-8 -*-
"""WakeLab 打包脚本：onefile + windowed，产物在 dist\\WakeLab.exe。"""
import os

for k in ("TCL_LIBRARY", "TK_LIBRARY", "PYTHONPATH"):
    os.environ.pop(k, None)
assert "TCL_LIBRARY" not in os.environ

import PyInstaller.__main__

EXCLUDES = [
    "--exclude-module", "PIL.Jpeg2KImagePlugin",
    "--exclude-module", "PIL.WebPImagePlugin",
    "--exclude-module", "PIL.TiffImagePlugin",
    "--exclude-module", "PIL.JpegImagePlugin",
    "--exclude-module", "PIL.MpoImagePlugin",
    "--exclude-module", "PIL.PdfImagePlugin",
    "--exclude-module", "PIL.EpsImagePlugin",
    "--exclude-module", "PIL.DdsImagePlugin",
    "--exclude-module", "PIL.MicImagePlugin",
    "--exclude-module", "PIL.McIdasImagePlugin",
    "--exclude-module", "PIL.SpiderImagePlugin",
    "--exclude-module", "PIL.FliImagePlugin",
    "--exclude-module", "PIL.FpxImagePlugin",
    "--exclude-module", "PIL.GbrImagePlugin",
    "--exclude-module", "PIL.ImImagePlugin",
    "--exclude-module", "PIL.ImtImagePlugin",
    "--exclude-module", "PIL.IptcImagePlugin",
    "--exclude-module", "PIL.PcdImagePlugin",
    "--exclude-module", "PIL.PcxImagePlugin",
    "--exclude-module", "PIL.PixarImagePlugin",
    "--exclude-module", "PIL.SgiImagePlugin",
    "--exclude-module", "PIL.SunImagePlugin",
    "--exclude-module", "PIL.WmfImagePlugin",
    "--exclude-module", "PIL.XbmImagePlugin",
    "--exclude-module", "PIL.XpmImagePlugin",
    "--exclude-module", "PIL.BlpImagePlugin",
    "--exclude-module", "PIL.CurImagePlugin",
    "--exclude-module", "PIL.Hdf5StubImagePlugin",
    "--exclude-module", "PIL.MpegImagePlugin",
    "--exclude-module", "PIL._imagingft",
    "--exclude-module", "PIL._imagingcms",
    "--exclude-module", "PIL._webp",
    "--exclude-module", "PIL._imagingquant",
    "--exclude-module", "numpy",
    "--exclude-module", "numpy.typing",
    "--exclude-module", "PIL.AvifImagePlugin",
    "--exclude-module", "PIL._avif",
    "--exclude-module", "unittest",
    "--exclude-module", "pydoc_data",
]

args = [
    "--noconfirm", "--clean", "--onefile", "--windowed",
    "--name", "WakeLab",
    "--distpath", "dist",
    "--workpath", "build",
    "--specpath", ".",
] + EXCLUDES + ["wake_lab.py"]

PyInstaller.__main__.run(args)
print("BUILD DONE -> dist\\WakeLab.exe")
