"""Diagnose SVG rendering on this machine.

Run from the project root:
    python debug_svg.py

It finds the first SVG-content file in data/icons/ and tests every
available renderer, printing the exact error for each failure.
"""

import os
import sys

icon_dir = "data/icons"
svg_path = None

print("Searching for SVG-content files in data/icons/ ...")
for fname in sorted(os.listdir(icon_dir)):
    path = os.path.join(icon_dir, fname)
    try:
        with open(path, "rb") as fh:
            head = fh.read(512)
        sniff = head.lstrip()[:200].lower()
        if b"<svg" in sniff or b"<?xml" in sniff:
            svg_path = path
            print(f"  Found SVG content: {path}")
            print(f"  First 120 bytes:   {head[:120]!r}")
            break
    except OSError:
        pass

if svg_path is None:
    print("No SVG-content files found in data/icons/. Nothing to test.")
    sys.exit(0)

print()
print("=" * 60)
print("Python:", sys.version)
print("=" * 60)

# ── svglib + reportlab ────────────────────────────────────────────
print("\n[1] svglib + reportlab")
try:
    from svglib.svglib import svg2rlg
    from reportlab.graphics import renderPM
    print(f"  svglib imported OK")
    import reportlab
    print(f"  reportlab version: {reportlab.Version}")
    drawing = svg2rlg(svg_path)
    print(f"  svg2rlg returned: {drawing}")
    if drawing is None:
        print("  svg2rlg returned None -> svglib could not parse the SVG")
    else:
        print(f"  drawing.width={drawing.width}, drawing.height={getattr(drawing,'height','?')}")
        try:
            img = renderPM.drawToPIL(drawing, dpi=96)
            print(f"  renderPM.drawToPIL succeeded: size={img.size} mode={img.mode}")
        except Exception as e:
            import traceback
            print(f"  renderPM.drawToPIL FAILED: {type(e).__name__}: {e}")
            traceback.print_exc()
except ImportError as e:
    print(f"  NOT INSTALLED: {e}")
except Exception as e:
    import traceback
    print(f"  UNEXPECTED ERROR: {type(e).__name__}: {e}")
    traceback.print_exc()

# ── cairosvg ─────────────────────────────────────────────────────
print("\n[2] cairosvg")
try:
    import cairosvg
    from io import BytesIO
    from PIL import Image
    print(f"  cairosvg imported OK, version: {cairosvg.__version__}")
    png = cairosvg.svg2png(url=svg_path, output_width=64, output_height=64)
    img = Image.open(BytesIO(png))
    print(f"  SUCCESS: size={img.size} mode={img.mode}")
except ImportError as e:
    print(f"  NOT INSTALLED: {e}")
except Exception as e:
    import traceback
    print(f"  FAILED: {type(e).__name__}: {e}")
    traceback.print_exc()

# ── Wand (ImageMagick) ───────────────────────────────────────────
print("\n[3] Wand (ImageMagick)")
try:
    from wand.image import Image as WandImage
    from io import BytesIO
    from PIL import Image
    with WandImage(filename=svg_path, resolution=96) as wimg:
        wimg.format = "png"
        png_bytes = wimg.make_blob()
    img = Image.open(BytesIO(png_bytes))
    print(f"  SUCCESS: size={img.size} mode={img.mode}")
except ImportError as e:
    print(f"  NOT INSTALLED: {e}")
except Exception as e:
    import traceback
    print(f"  FAILED: {type(e).__name__}: {e}")
    traceback.print_exc()

# ── Pillow direct (expected to fail for SVG) ─────────────────────
print("\n[4] Pillow direct open (expected to fail on SVG)")
try:
    from PIL import Image
    img = Image.open(svg_path)
    img.load()
    print(f"  Unexpectedly SUCCEEDED: size={img.size}")
except Exception as e:
    print(f"  Failed as expected: {type(e).__name__}: {e}")

print()
print("Done.")
