#!/usr/bin/env python3
"""notification_overlay.py — animated notification card in the OverlayGen style.

Renders a short transparent notification that pops up from the bottom of the
frame with a soft chime, holds, then drops back out. Same deliverables as
render_overlay.py:

  {out}_alpha.mov   ProRes 4444 with alpha + PCM audio   (primary)
  {out}_alpha.webm  VP9 with alpha + Opus audio          (lightweight)
  {out}_green.mp4   H.264 over pure green + AAC          (optional, --green)

Usage:
  python notification_overlay.py                 # 3s, default text + chime
  python notification_overlay.py --preview       # dump a PNG instead
  python notification_overlay.py --silent --green
  python notification_overlay.py --text "First line|Second line"
"""

import argparse
import math
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("Pillow is required:  python3 -m pip install Pillow")

from render_overlay import (CANVAS_W, CANVAS_H, OUT_FPS, SS, BAR_BG,
                            BAR_BORDER, GREEN, glow_text, get_anton_path,
                            _first_existing, BIG_FALLBACKS, finish_encoder)

DEFAULT_TEXT = "Your stream shouldn't|be stealing your frames"
SLIDE_IN = 0.35     # pop-up duration (with overshoot)
SLIDE_OUT = 0.30    # drop-out duration


def ease_out_back(p, c1=1.0):
    """Cubic ease-out with a slight overshoot."""
    c3 = c1 + 1
    return 1 + c3 * (p - 1) ** 3 + c1 * (p - 1) ** 2


# ------------------------------------------------------------------- card

