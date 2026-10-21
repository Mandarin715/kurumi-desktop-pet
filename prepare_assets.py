"""Convert the supplied white-background JPGs into transparent PNGs locally.

Only near-white pixels connected to an image edge are removed. The supplied
half-body stickers have open bottom outlines: close those in the flood-fill
mask first so the fill cannot escape into white skirts. Original RGB pixels
are preserved. Trim paths apply only to SHA-256-matching original JPGs.
"""
import argparse
import hashlib
import json
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent


def pixels(image):
    method = getattr(image, 'get_flattened_data', image.getdata)
    return method()


def cutout(source, target, close_path=()):
    rgb = Image.open(source).convert('RGB')
    width, height = rgb.size
    candidate = Image.new('L', rgb.size)
    candidate.putdata([255 if min(r, g, b) >= 225 and max(r, g, b) - min(r, g, b) <= 28
                       else 0 for r, g, b in pixels(rgb)])
    if close_path:
        # This only seals the original sticker trim in the segmentation mask;
        # it never paints a line onto the character or invents missing artwork.
        ImageDraw.Draw(candidate).line([tuple(p) for p in close_path], fill=0, width=2)
    # Flood-fill all edge-connected white components with a distinct marker.
    points = [(x, y) for x in range(width) for y in (0, height - 1)]
    points += [(x, y) for y in range(height) for x in (0, width - 1)]
    for point in points:
        if candidate.getpixel(point) == 255:
            ImageDraw.floodfill(candidate, point, 128, thresh=0)
    outside = candidate.point(lambda p: 255 if p == 128 else 0)
    border = outside.filter(ImageFilter.MaxFilter(3))
    alpha_pixels = []
    colors = []
    for color, removed, nearby in zip(pixels(rgb), pixels(outside), pixels(border)):
        if removed:
            a = 0
        elif nearby:
            # Remove a white matte from the antialiased outermost edge.
            a = min(255, max(0, round((255 - min(color)) * 255 / 64)))
        else:
            a = 255
        if 0 < a < 255:
            color = tuple(max(0, min(255, round((c - (255 - a)) * 255 / a))) for c in color)
        colors.append((*color, a))
        alpha_pixels.append(a)
    result = Image.new('RGBA', rgb.size)
    result.putdata(colors)
    result.save(target, optimize=True)
    print(f'{source.name} -> {target.name}: {sum(a == 0 for a in alpha_pixels) / len(alpha_pixels):.1%} transparent')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rebuild', action='store_true', help='Regenerate PNGs from original JPGs')
    args = parser.parse_args()
    trims = json.loads((ROOT / 'source_trims.json').read_text(encoding='utf-8'))
    assets = ROOT / 'assets'
    assets.mkdir(exist_ok=True)
    for number in range(1, 15):
        target = assets / f'kurumi_{number:02d}.png'
        if target.exists() and not args.rebuild:
            print(f'Preserving existing {target.name}')
            continue
        source = ROOT / f'kurumi_{number:02d}.jpg'
        trim = trims.get(source.name, {})
        close_path = trim.get('close_path', ()) if trim.get('sha256') == hashlib.sha256(source.read_bytes()).hexdigest() else ()
        cutout(source, target, close_path)
    with Image.open(assets / 'kurumi_10.png') as icon:
        icon.save(assets / 'kurumi.ico', sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (256, 256)])


if __name__ == '__main__':
    main()
