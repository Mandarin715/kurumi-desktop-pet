"""Produce a dark-background contact sheet for local visual inspection."""
from pathlib import Path
from PIL import Image, ImageDraw

root = Path(__file__).resolve().parent
# Regression samples in the light skirt fabric. Most used to be completely
# transparent because the background flood-fill entered the open bottom hem.
skirt_samples = {1: (500, 940), 2: (450, 980), 3: (500, 950), 4: (805, 935),
                 5: (380, 950), 6: (620, 940), 7: (470, 975), 8: (450, 980),
                 9: (755, 930), 10: (470, 965), 11: (430, 955), 12: (390, 960),
                 13: (400, 900), 14: (500, 950)}
sheet = Image.new('RGB', (1000, 1000), '#303b4d')
draw = ImageDraw.Draw(sheet)
for number in range(1, 15):
    tile = Image.open(root / 'assets' / f'kurumi_{number:02d}.png').convert('RGBA')
    assert tile.getpixel((0, 0))[3] == 0
    assert tile.getpixel(skirt_samples[number])[3] == 255, f'Skirt missing in pose {number}'
    assert tile.getchannel('A').crop((0, tile.height-10, tile.width, tile.height)).getbbox() is None
    tile.thumbnail((230, 220))
    x, y = ((number - 1) % 4) * 250, ((number - 1) // 4) * 250
    sheet.paste(tile, (x + (250 - tile.width) // 2, y + 10), tile)
    draw.text((x + 110, y + 232), f'{number:02d}', fill='white')
(root / 'build').mkdir(exist_ok=True)
sheet.save(root / 'build' / 'assets-preview.jpg')
