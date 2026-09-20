#!/usr/bin/env python3
"""
render.py — renders status.json into sign.png for the reTerminal E1001 door sign.

Usage:
    python3 render.py status.json sign.png
    python3 render.py status.json sign.png --preview          # also writes preview.png (scaled up, for viewing on a normal monitor)
    python3 render.py status.json sign.png --stale-minutes 45 # override staleness threshold

status.json shape (written by the launchd job / Shortcut / NFC handler):
{
  "state": "busy" | "free",
  "until": "2026-09-17T15:30:00-05:00",   // ISO8601, when this state expires. Required for "busy".
  "label": "In meeting",                   // free text shown under the big state word
  "next_event": {                          // optional — for front-running the calendar
      "start": "2026-09-17T16:00:00-05:00",
      "label": "Standup"
  },
  "last_fetch": "2026-09-17T15:12:03-05:00", // when the data source last successfully updated. Defaults to file mtime if absent.
  "booking_url": "https://calendly.com/you/15min"  // optional — shown as a QR code, only while state is FREE
}

Design intent (matches project brief):
  - 800x480, 1-bit, readable from ~15 ft down a hallway.
  - State derived from `until` vs now, not trusted as a static boolean — if `until` has
    passed, the sign shows FREE even if state=="busy" was the last thing written.
  - Front-run: if free now but next_event starts soon, show it as a subtitle.
  - Staleness: small "updated H:MMp" stamp bottom-right; if last_fetch is older than
    --stale-minutes, overlay a warning banner instead of trusting stale data.
  - QR code (bottom-left) to booking_url, shown only while FREE — lets someone grab
    time without knocking. Omitted entirely if booking_url isn't set.
  - Date/day-of-week stamp, top-left corner.

Requires the `qrcode` package: pip install qrcode[pil]
"""

import sys
import json
import argparse
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 800, 480

# Font paths differ per OS — try known locations in order, fall back to PIL's
# built-in bitmap font (ugly but always works) if none are found.
BOLD_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",       # Linux
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",           # macOS
    "/Library/Fonts/Arial Bold.ttf",                                # macOS (older)
    "C:\\Windows\\Fonts\\arialbd.ttf",                              # Windows
]
REGULAR_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
]

BLACK = 0
WHITE = 255

# How soon "next event" starts before we front-run it onto a FREE screen.
FRONT_RUN_WINDOW_MIN = 15

_font_cache = {}


def font(weight, size):
    """weight is 'bold' or 'regular'. Caches loaded fonts by (weight, size)."""
    key = (weight, size)
    if key in _font_cache:
        return _font_cache[key]
    candidates = BOLD_CANDIDATES if weight == "bold" else REGULAR_CANDIDATES
    for path in candidates:
        if Path(path).exists():
            try:
                f = ImageFont.truetype(path, size)
                _font_cache[key] = f
                return f
            except Exception:
                continue
    # Nothing found on this system — PIL's built-in font, works everywhere.
    f = ImageFont.load_default(size=size)
    _font_cache[key] = f
    return f


def parse_dt(s):
    """Parse ISO8601 with offset; naive strings are assumed local time."""
    return datetime.fromisoformat(s)


def load_status(path):
    data = json.loads(Path(path).read_text())
    mtime = datetime.fromtimestamp(Path(path).stat().st_mtime).astimezone()
    if "last_fetch" not in data:
        data["last_fetch"] = mtime.isoformat()
    return data


def resolve_display_state(data, now):
    """
    Turns raw status.json + now into what to actually draw.
    Returns a dict: {state, headline, subline, until_str}
    state is one of: "busy", "free", "offline"
    """
    until = None
    if data.get("until"):
        until = parse_dt(data["until"])
        if until.tzinfo is None:
            until = until.astimezone()

    is_busy = data.get("state") == "busy" and until is not None and until > now

    if is_busy and until is not None:  # redundant guard, but keeps the type checker happy
        return {
            "state": "busy",
            "headline": "IN MEETING",
            "subline": data.get("label", ""),
            "until_str": f"until {until.strftime('%-I:%M %p')}",
        }

    # Not busy -> free. Check whether to front-run the next event.
    next_event = data.get("next_event")
    if next_event:
        start = parse_dt(next_event["start"])
        if start.tzinfo is None:
            start = start.astimezone()
        minutes_away = (start - now).total_seconds() / 60
        if 0 <= minutes_away <= FRONT_RUN_WINDOW_MIN:
            return {
                "state": "free",
                "headline": "FREE",
                "subline": f"{next_event.get('label', 'Busy')} at {start.strftime('%-I:%M %p')}",
                "until_str": None,
            }

    return {
        "state": "free",
        "headline": "FREE",
        "subline": data.get("label", "") if data.get("state") == "free" else "",
        "until_str": None,
    }


def is_stale(last_fetch_str, now, stale_minutes):
    last_fetch = parse_dt(last_fetch_str)
    if last_fetch.tzinfo is None:
        last_fetch = last_fetch.astimezone()
    age_min = (now - last_fetch).total_seconds() / 60
    return age_min > stale_minutes, age_min, last_fetch


def draw_centered_text(draw, text, y, fnt, canvas_width=WIDTH):
    bbox = draw.textbbox((0, 0), text, font=fnt)
    w = bbox[2] - bbox[0]
    x = (canvas_width - w) // 2
    draw.text((x, y), text, font=fnt, fill=BLACK)
    return bbox[3] - bbox[1]  # height, for stacking


