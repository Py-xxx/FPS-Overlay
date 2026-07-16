#!/usr/bin/env python3
"""render_overlay.py — FPS comparison lower-third overlay renderer.

Reads two CapFrameX capture JSONs (baseline vs. with-app), renders a static
lower-third bar with live-updating FPS / average FPS / impact %, and encodes:

  {out}_alpha.mov   ProRes 4444 with real alpha  (primary, for NLE compositing)
  {out}_alpha.webm  VP9 with alpha               (lightweight alternative)
  {out}_green.mp4   H.264 over pure green        (optional, --green)

Usage (direct):
  python render_overlay.py --base baseline.json --app withapp.json \
      --game "Fortnite" --out fortnite_overlay [--green] [--preview] \
      [--duration 8.0] [--start 12.5]
  Without --start, the clip-length window with the smallest FPS impact is
  found automatically.

Usage (interactive, e.g. via run_overlay.bat):
  python render_overlay.py
  Scans the asset/ directory next to this script for games laid out as
      asset/<GameName>/Base/<capture>.json    (without the app)
      asset/<GameName>/Acrux/<capture>.json   (with the app)
  then prompts for the game, duration, etc. and writes to output/.
"""

import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from bisect import bisect_right
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except ImportError:
    sys.exit("Pillow is required:  python3 -m pip install Pillow")

# ---------------------------------------------------------------- constants

CANVAS_W, CANVAS_H = 1920, 1080
OUT_FPS = 60
SS = 2  # supersampling factor for the bar (rendered at 2x, downscaled)

TICK = 0.25         # live-FPS sample interval (4 Hz)
WINDOW = 1.0        # live-FPS smoothing window (seconds of real data)
STABILITY_WEIGHT = 0.5  # window scoring: impact vs. counter steadiness

BAR_BG = (7, 9, 14, 184)          # rgba(7,9,14,0.72)
BAR_BORDER = (255, 255, 255, 36)  # rgba(255,255,255,0.14)
IMPACT_TINT = (255, 255, 255, 15) # rgba(255,255,255,0.06)
LABEL_GREY = (0x98, 0xA1, 0xB0, 255)
WHITE = (255, 255, 255, 255)
DIVIDER = (255, 255, 255, 150)
GREEN = (0, 255, 0)

ANTON_URL = ("https://raw.githubusercontent.com/google/fonts/main/"
             "ofl/anton/Anton-Regular.ttf")

BIG_FALLBACKS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Narrow Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "C:/Windows/Fonts/arialbd.ttf",
]
LABEL_FALLBACKS = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


# ------------------------------------------------------------------- fonts

def _first_existing(paths):
    for p in paths:
        if Path(p).is_file():
            return p
    return None


def get_anton_path():
    """Return a path to Anton-Regular.ttf, downloading and caching it if needed."""
    cache = Path(__file__).resolve().parent / "asset" / "_fonts"
    cached = cache / "Anton-Regular.ttf"
    if cached.is_file():
        return str(cached)
    try:
        print("Downloading Anton font from Google Fonts ...")
        with urllib.request.urlopen(ANTON_URL, timeout=15) as resp:
            data = resp.read()
        cache.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(data)
        return str(cached)
    except Exception as exc:
        print(f"  warning: could not download Anton ({exc}); "
              "falling back to a system bold font")
        return None


def load_fonts():
    anton = get_anton_path()
    big_path = anton or _first_existing(BIG_FALLBACKS)
    label_path = _first_existing(LABEL_FALLBACKS) or big_path
    if big_path is None:
        sys.exit("No usable font found (Anton download failed and no "
                 "system fallback exists).")

    def f(path, size):
        return ImageFont.truetype(path, size)

    s = SS
    return {
        "big": f(big_path, 52 * s),
        "suffix": f(big_path, 17 * s),
        "label": f(label_path, 14 * s),
        "avg": f(label_path, 14 * s),
        "note": f(label_path, 13 * s),
    }


