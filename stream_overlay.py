#!/usr/bin/env python3
"""stream_overlay.py — mock full stream overlay (transparent, 1080p).

Renders a 10-second full-frame streamer overlay package designed to sit over
gameplay footage (e.g. Valorant):
  - live chat column (left): ordinary viewer chatter with a natural burst of
    "stream is laggy / jittery / low quality" complaints in the middle
  - LIVE status chip with a ticking uptime clock and viewer count
  - latest-follower / follower-goal panel (bottom right)
No real names or identities — everything is generic. Same deliverables as
the other OverlayGen tools:

  {out}_alpha.mov   ProRes 4444 with alpha   (primary)
  {out}_alpha.webm  VP9 with alpha           (lightweight)
  {out}_green.mp4   H.264 over pure green    (optional, --green)

Usage:
  python stream_overlay.py [--out output/stream_chat] [--duration 10]
                           [--green] [--preview]
"""

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("Pillow is required:  python3 -m pip install Pillow")

from render_overlay import (CANVAS_W, CANVAS_H, OUT_FPS, SS, BAR_BORDER,
                            LABEL_GREY, GREEN, _first_existing,
                            LABEL_FALLBACKS, draw_tracked, tracked_width,
                            finish_encoder)
from notification_overlay import start_encoder

# ------------------------------------------------------------- chat script
# (time, username, message) — generic handles, no real identities.
# Complaints cluster mid-clip like a real lag event; negative times are
# already on screen when the clip starts.
CHAT_SCRIPT = [
    # scrollback — the panel is already full when the clip starts
    (-10.8, "spray_transfer", "are these servers 128 tick"),
    (-9.9, "one_tap_wonder", "day one of asking for a deathmatch arc"),
    (-9.0, "headshot_hopeful", "these lobbies are cracked today"),
    (-8.1, "dm_warmup", "the warmup dm was rough lol"),
    (-7.2, "vandal_enjoyer", "map pool feels weird this act"),
    (-6.3, "first_light_", "just got here whats the score"),
    (-5.4, "eco_frag", "anyone else grinding placements this week"),
    (-4.5, "wombo_kombo", "is this comp or unrated"),
    (-3.6, "quiet_riot7", "what sens do you play on"),
    (-2.7, "bean_water", "0.35 i think he said earlier"),
    (-1.8, "spike_planter", "how long till next map"),
    (-0.9, "mid_or_feed", "chat whats the record today"),
    # live portion — lag complaints build up between normal questions
    (0.8, "lurker_supreme", "how many games is that today"),
    (1.7, "pixel_drift", "is it just me or is the stream kinda laggy"),
    (2.5, "grape_juice", "yea its stuttering for me too"),
    (3.3, "mid_or_feed", "thought it was my internet lol"),
    (4.2, "fps_junkie", "quality keeps dropping to 480p for me"),
    (5.0, "sleepy_cat_", "so jittery rn"),
    (5.9, "no_scope_km", "refreshed and its still choppy"),
    (6.8, "taco_at_3am", "what sens for the operator"),
    (7.6, "clutch_or_kick", "stream lagging again lmaooo"),
    (8.5, "certified_wiffer", "F for the bitrate"),
]

# Twitch-style default username colors
PALETTE = [(255, 105, 100), (30, 144, 255), (154, 205, 50), (255, 105, 180),
           (0, 206, 209), (218, 165, 32), (160, 110, 255), (0, 220, 130),
           (255, 130, 80), (120, 170, 200)]

TEXT_COLOR = (239, 239, 241)   # Twitch chat text
PANEL_BG = (7, 9, 14, 90)      # translucent panel background (~35%)
ACCENT_RED = (255, 77, 77)
ACCENT_BLUE = (86, 157, 255)

# Layout (1x pixels) — left column: chip + chat; bottom right: follower goal
CHIP_POS = (24, 296)
PANEL_X, PANEL_W = 24, 370
PANEL_Y, PANEL_H = 340, 480
SIDE_W, SIDE_H = 330, 88
SIDE_POS = (CANVAS_W - 24 - SIDE_W, CANVAS_H - 24 - SIDE_H)
HEADER_H = 36
PAD = 14
GAP = 10
LINE_H = 21
FADE = 0.20                    # seconds for a new message to fade/slide in
UPTIME_START = 1 * 3600 + 21 * 60 + 37   # uptime clock at t=0
VIEWERS = 248
FOLLOWER_GOAL = (87, 100)
LATEST_FOLLOWER = "grape_juice"

