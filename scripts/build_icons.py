"""Regenerate matching favicon/Apple icons with Pillow (development only)."""
from pathlib import Path
from PIL import Image, ImageDraw


def main():
    root = Path(__file__).resolve().parents[1] / 'static'
    scale = 8
    image = Image.new('RGBA', (64 * scale, 64 * scale))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, 64 * scale - 1, 64 * scale - 1), radius=17 * scale, fill='#7dcaff')
    for points in [[(32, 13), (9, 25), (32, 37), (55, 25)],
                   [(9, 33), (32, 45), (55, 33), (55, 40), (32, 52), (9, 40)]]:
        draw.polygon([(x * scale, y * scale) for x, y in points], fill='#172b43')
    image.save(root / 'favicon.ico', sizes=[(16, 16), (32, 32), (48, 48)])
    apple = Image.new('RGBA', image.size, '#7dcaff')
    apple.alpha_composite(image)
    apple.convert('RGB').resize((180, 180), Image.Resampling.LANCZOS).save(root / 'apple-touch-icon.png')


if __name__ == '__main__':
    main()