# ------------------------------------------------------------------- data

def load_capture(path):
    """Parse a CapFrameX capture; return sorted per-frame timestamps."""
    p = Path(path)
    if not p.is_file():
        sys.exit(f"Input file not found: {path}")
    try:
        with open(p, encoding="utf-8-sig") as fh:
            root = json.load(fh)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        sys.exit(f"Could not parse {path} as CapFrameX JSON: {exc}")
    try:
        cap = root["Runs"][0]["CaptureData"]
        times = [float(t) for t in cap["TimeInSeconds"]]
    except (KeyError, IndexError, TypeError) as exc:
        sys.exit(f"{path} does not look like a CapFrameX capture "
                 f"(missing Runs[0].CaptureData.TimeInSeconds): {exc}")
    if len(times) < 2:
        sys.exit(f"{path}: capture contains fewer than 2 frames")
    times.sort()
    duration = times[-1] - times[0]
    if duration < 55.0:
        print(f"  warning: {p.name} holds only {duration:.1f}s of data "
              "(expected ~60s)")
    return times


def avg_fps(times):
    return len(times) / (times[-1] - times[0])


def live_fps(times, t):
    """Frames presented in the (t - WINDOW, t] interval, as FPS."""
    n = bisect_right(times, t) - bisect_right(times, t - WINDOW)
    return round(n / WINDOW)


def impact_text(base_avg, app_avg):
    """Signed FPS difference with the app: -X% loss, +X% gain, <1% if tiny."""
    diff = (app_avg - base_avg) / base_avg * 100.0
    if abs(diff) < 1.0:
        return "<1%"
    return f"{'+' if diff > 0 else '-'}{round(abs(diff))}%"


def window_avg_fps(times, t0, t1):
    n = bisect_right(times, t1) - bisect_right(times, t0)
    return n / (t1 - t0)


def live_fps_series(times):
    """Live FPS at every absolute TICK-grid time covered by the capture.
    Returns (first_grid_index, values)."""
    k0 = math.ceil((times[0] + WINDOW) / TICK)
    k1 = math.floor(times[-1] / TICK)
    return k0, [live_fps(times, k * TICK) for k in range(k0, k1 + 1)]


def find_best_start(base_times, app_times, duration):
    """Scan both captures (in TICK steps) for the clip-length window where
    the app's FPS impact vs. baseline is smallest AND the live counters are
    steady, so the on-screen numbers support the averages instead of
    flashing unrepresentative spikes. Returns (start, impact)."""
    n_ticks = round(duration / TICK)
    bk0, b_series = live_fps_series(base_times)
    ak0, a_series = live_fps_series(app_times)
    k_lo = max(bk0, ak0)
    k_hi = min(bk0 + len(b_series), ak0 + len(a_series)) - 1 - n_ticks
    if k_hi < k_lo:
        print("  warning: captures are shorter than the clip; "
              "starting at the beginning instead of auto-selecting")
        return max(base_times[0], app_times[0]) + WINDOW, None
    best = None
    for k in range(k_lo, k_hi + 1):
        t = k * TICK
        b_avg = window_avg_fps(base_times, t, t + duration)
        a_avg = window_avg_fps(app_times, t, t + duration)
        if b_avg <= 0 or a_avg <= 0:
            continue
        impact = (b_avg - a_avg) / b_avg
        b_ticks = b_series[k - bk0: k - bk0 + n_ticks + 1]
        a_ticks = a_series[k - ak0: k - ak0 + n_ticks + 1]
        # Mean relative deviation of the live counters from their own
        # window averages: high = spiky, misleading numbers.
        dev = (sum(abs(v - b_avg) for v in b_ticks) / len(b_ticks) / b_avg
               + sum(abs(v - a_avg) for v in a_ticks) / len(a_ticks) / a_avg)
        # Deviation of the base-vs-app counter gap from the window's average
        # gap: high = moments where the two numbers contradict the story.
        gap = b_avg - a_avg
        div = (sum(abs((b - a) - gap) for b, a in zip(b_ticks, a_ticks))
               / len(b_ticks) / b_avg)
        score = impact + STABILITY_WEIGHT * (dev + div)
        if best is None or score < best[0]:
            best = (score, t, impact)
    _, best_t, best_impact = best
    return best_t, best_impact


