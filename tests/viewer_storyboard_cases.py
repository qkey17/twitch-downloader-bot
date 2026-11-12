"""Exercise native touch input and decoded inline preview images in Chromium."""
import random

from PIL import Image
from src.twitch_api.storyboard import jpeg


def preview_fixture(count):
    size = (220, 124)
    rng = random.Random(79)
    noise = Image.frombytes('RGB', size, rng.randbytes(size[0]*size[1]*3))
    images = [jpeg(Image.blend(Image.new('RGB', size, color), noise, .45), None, 128*1024, quality=92)
              for color in ('red', 'green', 'blue')]
    cover = jpeg(Image.new('RGB', (1280, 720), 'darkgreen'), None, 128*1024, quality=84)
    return dict(duration_seconds=count, thumbnail_preview_url=(
        'https://static-cdn.jtvnw.net/cf_vods/test/thumb.jpg'),
        preview_assets=dict(cover=cover, aspect=list(size),
                            frames=list(zip((0, count // 2, count - 1), images))))
