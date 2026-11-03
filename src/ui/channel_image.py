"""Bounded, optional Twitch avatar composition; no persistent image store."""
import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import io
import time
import warnings
import aiohttp
from PIL import Image, ImageDraw, ImageOps
from .card_images import DEFAULT, avatar_url

BACKGROUND = (28, 30, 36)
SIZE = (720, 405)
DIAMETER = 240
MAX_INPUT = 2 * 1024 * 1024
MAX_PIXELS = 4 * 1024 * 1024
MAX_OUTPUT = 192 * 1024

@dataclass(frozen=True)
class ChannelImage:
    data: bytes


def compose(data):
    if len(data) > MAX_INPUT:raise ValueError('avatar_size')
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data), formats=['PNG','JPEG','WEBP','GIF']) as image:
            if (max(image.size) > 2048 or image.width * image.height > MAX_PIXELS
                    or getattr(image,'is_animated',False)):raise ValueError('avatar_dimensions')
            image.load()
            face = ImageOps.fit(ImageOps.exif_transpose(image).convert('RGBA'), (DIAMETER*2,DIAMETER*2),
                                method=Image.Resampling.LANCZOS, centering=(.5,.5))
    # Render the 720x405 composition at 2x for exact half-pixel centering and soft edges.
    mask = Image.new('L',(DIAMETER*4,DIAMETER*4))
    ImageDraw.Draw(mask).ellipse((0,0,DIAMETER*4-1,DIAMETER*4-1),fill=255)
    mask = mask.resize((DIAMETER*2,DIAMETER*2),Image.Resampling.LANCZOS)
    from PIL import ImageChops
    mask = ImageChops.multiply(mask,face.getchannel('A'))
    canvas = Image.new('RGB',(SIZE[0]*2,SIZE[1]*2),BACKGROUND)
    # Symmetric center even on the odd 405-pixel final canvas.
    canvas.paste(face,(SIZE[0]-DIAMETER,SIZE[1]-DIAMETER),mask)
    canvas=canvas.resize(SIZE,Image.Resampling.LANCZOS)
    out=io.BytesIO();canvas.save(out,format='PNG',compress_level=6)
    if out.tell()>MAX_OUTPUT:raise ValueError('avatar_output')
    return ChannelImage(out.getvalue())


class AvatarCache:
    """16 results; success 10 min, failure 1 min; one bounded decode at a time."""
    def __init__(self):
        self.entries=OrderedDict();self.lock=asyncio.Lock()

    async def get(self,http,url):
        url=avatar_url(url)
        if not url or http is None:return DEFAULT
        now=time.monotonic()
        for key,(expires,_) in list(self.entries.items()):
            if expires<=now:self.entries.pop(key)
        if url in self.entries:
            self.entries.move_to_end(url);return self.entries[url][1]
        # Do not build an unbounded queue of decoders or pending HTTP requests.
        if self.lock.locked():return DEFAULT
        async with self.lock:
            value=DEFAULT
            try:
                async with http.get(url,allow_redirects=False,auto_decompress=False,
                                    timeout=aiohttp.ClientTimeout(total=3)) as response:
                    if response.status!=200 or response.headers.get('Content-Encoding','identity')!='identity':raise ValueError('avatar_response')
                    if response.content_length is not None and response.content_length>MAX_INPUT:raise ValueError('avatar_size')
                    raw=bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        if len(raw)+len(chunk)>MAX_INPUT:raise ValueError('avatar_size')
                        raw.extend(chunk)
                task=asyncio.create_task(asyncio.to_thread(compose,bytes(raw)))
                try:value=await asyncio.shield(task)
                except asyncio.CancelledError:
                    # A cancelled waiter must not release the decoder slot early.
                    while not task.done():
                        try:await asyncio.shield(task)
                        except asyncio.CancelledError:continue
                        except Exception:break
                    if task.done() and not task.cancelled():task.exception()
                    raise
            except Exception:
                pass  # Optional image only; no URLs or decoder details in logs.
            self.entries[url]=(time.monotonic()+(60 if value==DEFAULT else 600),value)
            while len(self.entries)>16:self.entries.popitem(last=False)
            return value


async def channel_image(context,session,url):
    if getattr(session,'avatar_source',None)==url and hasattr(session,'avatar_image'):
        return session.avatar_image
    app=context.application
    cache=app.bot_data.setdefault('tdb_avatar_images',AvatarCache())
    image=await cache.get(app.bot_data.get('aiohttp'),url)
    session.avatar_source,session.avatar_image=url,image
    return image