def make_qr(url, box_size=4, border=1):
    """Returns a 1-bit PIL image of the QR code, no extra padding beyond `border`."""
    try:
        import qrcode
        import qrcode.constants
        from qrcode.image.pil import PilImage
    except ImportError:
        raise SystemExit("booking_url is set but the 'qrcode' package isn't installed. Run: pip install qrcode[pil]")
    qr = qrcode.QRCode(
        box_size=box_size,
        border=border,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        image_factory=PilImage,  # force the PIL backend explicitly, not the default PNG one
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    return img.get_image().convert("1")  # .get_image() unwraps qrcode's PilImage to a real PIL.Image


def render(data, now, stale_minutes):
    img = Image.new("1", (WIDTH, HEIGHT), color=1)  # 1 = white in mode "1"
    draw = ImageDraw.Draw(img)

    stale, age_min, last_fetch = is_stale(data["last_fetch"], now, stale_minutes)

    if stale:
        # Confidently-stale data is worse than none — override everything with a warning.
        draw.rectangle([0, 0, WIDTH, HEIGHT], fill=WHITE)
        f_warn = font("bold", 64)
        f_sub = font("regular", 32)
        draw_centered_text(draw, "\u26a0 SIGN OFFLINE", 140, f_warn)
        draw_centered_text(draw, "knock, or check phone", 230, f_sub)
        draw_centered_text(
            draw,
            f"last update {last_fetch.strftime('%-I:%M %p')} ({int(age_min)} min ago)",
            300,
            font("regular", 24),
        )
        return img

    disp = resolve_display_state(data, now)

    f_headline = font("bold", 110)
    f_subline = font("regular", 44)
    f_until = font("regular", 32)
    f_stamp = font("regular", 20)

    # Vertical layout: headline centered in the upper-middle, subline below it.
    headline_y = 120
    draw_centered_text(draw, disp["headline"], headline_y, f_headline)

    sub_y = 260
    if disp["subline"]:
        draw_centered_text(draw, disp["subline"], sub_y, f_subline)

    if disp["until_str"]:
        draw_centered_text(draw, disp["until_str"], sub_y + 60, f_until)

    # Date / day-of-week, top-left corner.
    date_str = now.strftime("%a, %b %-d")
    draw.text((20, 18), date_str, font=f_stamp, fill=BLACK)

    # QR code to booking_url, bottom-left, only while FREE. Skipped entirely if
    # booking_url isn't set, or state is busy.
    booking_url = data.get("booking_url")
    if disp["state"] == "free" and booking_url:
        qr_img = make_qr(booking_url, box_size=4, border=1)
        qr_size = qr_img.size[0]
        qr_x, qr_y = 30, HEIGHT - qr_size - 46
        img.paste(qr_img, (qr_x, qr_y))
        caption = "scan to book time"
        draw.text((qr_x, qr_y + qr_size + 4), caption, font=f_stamp, fill=BLACK)

    # Staleness stamp, bottom-right, always shown (not just when stale) so you can
    # eyeball freshness at a glance even when everything's fine.
    stamp = f"updated {last_fetch.strftime('%-I:%M %p')}"
    bbox = draw.textbbox((0, 0), stamp, font=f_stamp)
    stamp_w = bbox[2] - bbox[0]
    draw.text((WIDTH - stamp_w - 16, HEIGHT - 32), stamp, font=f_stamp, fill=BLACK)

    return img


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("status_json", help="path to status.json")
    ap.add_argument("output_png", help="path to write sign.png (1-bit, 800x480)")
    ap.add_argument("--stale-minutes", type=int, default=45, help="staleness threshold in minutes (default 45)")
    ap.add_argument("--preview", action="store_true", help="also write preview.png scaled up for viewing on a monitor")
    ap.add_argument("--raw", action="store_true", help="also write a .bin file: the raw 1bpp buffer the ESP32 firmware fetches directly")
    ap.add_argument("--now", help="override 'now' as ISO8601, for testing specific scenarios")
    args = ap.parse_args()

    now = parse_dt(args.now).astimezone() if args.now else datetime.now().astimezone()

    data = load_status(args.status_json)
    img = render(data, now, args.stale_minutes)
    img.save(args.output_png)
    print(f"wrote {args.output_png} ({img.size[0]}x{img.size[1]}, mode {img.mode})")

    if args.preview:
        preview_path = str(Path(args.output_png).with_name("preview.png"))
        img.resize((WIDTH * 2, HEIGHT * 2), Image.Resampling.NEAREST).convert("L").save(preview_path)
        print(f"wrote {preview_path}")

    if args.raw:
        # Raw 1bpp buffer for the firmware: PIL's mode "1" packs bits MSB-first,
        # 1=white/0=black, byte-aligned rows — this happens to be exactly the
        # format GxEPD2's writeImage() expects. 800/8=100 bytes/row * 480 rows
        # = 48000 bytes, no padding needed since 800 is byte-aligned.
        raw_path = str(Path(args.output_png).with_suffix(".bin"))
        Path(raw_path).write_bytes(img.tobytes())
        print(f"wrote {raw_path} ({len(img.tobytes())} bytes)")


if __name__ == "__main__":
    main()