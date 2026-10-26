"""
Subprocess entry point for sipclient.video.VideoWindow.

Runs in its own Python process so Tk owns the main thread (macOS
requires AppKit/NSWindow to be created on the *process* main
thread).  Reads framed pixel buffers and stat snapshots from stdin,
displays via Tk + PIL with a stats HUD overlaid on each frame.

Wire protocol (little-endian)
-----------------------------
Each message starts with a 1-byte type discriminator:

    type = 0x00  →  FRAME
        uint32 width
        uint32 height
        uint32 byte_length
        bytes  data        # 4 bytes per pixel, byte_length == width*height*4
                           # Byte order is platform-dependent: ARGB on Darwin
                           # (pjmedia is patched to deliver ARGB there), BGRA
                           # on Linux/other.  We pick the RGB channels with a
                           # platform-aware index — see RGB_CHANNEL_ORDER.
                           # (may be over-allocated; see overflow handling)

    type = 0x01  →  STATS
        uint32 byte_length
        bytes  payload     # UTF-8 JSON dict, free-form keys

Parent closes stdin to request shutdown; we also exit when the user
closes the window.

Module entry point
------------------
    python3 -m sipclient._video_window_proc <window_title>
"""

from __future__ import annotations

import json
import os
import struct
import sys


# Pixel byte order in the frame buffer the parent sends us.  On Darwin
# pjmedia is patched (deps/patches/2.17/42_pjmedia_argb_format.patch in
# python3-sipsimple) to deliver ARGB in memory; on Linux/other platforms
# pjmedia's native format is BGRA.  We therefore pick R,G,B at different
# offsets to feed PIL a correctly coloured 24-bit RGB image.
#
#   ARGB memory layout (Darwin):  byte0=A byte1=R byte2=G byte3=B → [1,2,3]
#   BGRA memory layout (Linux):   byte0=B byte1=G byte2=R byte3=A → [2,1,0]
if sys.platform == 'darwin':
    RGB_CHANNEL_ORDER = [1, 2, 3]
else:
    RGB_CHANNEL_ORDER = [2, 1, 0]


MSG_FRAME = 0x00
MSG_STATS = 0x01

FRAME_HEADER_FMT = '<III'   # width, height, length
STATS_HEADER_FMT = '<I'     # length
FRAME_HEADER_SIZE = struct.calcsize(FRAME_HEADER_FMT)
STATS_HEADER_SIZE = struct.calcsize(STATS_HEADER_FMT)

# Cap the rendered display so the window never blows past a useful
# size even when the codec hands us a 1088x1088 / 1920x1080 picture.
# Aspect ratio is preserved — frames bigger than this are scaled
# down via PIL.Image.thumbnail before being handed to Tk.
MAX_DISPLAY_W = 480
MAX_DISPLAY_H = 360

# HUD layout
HUD_PADDING = 6
HUD_LINE_HEIGHT = 14
HUD_FONT_SIZE = 11
HUD_BG_ALPHA = 170          # 0=transparent, 255=opaque


def _read_exact(fd: int, n: int) -> bytes:
    """Read exactly n bytes or return less on EOF."""
    buf = bytearray()
    while len(buf) < n:
        chunk = os.read(fd, n - len(buf))
        if not chunk:
            return bytes(buf)
        buf.extend(chunk)
    return bytes(buf)