REGULAR_FALLBACKS = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
]


def load_chat_fonts():
    s = SS
    bold_path = _first_existing(LABEL_FALLBACKS)
    reg_path = _first_existing(REGULAR_FALLBACKS) or bold_path
    if bold_path is None:
        sys.exit("No usable system font found for chat text.")
    return {
        "bold": ImageFont.truetype(bold_path, 15 * s),
        "reg": ImageFont.truetype(reg_path, 15 * s),
        "header": ImageFont.truetype(bold_path, 12 * s),
        "chip": ImageFont.truetype(bold_path, 13 * s),
        "chip_reg": ImageFont.truetype(reg_path, 13 * s),
    }


def finish_2x(img, w, h):
    """Premultiplied-alpha downscale from 2x to 1x."""
    s = SS
    return (img.convert("RGBa")
            .resize((w // s, h // s), Image.LANCZOS)
            .convert("RGBA"))


def panel_base(w, h, fill=PANEL_BG):
    """Translucent rounded panel with the house border, at 2x."""
    s = SS
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    for kwargs in ({"fill": fill}, {"outline": BAR_BORDER, "width": 1 * s}):
        lay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(lay).rounded_rectangle(
            (0, 0, w - 1, h - 1), radius=8 * s, **kwargs)
        img.alpha_composite(lay)
    return img


def masked_layer(size, color, draw_fn):
    """Uniform-color RGBA layer from an L coverage mask (fringe-free)."""
    mask = Image.new("L", size, 0)
    draw_fn(ImageDraw.Draw(mask))
    lay = Image.new("RGBA", size, color + (0,))
    lay.putalpha(mask)
    return lay


def build_message(user, color, text, bold, reg):
    """One chat message: colored bold username inline with wrapped text.
    Rendered at 2x, premultiplied-downscaled to a 1x RGBA image."""
    s = SS
    max_w = (PANEL_W - 2 * PAD) * s
    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))

    head = user + ": "
    items = [(0, 0, head, True)]
    x, line = probe.textlength(head, font=bold), 0
    for word in text.split():
        w = probe.textlength(word + " ", font=reg)
        if x + w > max_w and x > 0:
            line += 1
            x = 0
        items.append((x, line, word + " ", False))
        x += w
    n_lines = line + 1

    size = (max_w, n_lines * LINE_H * s + 2 * s)

    def draw_part(want_user):
        def fn(d):
            for ix, il, word, is_user in items:
                if is_user == want_user:
                    d.text((ix, il * LINE_H * s), word,
                           font=bold if is_user else reg, fill=255)
        return fn

    img = masked_layer(size, color, draw_part(True))
    img.alpha_composite(masked_layer(size, TEXT_COLOR, draw_part(False)))
    return (img.convert("RGBa")
            .resize((size[0] // s, size[1] // s), Image.LANCZOS)
            .convert("RGBA"))


def build_panel(fonts):
    """Chat panel background with a small letterspaced header."""
    s = SS
    w, h = PANEL_W * s, PANEL_H * s
    img = panel_base(w, h)

    lay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(lay).line(
        (PAD * s, HEADER_H * s, w - PAD * s, HEADER_H * s),
        fill=(255, 255, 255, 28), width=1 * s)
    img.alpha_composite(lay)

    tracking = round(2.2 * s)
    img.alpha_composite(masked_layer(
        (w, h), LABEL_GREY[:3],
        lambda d: draw_tracked(d, PAD * s, 11 * s, "LIVE CHAT",
                               fonts["header"], 255, tracking)))
    return finish_2x(img, w, h)


def build_chip(fonts, uptime_s):
    """'● LIVE / uptime / viewers' status chip with skewed dividers."""
    s = SS
    h = 36 * s
    pad, gap = 14 * s, 12 * s
    dot_r = 4 * s
    clock = (f"{uptime_s // 3600}:{uptime_s % 3600 // 60:02d}:"
             f"{uptime_s % 60:02d}")
    viewers = f"{VIEWERS} WATCHING"
    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    live_w = probe.textlength("LIVE", font=fonts["chip"])
    clock_w = probe.textlength(clock, font=fonts["chip_reg"])
    view_w = probe.textlength(viewers, font=fonts["chip_reg"])
    w = int(pad + 2 * dot_r + 8 * s + live_w + 2 * (2 * gap + 2 * s)
            + clock_w + view_w + pad)

    img = panel_base(w, h)
    cy = h / 2
    lay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(lay).ellipse(
        (pad, cy - dot_r, pad + 2 * dot_r, cy + dot_r),
        fill=(*ACCENT_RED, 255))
    img.alpha_composite(lay)

    x = pad + 2 * dot_r + 8 * s
    img.alpha_composite(masked_layer(
        (w, h), (255, 255, 255),
        lambda d: d.text((x, cy), "LIVE", font=fonts["chip"],
                         fill=255, anchor="lm")))
    x += live_w + gap

    def divider(d, dx):
        lean = 3 * s
        d.polygon([(dx + lean, 8 * s), (dx + lean + 2 * s, 8 * s),
                   (dx - lean + 2 * s, h - 8 * s), (dx - lean, h - 8 * s)],
                  fill=255)

    grey_parts = []
    for text in (clock, viewers):
        grey_parts.append((x, "div"))
        x += 2 * s + gap
        grey_parts.append((x, text))
        x += probe.textlength(text, font=fonts["chip_reg"]) + gap

    def draw_grey(d):
        for gx, item in grey_parts:
            if item == "div":
                divider(d, gx)
            else:
                d.text((gx, cy), item, font=fonts["chip_reg"],
                       fill=255, anchor="lm")

    img.alpha_composite(masked_layer((w, h), LABEL_GREY[:3], draw_grey))
    return finish_2x(img, w, h)


def build_side_panel(fonts):
    """Latest follower + follower goal progress panel."""
    s = SS
    w, h = SIDE_W * s, SIDE_H * s
    img = panel_base(w, h)
    tracking = round(2.2 * s)
    done, total = FOLLOWER_GOAL

    def grey(d):
        draw_tracked(d, PAD * s, 13 * s, "LATEST FOLLOWER",
                     fonts["header"], 255, tracking)
        draw_tracked(d, PAD * s, 42 * s, "FOLLOWER GOAL",
                     fonts["header"], 255, tracking)

    img.alpha_composite(masked_layer((w, h), LABEL_GREY[:3], grey))
    img.alpha_composite(masked_layer(
        (w, h), ACCENT_BLUE,
        lambda d: d.text((w - PAD * s, 12 * s), LATEST_FOLLOWER,
                         font=fonts["chip"], fill=255, anchor="ra")))
    img.alpha_composite(masked_layer(
        (w, h), (255, 255, 255),
        lambda d: d.text((w - PAD * s, 41 * s), f"{done}/{total}",
                         font=fonts["chip"], fill=255, anchor="ra")))

    bar_y0, bar_y1 = 66 * s, 72 * s
    track = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(track)
    d.rounded_rectangle((PAD * s, bar_y0, w - PAD * s, bar_y1),
                        radius=3 * s, fill=(255, 255, 255, 30))
    img.alpha_composite(track)
    fill_w = PAD * s + (w - 2 * PAD * s) * done / total
    fill = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(fill).rounded_rectangle(
        (PAD * s, bar_y0, fill_w, bar_y1), radius=3 * s,
        fill=(*ACCENT_BLUE, 230))
    img.alpha_composite(fill)
    return finish_2x(img, w, h)


# ------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        description="Render a mock Twitch chat overlay (transparent 1080p).")
    ap.add_argument("--out", default=None,
                    help="output basename (default: output/stream_chat)")
    ap.add_argument("--duration", type=float, default=10.0,
                    help="clip length in seconds (default 10.0)")
    ap.add_argument("--green", action="store_true",
                    help="also render an H.264 green-screen mp4")
    ap.add_argument("--preview", action="store_true",
                    help="dump a preview PNG instead of rendering video")
    args = ap.parse_args()

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None and not args.preview:
        sys.exit("ffmpeg not found on PATH. Install it (e.g. `brew install "
                 "ffmpeg` on macOS, or download from ffmpeg.org on Windows "
                 "and add it to PATH) and re-run.")

    if args.out is None:
        args.out = str(Path(__file__).resolve().parent
                       / "output" / "stream_chat")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)

    fonts = load_chat_fonts()
    panel = build_panel(fonts)
    side = build_side_panel(fonts)
    chip_cache = {}
    messages = [(t, build_message(user, PALETTE[i % len(PALETTE)], text,
                                  fonts["bold"], fonts["reg"]))
                for i, (t, user, text) in enumerate(CHAT_SCRIPT)]

    area_h = PANEL_H - HEADER_H - PAD

    def chat_state(t):
        """(visible message indices, fade of the newest) — the cache key."""
        vis = [i for i, (mt, _) in enumerate(messages) if mt <= t]
        if not vis:
            return (), 1.0
        newest_t = messages[vis[-1]][0]
        fade = min(1.0, max(0.0, (t - newest_t) / FADE))
        return tuple(vis), round(fade * 12) / 12

    def compose(t):
        canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
        sec = UPTIME_START + int(t)
        if sec not in chip_cache:
            chip_cache[sec] = build_chip(fonts, sec)
        canvas.alpha_composite(chip_cache[sec], CHIP_POS)
        canvas.alpha_composite(side, SIDE_POS)
        canvas.alpha_composite(panel, (PANEL_X, PANEL_Y))
        vis, fade = chat_state(t)
        layer = Image.new("RGBA", (PANEL_W - 2 * PAD, area_h), (0, 0, 0, 0))
        y = area_h
        for pos, i in enumerate(reversed(vis)):
            img = messages[i][1]
            y -= img.height + GAP
            yy = y
            if pos == 0 and fade < 1.0:  # newest: fade + slight rise
                img = img.copy()
                img.putalpha(img.getchannel("A").point(
                    lambda v: int(v * fade)))
                yy = y + round((1 - fade) * 8)
            if yy + img.height <= 0:
                break
            if yy < 0:  # partially scrolled past the top: crop, don't skip
                img = img.crop((0, -yy, img.width, img.height))
                yy = 0
            layer.alpha_composite(img, (0, yy))
        canvas.alpha_composite(layer, (PANEL_X + PAD, PANEL_Y + HEADER_H
                                       + PAD // 2))
        return canvas

    if args.preview:
        path = f"{args.out}_preview.png"
        compose(6.0).save(path)
        print(f"wrote {path}")
        return

    n_frames = round(args.duration * OUT_FPS)
    log_dir = tempfile.mkdtemp(prefix="streamchat_ffmpeg_")
    title = "Stream chat overlay"
    encoders = [
        ("rgba", start_encoder(
            ffmpeg, "rgba", None, [],
            ["-c:v", "prores_ks", "-profile:v", "4444",
             "-pix_fmt", "yuva444p10le", "-vendor", "apl0",
             "-metadata", f"title={title}"],
            f"{args.out}_alpha.mov", log_dir)),
        ("rgba", start_encoder(
            ffmpeg, "rgba", None, [],
            ["-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
             "-crf", "28", "-b:v", "0", "-row-mt", "1",
             "-cpu-used", "4", "-auto-alt-ref", "0",
             "-metadata", f"title={title}"],
            f"{args.out}_alpha.webm", log_dir)),
    ]
    if args.green:
        encoders.append(("green", start_encoder(
            ffmpeg, "rgb24", None, [],
            ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
             "-preset", "medium", "-movflags", "+faststart",
             "-metadata", f"title={title}"],
            f"{args.out}_green.mp4", log_dir)))

    green_bg = Image.new("RGBA", (CANVAS_W, CANVAS_H), (*GREEN, 255))
    rgba_bytes = green_bytes = None
    last_key = None
    print(f"Rendering {n_frames} frames -> "
          + ", ".join(p._out_path for _, p in encoders))
    try:
        for f in range(n_frames):
            t = f / OUT_FPS
            key = (chat_state(t), int(t))
            if key != last_key:
                frame = compose(t)
                rgba_bytes = frame.tobytes()
                if args.green:
                    green_bytes = Image.alpha_composite(
                        green_bg, frame).convert("RGB").tobytes()
                last_key = key
            for kind, proc in encoders:
                proc.stdin.write(rgba_bytes if kind == "rgba"
                                 else green_bytes)
            if (f + 1) % (OUT_FPS * 2) == 0:
                print(f"  {f + 1}/{n_frames} frames")
    except BrokenPipeError:
        sys.exit("ffmpeg pipe closed unexpectedly — check the encoder "
                 f"logs in {log_dir}")
    for _, proc in encoders:
        finish_encoder(proc)
    for _, proc in encoders:
        print(f"  wrote {proc._out_path}")


if __name__ == "__main__":
    main()
