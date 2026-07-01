"""
Gnoll Guard artwork cropper.

Drop your raw Gemini images in the same folder as this script, name them
as shown below, then run:

    python tools/crop_artwork.py

It will crop the watermark from the bottom-right corner of each image,
resize to the correct final dimensions, and write the output files to
tools/output/ — ready to drop straight into assets/ or web/public/.

Expected input filenames
------------------------
    icon_raw.png          Gnoll head portrait (brand icon / favicon)
    banner_raw.png        Hero banner (website homepage)
    mascot_raw.png        Friendly gnoll helper (mascot section)
    tray_raw.png          Paw print (system tray icon)
    panel_raw.png         Alert window background panel
    og_raw.png            OG / social sharing banner
    download_raw.png      Download page illustration

Watermark crop
--------------
The Gemini watermark is a small star in the very bottom-right corner.
CROP_PCT controls how much is removed from the bottom and right edges.
Default 7% removes the star cleanly on all observed outputs.
Increase it if the star is still visible after cropping.
"""

import os
import sys

try:
    from PIL import Image
except ImportError:
    sys.exit("Pillow is required: pip install pillow")

# ── Config ────────────────────────────────────────────────────────────────────

# Percentage to crop from the bottom AND right edge to remove the watermark.
# 7% is safe for all image sizes Gemini produces.
CROP_PCT = 0.07

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")

# (input_filename, output_filename, final_width, final_height)
IMAGES = [
    ("icon_raw.png",     "icon.png",          512, 512),   # → assets/ + web/public/gnoll-icon.png
    ("banner_raw.png",   "hero-banner.png",  1776, 576),   # → web/public/
    ("mascot_raw.png",   "gnoll-mascot.png",  672, 577),   # → web/public/
    ("tray_raw.png",     "tray_icon.png",      64,  64),   # → assets/
    ("panel_raw.png",    "LootWindow.png",    344, 248),   # → assets/
    ("og_raw.png",       "og-image.png",     1200, 630),   # → web/public/
    ("download_raw.png", "download-art.png",  600, 400),   # → web/public/
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def crop_watermark(img: Image.Image, pct: float = CROP_PCT) -> Image.Image:
    """Remove pct% from the right edge and pct% from the bottom edge."""
    w, h = img.size
    right  = int(w * (1 - pct))
    bottom = int(h * (1 - pct))
    return img.crop((0, 0, right, bottom))


def process(src_path: str, dst_path: str, width: int, height: int):
    img = Image.open(src_path).convert("RGBA")
    print(f"  Loaded  {os.path.basename(src_path):25s}  {img.size[0]}×{img.size[1]}")

    cropped = crop_watermark(img)
    print(f"  Cropped → {cropped.size[0]}×{cropped.size[1]}")

    resized = cropped.resize((width, height), Image.LANCZOS)
    print(f"  Resized → {width}×{height}")

    # Save as PNG (keeps transparency for icons) or high-quality JPEG for banners
    if dst_path.endswith(".png"):
        resized.save(dst_path, "PNG", optimize=True)
    else:
        resized.convert("RGB").save(dst_path, "JPEG", quality=92)

    print(f"  Saved   {os.path.basename(dst_path)}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    processed = 0
    skipped   = 0

    for src_name, dst_name, w, h in IMAGES:
        src_path = os.path.join(SCRIPT_DIR, src_name)
        dst_path = os.path.join(OUTPUT_DIR, dst_name)

        if not os.path.isfile(src_path):
            print(f"  SKIP  {src_name} (not found)\n")
            skipped += 1
            continue

        print(f"Processing {src_name} → {dst_name} ({w}×{h})")
        try:
            process(src_path, dst_path, w, h)
            processed += 1
        except Exception as e:
            print(f"  ERROR: {e}\n")

    print(f"Done. {processed} image(s) written to tools/output/")
    if skipped:
        print(f"      {skipped} skipped (files not found — name them as shown above)")


if __name__ == "__main__":
    main()