def _derive_dims(px: int, tag_w: int, tag_h: int):
    """
    Fall-back dimension resolver for the rare case where the parent
    sends a frame buffer that's *smaller* than the tagged display
    size.  Common case (buffer over-allocated) is handled directly
    in the renderer.

    Returns (w, h) that multiply to px, or None if no plausible
    interpretation exists.  Prefers known video resolutions over
    square interpretations when both factor.
    """
    if px <= 0:
        return None
    common_resolutions = [
        (1920, 1088), (1920, 1080),
        (1280, 720),  (1280, 736),
        (1280, 960),  (960, 544),
        (1088, 1088), (854, 480),
        (640, 480),   (480, 640),
        (720, 1280),  (1080, 1920),
    ]
    for w, h in common_resolutions:
        if w * h == px:
            return (w, h)
    side = int(px ** 0.5)
    if side * side == px:
        return (side, side)
    if tag_h > 0:
        aspect = tag_w / tag_h
        h_guess = int((px / aspect) ** 0.5)
        for h in (h_guess - 16, h_guess, h_guess + 16, h_guess + 32):
            h_aligned = max(16, (h // 16) * 16)
            if h_aligned <= 0:
                continue
            if px % h_aligned == 0:
                w = px // h_aligned
                if w > 0 and w % 16 == 0:
                    return (w, h_aligned)
    return None


def _format_stats(stats: dict) -> list[str]:
    """Render the stats dict into a short list of human-readable lines."""
    if not stats:
        return []
    lines = []
    codec = stats.get('codec')
    if codec:
        lines.append(f'codec: {codec}')
    rtt_ms = stats.get('rtt_ms')
    if rtt_ms is not None:
        lines.append(f'rtt: {rtt_ms:.0f} ms')
    loss = stats.get('packet_loss')
    if loss is not None:
        # Treat as a 0..1 ratio if <= 1, otherwise as a percentage.
        if loss <= 1.0:
            lines.append(f'loss: {loss * 100:.2f}%')
        else:
            lines.append(f'loss: {loss:.2f}%')
    bw = stats.get('bandwidth_kbps')
    if bw is not None:
        if bw >= 1024:
            lines.append(f'bw: {bw / 1024:.2f} Mbps')
        else:
            lines.append(f'bw: {bw:.0f} kbps')
    fps = stats.get('fps')
    if fps is not None:
        lines.append(f'fps: {fps:.1f}')
    res = stats.get('resolution')
    if res:
        lines.append(f'res: {res}')
    # Anything else the caller wanted to surface — opt-in via "extra"
    # so we don't dump random internal keys onto the HUD.
    extra = stats.get('extra')
    if isinstance(extra, dict):
        for k, v in extra.items():
            lines.append(f'{k}: {v}')
    return lines


def main(argv: list[str]) -> int:
    title = argv[1] if len(argv) > 1 else 'SIP Video'

    try:
        import tkinter as tk
        from PIL import Image, ImageTk, ImageDraw, ImageFont
        import numpy as np
    except ImportError as exc:
        sys.stderr.write(f'[VideoWindow] missing dependency: {exc}\n')
        return 2

    # Try to find a fixed-width font — falls back to default bitmap font.
    try:
        hud_font = ImageFont.truetype('Menlo.ttc', HUD_FONT_SIZE)
    except OSError:
        try:
            hud_font = ImageFont.truetype('DejaVuSansMono.ttf', HUD_FONT_SIZE)
        except OSError:
            hud_font = ImageFont.load_default()

    in_fd = sys.stdin.buffer.fileno()

    root = tk.Tk()
    root.title(title)
    root.geometry('480x360')
    # Black background = letterbox bars when the user stretches the
    # window to a different aspect ratio than the source frame.
    label = tk.Label(root, bg='black')
    label.pack(fill='both', expand=True)
    state = {
        'image_ref': None,
        'closed': False,
        'stats': {},
        # Latest source RGBA frame (un-thumbnailed, pre-HUD) so a
        # <Configure> resize event can re-render at the new label
        # size without waiting for the next wire frame.
        'src_image': None,
        # Current label size in pixels — driven by <Configure>.
        'label_size': (480, 360),
    }

    def on_user_close() -> None:
        state['closed'] = True
    root.protocol('WM_DELETE_WINDOW', on_user_close)

    def _render(src_img: 'Image.Image') -> None:
        """Scale src_img to fit the current label size, overlay HUD, push to Tk."""
        lw, lh = state['label_size']
        if lw <= 1 or lh <= 1:
            return
        scaled = src_img.copy()
        scaled.thumbnail((lw, lh), Image.LANCZOS)

        # Stats HUD on top of the scaled image.  Source is RGB, so
        # draw with an RGB Draw context (no per-pixel alpha).
        lines = _format_stats(state['stats'])
        if lines:
            draw = ImageDraw.Draw(scaled, 'RGB')
            widths = []
            for line in lines:
                try:
                    bbox = draw.textbbox((0, 0), line, font=hud_font)
                    widths.append(bbox[2] - bbox[0])
                except AttributeError:
                    widths.append(len(line) * 7)
            box_w = max(widths) + 2 * HUD_PADDING
            box_h = HUD_LINE_HEIGHT * len(lines) + 2 * HUD_PADDING
            # RGB draw context — fill colors are 3-tuples.  Solid
            # dark block keeps text readable over varied scenes;
            # the HUD_BG_ALPHA constant is kept for backward
            # compatibility but no longer applied here.
            draw.rectangle(
                [(0, 0), (box_w, box_h)],
                fill=(0, 0, 0),
            )
            for i, line in enumerate(lines):
                draw.text(
                    (HUD_PADDING, HUD_PADDING + i * HUD_LINE_HEIGHT),
                    line, fill=(255, 255, 255), font=hud_font,
                )

        photo = ImageTk.PhotoImage(scaled)
        state['image_ref'] = photo  # keep alive
        label.config(image=photo)

    def _on_label_resize(event) -> None:
        state['label_size'] = (event.width, event.height)
        src = state.get('src_image')
        if src is not None:
            _render(src)

    label.bind('<Configure>', _on_label_resize)

    def read_next_message():
        """Return ('frame', (w,h,bytes)) | ('stats', dict) | None on EOF."""
        type_byte = os.read(in_fd, 1)
        if not type_byte:
            return None
        t = type_byte[0]
        if t == MSG_FRAME:
            header = _read_exact(in_fd, FRAME_HEADER_SIZE)
            if not header:
                return None
            width, height, length = struct.unpack(FRAME_HEADER_FMT, header)
            data = _read_exact(in_fd, length)
            if len(data) != length:
                return None
            return ('frame', (width, height, data))
        if t == MSG_STATS:
            header = _read_exact(in_fd, STATS_HEADER_SIZE)
            if not header:
                return None
            (length,) = struct.unpack(STATS_HEADER_FMT, header)
            payload = _read_exact(in_fd, length)
            if len(payload) != length:
                return None
            try:
                return ('stats', json.loads(payload.decode('utf-8')))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                sys.stderr.write(f'[VideoWindow] bad stats msg: {exc}\n')
                return ('stats', {})
        # Unknown type — protocol desync, bail out so the parent
        # restarts us rather than silently corrupting frames.
        sys.stderr.write(f'[VideoWindow] unknown msg type 0x{t:02x}\n')
        return None

    def pump() -> None:
        if state['closed']:
            root.destroy()
            return

        msg = None
        try:
            msg = read_next_message()
        except OSError:
            state['closed'] = True
            root.after(0, pump)
            return

        if msg is None:
            state['closed'] = True
            root.after(0, pump)
            return

        kind, payload = msg
        if kind == 'stats':
            state['stats'] = payload or {}
            # Re-render the current frame so the HUD updates even
            # when no new frame has arrived since the last stats push.
            src = state.get('src_image')
            if src is not None:
                _render(src)
            root.after(1, pump)
            return

        # kind == 'frame'
        width, height, data = payload
        try:
            expected = width * height * 4
            got = len(data)
            if got > expected:
                data = data[:expected]
            elif got < expected:
                if got % 4 != 0:
                    raise ValueError(
                        f'short frame {got} bytes not 4-aligned '
                        f'(tag {width}x{height})'
                    )
                new_dims = _derive_dims(got // 4, width, height)
                if new_dims is None:
                    raise ValueError(
                        f'cannot fit {got} bytes into a sensible '
                        f'resolution (tag {width}x{height})'
                    )
                width, height = new_dims

            arr = (
                np.frombuffer(data, dtype=np.uint8)
                  .reshape((height, width, 4))
            )
            # Pick the R,G,B channels using the platform-aware order
            # (ARGB on Darwin, BGRA on Linux) and feed PIL as 24-bit
            # RGB.  We deliberately drop the alpha channel: pjmedia's
            # framebuffer often leaves it zeroed, and using RGBA mode
            # then composites the frame fully transparent against the
            # Tk label's black bg — i.e. you see a black window even
            # when real pixels arrive.
            rgb = arr[:, :, RGB_CHANNEL_ORDER]
            img = Image.frombuffer('RGB', (width, height), rgb.tobytes(),
                                   'raw', 'RGB', 0, 1)
            state['src_image'] = img
            _render(img)
        except Exception as exc:
            sys.stderr.write(f'[VideoWindow] render error: {exc}\n')

        root.after(1, pump)

    root.after(1, pump)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
