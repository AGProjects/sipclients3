"""
sipclient.video — on-screen video display for sip-session3 calls.

VideoWindow spawns a separate Python subprocess for each video stream
that owns its own Tk main thread.  This sidesteps macOS' requirement
that NSWindow be created on the process main thread — sip-session3's
own main thread is busy running the Twisted reactor, so Tk can't
live there.

Public surface
--------------
    window = VideoWindow(title='Remote video')
    window.start()
    renderer = FrameBufferVideoRenderer(window.frame_handler)
    renderer.producer = video_stream.producer
    ...
    window.close()

frame_handler is safe to call from any thread; it drops on a full
pipe so the pjsip media worker is never blocked.

Pixel format
------------
FrameBufferVideoRenderer delivers ARGB byte order in memory on
Darwin (see deps/patches/2.17/42_pjmedia_argb_format.patch in
python3-sipsimple) and BGRA on Linux / other platforms.  The
subprocess renderer (_video_window_proc) picks the R,G,B channels
with a platform-aware index, so both layouts produce correct
colours.
"""

from __future__ import annotations

import json
import struct
import subprocess
import sys
import threading
from typing import Optional


MSG_FRAME = 0x00
MSG_STATS = 0x01
FRAME_HEADER_FMT = '<III'   # width, height, byte_length
STATS_HEADER_FMT = '<I'     # length


class VideoWindow:
    """
    Out-of-process Tk video display.

    Spawns a child Python that imports sipclient._video_window_proc
    and reads framed pixel buffers from its stdin.  Frames are
    forwarded by frame_handler() over the pipe.

    Frames are written from a small dispatcher thread so the pjsip
    media worker isn't blocked by pipe back-pressure.  If the
    dispatcher's internal queue fills up (Tk too slow), frames are
    dropped at frame_handler() time.
    """

    def __init__(self, title: str = 'SIP Video', max_queue: int = 2) -> None:
        self.title = title
        self._proc: Optional[subprocess.Popen] = None
        self._send_lock = threading.Lock()
        # Single-frame "queue" via condition + slot so we always send
        # the freshest frame and drop the older one if we're behind.
        # Avoids unbounded backlog when Tk's renderer can't keep up.
        self._latest_lock = threading.Lock()
        self._latest_frame = None  # (width, height, bytes)
        self._frame_available = threading.Event()
        self._stop_event = threading.Event()
        self._sender_thread: Optional[threading.Thread] = None

    # -- public surface ----------------------------------------------------

    def start(self) -> None:
        """Spawn the subprocess and the dispatcher thread."""
        if self._proc is not None and self._proc.poll() is None:
            return
        try:
            self._proc = subprocess.Popen(
                [sys.executable, '-u', '-m', 'sipclient._video_window_proc', self.title],
                stdin=subprocess.PIPE,
                # Inherit stdout/stderr so render errors surface in the
                # parent's terminal — handy when debugging Tk import
                # failures or missing pillow.
            )
        except Exception as exc:
            print(f'[VideoWindow] could not spawn subprocess: {exc}')
            self._proc = None
            return

        self._stop_event.clear()
        self._sender_thread = threading.Thread(
            target=self._sender_loop, name='VideoWindow-sender', daemon=True,
        )
        self._sender_thread.start()

    def close(self) -> None:
        """Tell the dispatcher to stop and the subprocess to exit."""
        self._stop_event.set()
        self._frame_available.set()  # wake the sender
        if self._sender_thread is not None:
            self._sender_thread.join(timeout=2.0)
            self._sender_thread = None
        if self._proc is not None:
            try:
                if self._proc.stdin is not None:
                    self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=2.0)
            except Exception:
                try:
                    self._proc.terminate()
                except Exception:
                    pass
            self._proc = None

    def is_alive(self) -> bool:
        """
        True if the display subprocess is still running.  Returns
        False once the user closes the window with the mouse / the
        WM kills it, so callers can detect "gone" without trying to
        push another frame first.
        """
        return self._proc is not None and self._proc.poll() is None

    def frame_handler(self, frame) -> None:
        """
        Pass as the frame_handler argument to
        FrameBufferVideoRenderer(frame_handler=...).

        Replaces any previous unsent frame so we never lag — newest
        frame wins.  Safe from any thread.
        """
        if self._proc is None or self._proc.poll() is not None:
            return
        with self._latest_lock:
            self._latest_frame = (frame.width, frame.height, frame.data)
            self._frame_available.set()

    def update_stats(self, stats: dict) -> None:
        """
        Push a stats snapshot to the window.  The HUD overlays the
        most recent set on every rendered frame.  Common keys:
        codec, rtt_ms, packet_loss, bandwidth_kbps, fps, resolution.
        Anything in stats['extra'] (dict) is rendered verbatim.
        Safe from any thread; sent immediately (no batching).
        """
        if self._proc is None or self._proc.poll() is not None:
            return
        stdin = self._proc.stdin
        if stdin is None:
            return
        try:
            payload = json.dumps(stats or {}).encode('utf-8')
            header = bytes([MSG_STATS]) + struct.pack(STATS_HEADER_FMT, len(payload))
            # Protect stdin writes — sender thread also writes here.
            with self._send_lock:
                stdin.write(header)
                stdin.write(payload)
                stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    # -- internals ---------------------------------------------------------

    def _sender_loop(self) -> None:
        """
        Block until a frame is available, snapshot+clear the slot,
        write to the subprocess's stdin.  If the pipe is broken the
        subprocess died and we stop.
        """
        proc = self._proc
        if proc is None or proc.stdin is None:
            return
        stdin = proc.stdin
        while not self._stop_event.is_set():
            if not self._frame_available.wait(timeout=0.25):
                continue
            with self._latest_lock:
                self._frame_available.clear()
                frame = self._latest_frame
                self._latest_frame = None
            if frame is None:
                continue
            width, height, data = frame
            try:
                header = bytes([MSG_FRAME]) + struct.pack(
                    FRAME_HEADER_FMT, width, height, len(data),
                )
                with self._send_lock:
                    stdin.write(header)
                    stdin.write(data)
                    stdin.flush()
            except (BrokenPipeError, OSError):
                # Subprocess gone — stop trying.
                break


# Standalone smoke test — moving gradient at 30fps for 5 seconds.
if __name__ == '__main__':
    import time
    import numpy as np

    class _FakeFrame:
        def __init__(self, data, width, height):
            self.data = data
            self.width = width
            self.height = height

    win = VideoWindow(title='Self-test')
    win.start()
    w, h = 640, 480
    for i in range(150):
        argb = np.zeros((h, w, 4), dtype=np.uint8)
        argb[:, :, 0] = 0xFF
        argb[:, :, 1] = (i * 5) & 0xFF
        argb[:, :, 2] = ((i + 80) * 5) & 0xFF
        argb[:, :, 3] = ((i + 160) * 5) & 0xFF
        win.frame_handler(_FakeFrame(argb.tobytes(), w, h))
        time.sleep(1 / 30)
    win.close()
