"""Prepare standalone Noto font faces from Debian packages during image build."""

from pathlib import Path
from shutil import copyfile

from fontTools.ttLib import TTCollection


def main() -> None:
    destination = Path("/opt/fonts")
    destination.mkdir(parents=True, exist_ok=True)
    required = {
        "NotoSansCJKsc-Regular",
        "NotoSansCJKtc-Regular",
        "NotoSansCJKjp-Regular",
        "NotoSansCJKkr-Regular",
    }
    # Select by PostScript name, not collection order, to preserve regional glyphs.
    with TTCollection("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc") as fonts:
        for font in fonts.fonts:
            name = font["name"].getDebugName(6)
            if name in required:
                font.save(destination / f"{name}.otf")
                required.remove(name)
    if required:
        raise RuntimeError(f"Noto CJK faces missing: {sorted(required)}")

    for name in ("NotoSans", "NotoSansArabic", "NotoSansThai"):
        filename = f"{name}-Regular.ttf"
        copyfile(Path("/usr/share/fonts/truetype/noto") / filename, destination / filename)
    for package in ("fonts-noto-core", "fonts-noto-cjk"):
        copyfile(
            Path("/usr/share/doc") / package / "copyright",
            destination / f"{package}-copyright",
        )


if __name__ == "__main__":
    main()