def build_tick_table(times, start, n_ticks, name):
    """Live-FPS value for each 0.25s tick, clamped to the capture's range.
    A median-of-3 pass across ticks removes single-tick spikes that would
    flash numbers wildly off the capture's average."""
    t0, t1 = times[0], times[-1]
    vals, clamped = [], False
    for k in range(n_ticks):
        t = start + k * TICK
        if t > t1:
            t, clamped = t1, True
        t = max(t, t0 + WINDOW)
        vals.append(live_fps(times, t))
    if clamped:
        print(f"  warning: {name} ran out of data before the clip ended; "
              "the last live value is held")
    smoothed = []
    for i in range(len(vals)):
        neigh = sorted(vals[max(0, i - 1):i + 2])
        smoothed.append(neigh[len(neigh) // 2])
    return smoothed


# --------------------------------------------------------------- rendering

def draw_tracked(draw, x, y, text, font, fill, tracking):
    """Draw text with letterspacing; returns nothing (x precomputed)."""
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking


def tracked_width(draw, text, font, tracking):
    if not text:
        return 0
    return (sum(draw.textlength(c, font=font) for c in text)
            + tracking * (len(text) - 1))


def glow_text(layer_size, draw_fn, blur, alpha, color=(255, 255, 255)):
    """Render draw_fn (which draws onto an L mask with fill=255) and return
    (glow_layer, crisp_layer) as uniform-color RGBA with the coverage as
    alpha. Drawing directly onto a transparent RGBA layer instead would
    darken the antialiased edge pixels (RGB gets scaled by coverage)."""
    mask = Image.new("L", layer_size, 0)
    draw_fn(ImageDraw.Draw(mask))
    crisp = Image.new("RGBA", layer_size, color + (0,))
    crisp.putalpha(mask)
    glow = Image.new("RGBA", layer_size, color + (0,))
    glow.putalpha(mask.filter(ImageFilter.GaussianBlur(blur)).point(
        lambda v: int(v * alpha)))
    return glow, crisp


class BarRenderer:
    """Renders the lower-third bar at 2x and caches downscaled 1x images."""

    def __init__(self, fonts, base_avg_disp, app_avg_disp, impact,
                 max_live_digits, note=""):
        self.fonts = fonts
        self.impact = impact
        self.note = note
        s = SS
        self.rect_h = 118 * s          # the rounded-rect bar itself
        self.note_gap = 8 * s
        # total image height includes the caption hanging below the bar
        self.h = self.rect_h + (self.note_gap + 20 * s if note else 0)
        self.tracking = round(2.2 * s)

        probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
        big = fonts["big"]

        # Fixed-width number box sized to the widest live value seen in the
        # data, so changing digits never shift the layout.
        widest_digit = max(probe.textlength(d, font=big) for d in "0123456789")
        self.num_box_w = int(widest_digit * max_live_digits) + 4 * s
        self.suffix_w = probe.textlength("FPS", font=fonts["suffix"])
        self.num_gap = 5 * s

        group_w = self.num_box_w + self.num_gap + self.suffix_w
        cell_pad = 26 * s
        label_w = max(tracked_width(probe, t, fonts["label"], self.tracking)
                      for t in ("WITHOUT APP", "WITH APP"))
        self.fps_cell_w = int(max(group_w, label_w) + 2 * cell_pad)

        impact_label_w = tracked_width(probe, "IMPACT", fonts["label"],
                                       self.tracking)
        impact_val_w = probe.textlength(impact, font=big)
        self.impact_cell_w = int(max(impact_label_w, impact_val_w, 70 * s)
                                 + 2 * cell_pad)

        self.w = 2 * self.fps_cell_w + self.impact_cell_w
        if note:
            note_w = tracked_width(probe, note, fonts["note"],
                                   self.tracking)
            self.w = max(self.w, int(note_w) + 8 * s)
        x0 = (self.w - (2 * self.fps_cell_w + self.impact_cell_w)) // 2
        self.cells = [
            (x0, self.fps_cell_w),
            (x0 + self.fps_cell_w, self.fps_cell_w),
            (x0 + 2 * self.fps_cell_w, self.impact_cell_w),
        ]
        # Row geometry (2x px)
        self.label_y = 16 * s
        self.big_baseline = 78 * s
        self.avg_y = 92 * s

        self.avg_disp = (base_avg_disp, app_avg_disp)
        self.static = self._render_static()
        self._cache = {}

    # -- static parts: bg, border, tint, dividers, labels, avg rows, impact
    def _render_static(self):
        s = SS
        img = Image.new("RGBA", (self.w, self.h), (0, 0, 0, 0))

        # Semi-transparent shapes must be composited from their own layers:
        # drawing them directly would overwrite the pixels underneath
        # (including alpha) instead of blending.
        def layer(draw_fn):
            lay = Image.new("RGBA", (self.w, self.h), (0, 0, 0, 0))
            draw_fn(ImageDraw.Draw(lay))
            return lay

        # All dividers and the tint edge lean along the same skew line:
        # x(y) for the centerline of a divider anchored at cell boundary cx.
        rh = self.rect_h
        lean = math.tan(math.radians(12)) * (rh / 2)

        def div_x(cx, y):
            return cx + lean * (0.5 - y / rh)

        tint_cx = self.cells[2][0]
        content = layer(lambda d: d.rectangle(
            (0, 0, self.w, rh), fill=BAR_BG))
        content.alpha_composite(layer(lambda d: d.polygon(
            [(div_x(tint_cx, 0), 0), (self.w, 0),
             (self.w, rh), (div_x(tint_cx, rh), rh)],
            fill=IMPACT_TINT)))

        # Clip to the rounded-rect silhouette
        mask = Image.new("L", (self.w, self.h), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, self.w - 1, rh - 1), radius=8 * s, fill=255)
        content.putalpha(Image.composite(
            content.getchannel("A"), Image.new("L", content.size, 0), mask))
        img.alpha_composite(content)

        img.alpha_composite(layer(lambda d: d.rounded_rectangle(
            (0, 0, self.w - 1, rh - 1), radius=8 * s,
            outline=BAR_BORDER, width=1 * s)))

        # Skewed dividers with a slight glow, on the same line as the tint edge
        inset = 10 * s
        half_w = 1 * s  # 2px wide at 1x

        def divider_poly(cx):
            top_x = div_x(cx, inset)
            bot_x = div_x(cx, rh - inset)
            return [(top_x - half_w, inset), (top_x + half_w, inset),
                    (bot_x + half_w, rh - inset),
                    (bot_x - half_w, rh - inset)]

        for cx in (self.cells[1][0], self.cells[2][0]):
            glow, _ = glow_text(
                img.size,
                lambda dd, cx=cx: dd.polygon(divider_poly(cx), fill=255),
                blur=3 * s, alpha=0.35)
            img.alpha_composite(glow)
            img.alpha_composite(layer(
                lambda d, cx=cx: d.polygon(divider_poly(cx), fill=DIVIDER)))

        # Labels + AVG rows + impact value
        d = ImageDraw.Draw(img)
        labels = ("WITHOUT APP", "WITH APP", "IMPACT")
        for (cell_x, cell_w), label in zip(self.cells, labels):
            cx = cell_x + cell_w / 2
            lw = tracked_width(d, label, self.fonts["label"], self.tracking)
            draw_tracked(d, cx - lw / 2, self.label_y, label,
                         self.fonts["label"], LABEL_GREY, self.tracking)

        # "FPS" suffixes are static: same position regardless of the value
        for i in (0, 1):
            cell_x, cell_w = self.cells[i]
            cx = cell_x + cell_w / 2
            group_w = self.num_box_w + self.num_gap + self.suffix_w
            d.text((cx + group_w / 2 - self.suffix_w, self.big_baseline),
                   "FPS", font=self.fonts["suffix"], fill=LABEL_GREY,
                   anchor="ls")

        for i in (0, 1):
            cell_x, cell_w = self.cells[i]
            cx = cell_x + cell_w / 2
            grey, white_num = "AVG ", str(self.avg_disp[i])
            gw = d.textlength(grey, font=self.fonts["avg"])
            ww = d.textlength(white_num, font=self.fonts["avg"])
            x = cx - (gw + ww) / 2
            d.text((x, self.avg_y), grey, font=self.fonts["avg"],
                   fill=LABEL_GREY)
            d.text((x + gw, self.avg_y), white_num, font=self.fonts["avg"],
                   fill=WHITE)

        # Impact value (static, glowing, centered a touch lower since the
        # impact cell has no AVG row)
        cell_x, cell_w = self.cells[2]
        cx = cell_x + cell_w / 2
        baseline = self.big_baseline + 6 * s
        glow, crisp = glow_text(
            img.size,
            lambda dd: dd.text((cx, baseline), self.impact,
                               font=self.fonts["big"], fill=255,
                               anchor="ms"),
            blur=6 * s, alpha=0.30)
        img.alpha_composite(glow)
        img.alpha_composite(crisp)

        # Small caption hanging below the bar (e.g. capture format).
        # It sits over fully transparent pixels, where drawing text directly
        # darkens antialiased edges (the RGB gets scaled by coverage) and
        # looks pixelated once composited over footage — so render the
        # coverage as a mask and apply a uniform color instead.
        if self.note:
            cov = Image.new("L", img.size, 0)
            md = ImageDraw.Draw(cov)
            nw = tracked_width(md, self.note, self.fonts["note"],
                               self.tracking)
            draw_tracked(md, (self.w - nw) / 2, rh + self.note_gap,
                         self.note, self.fonts["note"], 255, self.tracking)
            note_lay = Image.new("RGBA", img.size, LABEL_GREY[:3] + (0,))
            note_lay.putalpha(cov)
            img.alpha_composite(note_lay)
        return img

    def render(self, live_base, live_app):
        """Return the 1x RGBA bar image for a pair of live FPS values."""
        key = (live_base, live_app)
        if key in self._cache:
            return self._cache[key]

        s = SS
        img = self.static.copy()

        def draw_pair(dd):
            for i, val in enumerate((live_base, live_app)):
                cell_x, cell_w = self.cells[i]
                cx = cell_x + cell_w / 2
                group_w = self.num_box_w + self.num_gap + self.suffix_w
                box_cx = cx - group_w / 2 + self.num_box_w / 2
                dd.text((box_cx, self.big_baseline), str(val),
                        font=self.fonts["big"], fill=255, anchor="ms")

        glow, crisp = glow_text(img.size, draw_pair, blur=6 * s, alpha=0.30)
        img.alpha_composite(glow)
        img.alpha_composite(crisp)

        # Downscale in premultiplied alpha: resampling straight-alpha RGBA
        # bleeds the black RGB of transparent neighbors into edge pixels.
        out = (img.convert("RGBa")
               .resize((self.w // s, self.h // s), Image.LANCZOS)
               .convert("RGBA"))
        self._cache[key] = out
        return out


# ---------------------------------------------------------------- ffmpeg

def start_encoder(ffmpeg, in_pix_fmt, out_args, out_path, title, log_dir):
    log = open(Path(log_dir) / (Path(out_path).name + ".log"), "wb")
    cmd = [ffmpeg, "-y",
           "-f", "rawvideo", "-pix_fmt", in_pix_fmt,
           "-video_size", f"{CANVAS_W}x{CANVAS_H}",
           "-framerate", str(OUT_FPS), "-i", "-",
           "-metadata", f"title={title}",
           *out_args, str(out_path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=log,
                            stderr=subprocess.STDOUT)
    proc._log_file = log
    proc._out_path = str(out_path)
    return proc


def finish_encoder(proc):
    proc.stdin.close()
    ret = proc.wait()
    proc._log_file.close()
    if ret != 0:
        tail = Path(proc._log_file.name).read_text(errors="replace")[-2000:]
        sys.exit(f"ffmpeg failed for {proc._out_path} "
                 f"(exit {ret}):\n{tail}")


# ----------------------------------------------------------- interactive

ASSET_DIR_NAME = "asset"
BASE_SUBDIR = "Base"
APP_SUBDIR = "Acrux"


def ask(msg):
    try:
        return input(msg).strip()
    except (EOFError, KeyboardInterrupt):
        sys.exit("\nCancelled.")


def ask_float(label, default):
    while True:
        raw = ask(f"{label} [{default}]: ")
        if not raw:
            return default
        try:
            val = float(raw)
            if val > 0:
                return val
        except ValueError:
            pass
        print("  please enter a positive number")


def ask_yn(label, default=False):
    hint = "Y/n" if default else "y/N"
    raw = ask(f"{label} [{hint}]: ").lower()
    if not raw:
        return default
    return raw in ("y", "yes")


def discover_games(asset_dir):
    """Return [(name, base_jsons, app_jsons)] for every valid game folder."""
    games = []
    for d in sorted(asset_dir.iterdir()):
        if not d.is_dir() or d.name.startswith((".", "_")):
            continue
        base_dir, app_dir = d / BASE_SUBDIR, d / APP_SUBDIR
        if not (base_dir.is_dir() and app_dir.is_dir()):
            print(f"  skipping {d.name}/ (needs both {BASE_SUBDIR}/ "
                  f"and {APP_SUBDIR}/ subfolders)")
            continue
        base_jsons = sorted(base_dir.glob("*.json"))
        app_jsons = sorted(app_dir.glob("*.json"))
        if not base_jsons or not app_jsons:
            print(f"  skipping {d.name}/ (missing a .json capture in "
                  f"{BASE_SUBDIR}/ or {APP_SUBDIR}/)")
            continue
        games.append((d.name, base_jsons, app_jsons))
    return games


def pick_best_pair(base_files, app_files, duration):
    """Evaluate every Base x Acrux capture combination and return the
    (base_file, app_file) pair whose best clip-length window has the
    smallest FPS impact."""
    cache = {}

    def load(p):
        if p not in cache:
            cache[p] = load_capture(p)
        return cache[p]

    best = None
    for bf in base_files:
        for af in app_files:
            _, imp = find_best_start(load(bf), load(af), duration)
            score = math.inf if imp is None else imp
            if imp is not None:
                print(f"  {bf.name} vs {af.name}: "
                      f"best window impact {imp * 100:+.1f}%")
            if best is None or score < best[0]:
                best = (score, bf, af)
    _, bf, af = best
    print(f"  -> using {BASE_SUBDIR}/{bf.name} vs {APP_SUBDIR}/{af.name}")
    return bf, af


def interactive_config(args):
    """Fill args in-place by scanning asset/ and prompting the user."""
    script_dir = Path(__file__).resolve().parent
    asset_dir = script_dir / ASSET_DIR_NAME
    layout_help = (
        f"Expected layout:\n"
        f"  {asset_dir}/<GameName>/{BASE_SUBDIR}/<capture>.json\n"
        f"  {asset_dir}/<GameName>/{APP_SUBDIR}/<capture>.json")
    if not asset_dir.is_dir():
        sys.exit(f"No '{ASSET_DIR_NAME}' directory found next to the "
                 f"script.\n{layout_help}")
    print(f"Scanning {asset_dir} ...")
    games = discover_games(asset_dir)
    if not games:
        sys.exit(f"No game folders with captures found.\n{layout_help}")

    print("\nAvailable games:")
    for i, (name, base_jsons, app_jsons) in enumerate(games, 1):
        def describe(files):
            return files[0].name if len(files) == 1 else f"{len(files)} captures"
        print(f"  {i}. {name}  ({BASE_SUBDIR}: {describe(base_jsons)}, "
              f"{APP_SUBDIR}: {describe(app_jsons)})")
    while True:
        raw = ask(f"Pick a game [1-{len(games)}, default 1]: ") or "1"
        if raw.isdigit() and 1 <= int(raw) <= len(games):
            idx = int(raw) - 1
            break
        print("  please enter a number from the list")

    name, base_jsons, app_jsons = games[idx]
    if len(base_jsons) == 1 and len(app_jsons) == 1:
        base_json, app_json = base_jsons[0], app_jsons[0]
    else:
        print(f"\nEvaluating {len(base_jsons) * len(app_jsons)} "
              "capture combinations ...")
        base_json, app_json = pick_best_pair(base_jsons, app_jsons,
                                             args.duration)
    args.game = name
    args.base = str(base_json)
    args.app = str(app_json)

    slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    out_dir = script_dir / "output" / name
    args.out = str(out_dir / f"{slug}_overlay")
    print(f"\nGame: {name}   duration: {args.duration}s   "
          f"output: {args.out}_*\n")


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(
        description="Render FPS comparison overlay videos from two "
                    "CapFrameX captures.")
    ap.add_argument("--base", help="baseline capture JSON")
    ap.add_argument("--app", help="with-app capture JSON")
    ap.add_argument("--game", default="", help="game name (file metadata)")
    ap.add_argument("--out", help="output basename")
    ap.add_argument("--interactive", action="store_true",
                    help="pick a game from the asset/ directory and be "
                         "prompted for options (default when --base/--app/"
                         "--out are omitted)")
    ap.add_argument("--duration", type=float, default=8.0,
                    help="clip length in seconds (default 8.0)")
    ap.add_argument("--start", type=float, default=None,
                    help="offset into the captures for live numbers; "
                         "default: auto-pick the clip-length window where "
                         "the FPS impact is smallest")
    ap.add_argument("--note", default="RECORDED IN 1080p @ 120 FPS",
                    help="small caption below the bar; pass an empty "
                         "string to disable (default: %(default)s)")
    ap.add_argument("--green", action="store_true",
                    help="also render an H.264 green-screen mp4")
    ap.add_argument("--preview", action="store_true",
                    help="dump a preview PNG instead of rendering video")
    args = ap.parse_args()

    if args.interactive or not (args.base or args.app or args.out):
        interactive_config(args)
    elif not (args.base and args.app and args.out):
        ap.error("--base, --app and --out must all be given "
                 "(or none, for interactive mode)")

    if args.duration <= 0:
        sys.exit("--duration must be positive")

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None and not args.preview:
        sys.exit("ffmpeg not found on PATH. Install it (e.g. `brew install "
                 "ffmpeg` on macOS, or download from ffmpeg.org on Windows "
                 "and add it to PATH) and re-run.")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)

    print(f"Loading captures ...")
    base_times = load_capture(args.base)
    app_times = load_capture(args.app)

    base_avg = avg_fps(base_times)
    app_avg = avg_fps(app_times)
    impact = impact_text(base_avg, app_avg)

    if args.start is None:
        args.start, win_impact = find_best_start(base_times, app_times,
                                                 args.duration)
        if win_impact is not None:
            print(f"Auto-selected live window: {args.start:.2f}s - "
                  f"{args.start + args.duration:.2f}s "
                  f"(impact within window: {win_impact * 100:+.1f}%)")

    n_frames = round(args.duration * OUT_FPS)
    n_ticks = math.ceil(args.duration / TICK) + 1
    base_ticks = build_tick_table(base_times, args.start, n_ticks, "baseline")
    app_ticks = build_tick_table(app_times, args.start, n_ticks, "app")
    max_digits = max(2, *(len(str(v)) for v in base_ticks + app_ticks))

    fonts = load_fonts()
    bar = BarRenderer(fonts, round(base_avg), round(app_avg), impact,
                      max_digits, note=args.note.strip())
    bar_w_1x, rect_h_1x = bar.w // SS, bar.rect_h // SS
    bar_x = (CANVAS_W - bar_w_1x) // 2

    # Static position: bar bottom edge 8% up from the frame bottom
    bar_y = round(CANVAS_H * 0.92 - rect_h_1x)

    def compose(t):
        """Full transparent 1920x1080 RGBA frame at clip time t."""
        k = min(int(t / TICK), n_ticks - 1)
        canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
        canvas.alpha_composite(bar.render(base_ticks[k], app_ticks[k]),
                               (bar_x, bar_y))
        return canvas, (base_ticks[k], app_ticks[k])

    title = (f"{args.game} FPS comparison" if args.game
             else "FPS comparison")

    if args.preview:
        t = args.duration / 2
        frame, _ = compose(t)
        path = f"{args.out}_preview.png"
        frame.save(path)
        print(f"  wrote {path}  (t={t:.2f}s)")
    else:
        out_mov = f"{args.out}_alpha.mov"
        out_webm = f"{args.out}_alpha.webm"
        out_green = f"{args.out}_green.mp4"
        log_dir = tempfile.mkdtemp(prefix="overlay_ffmpeg_")

        encoders = [
            ("rgba", start_encoder(
                ffmpeg, "rgba",
                ["-c:v", "prores_ks", "-profile:v", "4444",
                 "-pix_fmt", "yuva444p10le", "-vendor", "apl0"],
                out_mov, title, log_dir)),
            ("rgba", start_encoder(
                ffmpeg, "rgba",
                ["-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
                 "-crf", "28", "-b:v", "0", "-row-mt", "1",
                 "-cpu-used", "4", "-auto-alt-ref", "0"],
                out_webm, title, log_dir)),
        ]
        if args.green:
            encoders.append(("green", start_encoder(
                ffmpeg, "rgb24",
                ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
                 "-preset", "medium", "-movflags", "+faststart"],
                out_green, title, log_dir)))

        green_bg = Image.new("RGBA", (CANVAS_W, CANVAS_H), (*GREEN, 255))
        rgba_bytes = green_bytes = None
        last_key = None
        print(f"Rendering {n_frames} frames -> "
              + ", ".join(p._out_path for _, p in encoders))
        try:
            for f in range(n_frames):
                t = f / OUT_FPS
                frame, key = compose(t)
                if key != last_key:
                    rgba_bytes = frame.tobytes()
                    if args.green:
                        green_bytes = Image.alpha_composite(
                            green_bg, frame).convert("RGB").tobytes()
                    last_key = key
                for kind, proc in encoders:
                    proc.stdin.write(rgba_bytes if kind == "rgba"
                                     else green_bytes)
                if (f + 1) % OUT_FPS == 0:
                    print(f"  {f + 1}/{n_frames} frames")
        except BrokenPipeError:
            sys.exit("ffmpeg pipe closed unexpectedly — check the encoder "
                     f"logs in {log_dir}")
        for _, proc in encoders:
            finish_encoder(proc)
        for _, proc in encoders:
            print(f"  wrote {proc._out_path}")

    print()
    game = f" ({args.game})" if args.game else ""
    print(f"Stats{game}:")
    print(f"  baseline avg : {base_avg:.1f} FPS (shown as {round(base_avg)})")
    print(f"  with-app avg : {app_avg:.1f} FPS (shown as {round(app_avg)})")
    print(f"  impact       : {impact}")


if __name__ == "__main__":
    main()
