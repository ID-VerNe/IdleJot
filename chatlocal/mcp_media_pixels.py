"""Image pixel decoding and frame sampling (issue #11, section PC).

Split out of ``mcp_chat_media.py``: ``pixels`` turns bounded raw bytes into the
delivered JPEG frames. One frame by default -- every extra frame of a group-chat
animation is repeated token cost, so ``frames='all'`` is opt-in.
"""
import base64
import io

from mcp import types
from PIL import Image, UnidentifiedImageError

from . import reply_timing as timing
from .mcp_media_transport import MAX_BYTES


@timing.stage('image.decode_encode')
def pixels(raw,*,validate_only=False,frames='one'):
    """解码为交付像素；默认只取一帧，frames='all' 才给 3 个采样帧。

    群聊里绝大多数动图只需要看一帧就能理解；每多一帧都是重复的 token 成本，
    所以默认帧数从 3 降到 1，只有明确要求看动作时才补采样。
    """
    timing.note(image_bytes=len(raw) if raw else 0)
    if not raw or len(raw)>MAX_BYTES:
        timing.note(image_error='empty_or_byte_limit')
        raise ValueError('图片为空或超过单张 6 MiB 上限。')
    try:
        with Image.open(io.BytesIO(raw)) as im:
            if im.format not in ('PNG','JPEG','GIF','WEBP'):raise ValueError('暂不支持这种图片格式。')
            total = getattr(im, 'n_frames', 1)
            timing.note(image_dimensions=list(im.size), total_frames=total)
            if total>300 or im.width*im.height*total>80000000 or im.width*im.height>12000000:
                timing.note(image_error='dimensions_or_frame_limit')
                raise ValueError('图片尺寸或动画帧数过大，未处理。')
            size, fmt = list(im.size), im.format
            im.verify()
        if validate_only:return
        if frames=='all':
            indices = sorted({0, total//2, total-1})
        else:
            indices = [total//2 if total>1 else 0]
        images, kept = [], []
        with Image.open(io.BytesIO(raw)) as im:
            for index in indices:
                im.seek(index)
                frame = im.convert('RGBA');frame.thumbnail((1280,1280))
                background = Image.new('RGB', frame.size, 'white');background.paste(frame, mask=frame.getchannel('A'))
                out = io.BytesIO();background.save(out, 'JPEG', quality=82)
                kept.append(index)
                images.append(types.ImageContent(type='image', mimeType='image/jpeg', data=base64.b64encode(out.getvalue()).decode('ascii')))
        timing.note(image_dimensions=size, total_frames=total, returned_frames=len(images),
                    returned_base64_chars=sum(len(frame.data) for frame in images))
        return dict(format=fmt, dimensions=size, total_frames=total, frame_indices=kept), images
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError, EOFError):
        raise ValueError('图片损坏或未完整下载；不会影响持续聊天和普通工具。') from None