def build_card(lines):
    """Render the notification card (2x supersampled, downscaled to 1x)."""
    s = SS
    font_path = get_anton_path() or _first_existing(BIG_FALLBACKS)
    if font_path is None:
        sys.exit("No usable font found (Anton download failed and no "
                 "system fallback exists).")
    font = ImageFont.truetype(font_path, 34 * s)

    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    lines = [t.upper() for t in lines]
    text_w = max(probe.textlength(t, font=font) for t in lines)
    pad_x, pad_y, line_h = 38 * s, 26 * s, 44 * s
    w = int(text_w + 2 * pad_x)
    h = int(2 * pad_y + line_h * len(lines))

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))

    def layer(draw_fn):
        lay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw_fn(ImageDraw.Draw(lay))
        return lay

    img.alpha_composite(layer(lambda d: d.rounded_rectangle(
        (0, 0, w - 1, h - 1), radius=8 * s, fill=BAR_BG)))
    img.alpha_composite(layer(lambda d: d.rounded_rectangle(
        (0, 0, w - 1, h - 1), radius=8 * s,
        outline=BAR_BORDER, width=1 * s)))

    def draw_lines(dd):
        for i, text in enumerate(lines):
            dd.text((w / 2, pad_y + line_h * (i + 0.5)), text,
                    font=font, fill=255, anchor="mm")

    glow, crisp = glow_text((w, h), draw_lines, blur=6 * s, alpha=0.30)
    img.alpha_composite(glow)
    img.alpha_composite(crisp)

    return (img.convert("RGBa")
            .resize((w // s, h // s), Image.LANCZOS)
            .convert("RGBA"))


# ------------------------------------------------------------------ sound

def synth_chime(path, total_s, land_t, sr=48000):
    """A soft landing thump plus a gentle two-note chime (D5 -> A5)."""
    n = int(total_s * sr)
    buf = [0.0] * n

    def add_tone(t0, freq, dur, amp, decay):
        start = int(t0 * sr)
        for i in range(int(dur * sr)):
            j = start + i
            if not 0 <= j < n:
                continue
            t = i / sr
            env = min(1.0, t / 0.005) * math.exp(-t * decay)
            sample = (math.sin(2 * math.pi * freq * t)
                      + 0.30 * math.sin(4 * math.pi * freq * t)
                      * math.exp(-t * decay * 1.5))
            buf[j] += amp * env * sample

    add_tone(land_t - 0.05, 220.0, 0.15, 0.16, 30)   # soft thump on landing
    add_tone(land_t, 587.33, 0.55, 0.22, 9)          # D5
    add_tone(land_t + 0.12, 880.00, 0.80, 0.20, 7)   # A5

    peak = max(abs(v) for v in buf) or 1.0
    scale = min(1.0, 0.8 / peak)
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"".join(
            struct.pack("<h", int(v * scale * 32767)) for v in buf))


# ----------------------------------------------------------------- ffmpeg

def start_encoder(ffmpeg, in_pix_fmt, wav, audio_args, out_args, out_path,
                  log_dir):
    log = open(Path(log_dir) / (Path(out_path).name + ".log"), "wb")
    cmd = [ffmpeg, "-y",
           "-f", "rawvideo", "-pix_fmt", in_pix_fmt,
           "-video_size", f"{CANVAS_W}x{CANVAS_H}",
           "-framerate", str(OUT_FPS), "-i", "-"]
    if wav is not None:
        cmd += ["-i", str(wav), "-map", "0:v", "-map", "1:a",
                *audio_args, "-shortest"]
    cmd += [*out_args, str(out_path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=log,
                            stderr=subprocess.STDOUT)
    proc._log_file = log
    proc._out_path = str(out_path)
    return proc


# ------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        description="Render an animated notification overlay with sound.")
    ap.add_argument("--text", default=DEFAULT_TEXT,
                    help="notification text; '|' separates lines "
                         "(default: %(default)s)")
    ap.add_argument("--out", default=None,
                    help="output basename (default: output/notification)")
    ap.add_argument("--duration", type=float, default=3.0,
                    help="clip length in seconds (default 3.0)")
    ap.add_argument("--green", action="store_true",
                    help="also render an H.264 green-screen mp4")
    ap.add_argument("--silent", action="store_true",
                    help="skip the sound effect")
    ap.add_argument("--preview", action="store_true",
                    help="dump a preview PNG instead of rendering video")
    args = ap.parse_args()

    if args.duration <= SLIDE_IN + SLIDE_OUT:
        sys.exit(f"--duration must be longer than "
                 f"{SLIDE_IN + SLIDE_OUT:.2f}s")

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None and not args.preview:
        sys.exit("ffmpeg not found on PATH. Install it (e.g. `brew install "
                 "ffmpeg` on macOS, or download from ffmpeg.org on Windows "
                 "and add it to PATH) and re-run.")

    if args.out is None:
        args.out = str(Path(__file__).resolve().parent
                       / "output" / "notification")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)

    lines = [t.strip() for t in args.text.split("|") if t.strip()]
    card = build_card(lines)
    card_x = (CANVAS_W - card.width) // 2
    y_final = round(CANVAS_H * 0.92 - card.height)
    y_off = CANVAS_H + 2

    def card_y(t):
        if t < SLIDE_IN:
            return round(y_off + (y_final - y_off)
                         * ease_out_back(t / SLIDE_IN))
        if t > args.duration - SLIDE_OUT:
            p = (t - (args.duration - SLIDE_OUT)) / SLIDE_OUT
            return round(y_final + (y_off - y_final) * p ** 3)
        return y_final

    def compose(t):
        canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
        y = card_y(t)
        if y < CANVAS_H:
            canvas.alpha_composite(card, (card_x, y))
        return canvas, y

    if args.preview:
        frame, _ = compose(args.duration / 2)
        path = f"{args.out}_preview.png"
        frame.save(path)
        print(f"wrote {path}")
        return

    n_frames = round(args.duration * OUT_FPS)
    log_dir = tempfile.mkdtemp(prefix="notification_ffmpeg_")
    wav = None
    if not args.silent:
        wav = Path(log_dir) / "chime.wav"
        synth_chime(wav, args.duration, SLIDE_IN)

    title = "Notification overlay"
    encoders = [
        ("rgba", start_encoder(
            ffmpeg, "rgba", wav, ["-c:a", "pcm_s16le"],
            ["-c:v", "prores_ks", "-profile:v", "4444",
             "-pix_fmt", "yuva444p10le", "-vendor", "apl0",
             "-metadata", f"title={title}"],
            f"{args.out}_alpha.mov", log_dir)),
        ("rgba", start_encoder(
            ffmpeg, "rgba", wav, ["-c:a", "libopus", "-b:a", "96k"],
            ["-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
             "-crf", "28", "-b:v", "0", "-row-mt", "1",
             "-cpu-used", "4", "-auto-alt-ref", "0",
             "-metadata", f"title={title}"],
            f"{args.out}_alpha.webm", log_dir)),
    ]
    if args.green:
        encoders.append(("green", start_encoder(
            ffmpeg, "rgb24", wav, ["-c:a", "aac", "-b:a", "128k"],
            ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
             "-preset", "medium", "-movflags", "+faststart",
             "-metadata", f"title={title}"],
            f"{args.out}_green.mp4", log_dir)))

    green_bg = Image.new("RGBA", (CANVAS_W, CANVAS_H), (*GREEN, 255))
    rgba_bytes = green_bytes = None
    last_y = None
    print(f"Rendering {n_frames} frames -> "
          + ", ".join(p._out_path for _, p in encoders))
    try:
        for f in range(n_frames):
            frame, y = compose(f / OUT_FPS)
            if y != last_y:
                rgba_bytes = frame.tobytes()
                if args.green:
                    green_bytes = Image.alpha_composite(
                        green_bg, frame).convert("RGB").tobytes()
                last_y = y
            for kind, proc in encoders:
                proc.stdin.write(rgba_bytes if kind == "rgba"
                                 else green_bytes)
    except BrokenPipeError:
        sys.exit("ffmpeg pipe closed unexpectedly — check the encoder "
                 f"logs in {log_dir}")
    for _, proc in encoders:
        finish_encoder(proc)
    for _, proc in encoders:
        print(f"  wrote {proc._out_path}")
    print("Done." + ("" if args.silent else "  (chime lands with the card)"))


if __name__ == "__main__":
    main()
