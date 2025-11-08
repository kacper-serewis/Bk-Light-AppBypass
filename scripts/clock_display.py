import argparse
import asyncio
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from PIL import Image, ImageDraw, ImageFont

project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.append(str(project_root))

from bk_light.config import AppConfig, clock_options, load_config
from bk_light.panel_manager import PanelManager


def parse_color(value: Optional[str]) -> Optional[tuple[int, int, int]]:
    if value is None:
        return None
    cleaned = value.replace("#", "").replace(" ", "")
    if "," in cleaned:
        parts = cleaned.split(",")
        return tuple(int(part) for part in parts[:3])
    if len(cleaned) == 6:
        return tuple(int(cleaned[i:i + 2], 16) for i in (0, 2, 4))
    raise ValueError("Invalid color")


def load_font(path: Optional[Path], size: int) -> ImageFont.ImageFont:
    if path is None:
        return ImageFont.load_default()
    try:
        return ImageFont.truetype(str(path), size)
    except Exception:
        return ImageFont.load_default()


def resolve_timezone(config: AppConfig, override: Optional[str]) -> timezone:
    tz_name = override or config.device.timezone
    if not tz_name or tz_name == "auto":
        return datetime.now().astimezone().tzinfo or timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tz_name)
    except Exception:
        return datetime.now().astimezone().tzinfo or timezone.utc


def build_clock_image(
    canvas: tuple[int, int],
    text: str,
    color: tuple[int, int, int],
    accent: tuple[int, int, int],
    background: tuple[int, int, int],
    font_path: Optional[Path],
    size: int,
    colon_visible: bool,
    antialias: bool,
) -> Image.Image:
    font = load_font(font_path, size)
    dummy = Image.new("L", (1, 1), 0)
    draw_dummy = ImageDraw.Draw(dummy)
    parts = text.split(":", 1)
    left_text = parts[0]
    right_text = parts[1] if len(parts) > 1 else ""

    def render_segment(segment: str) -> tuple[Image.Image, tuple[int, int, int, int]]:
        if not segment:
            empty = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
            return empty, (0, 0, 0, 0)
        bbox = draw_dummy.textbbox((0, 0), segment, font=font)
        width = max(1, bbox[2] - bbox[0])
        height = max(1, bbox[3] - bbox[1])
        mask_mode = "L" if antialias else "1"
        mask = Image.new(mask_mode, (width, height), 0)
        draw_mask = ImageDraw.Draw(mask)
        draw_mask.text((-bbox[0], -bbox[1]), segment, fill=255, font=font)
        if not antialias:
            mask = mask.convert("L")
        text_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        fill_layer = Image.new("RGBA", (width, height), (*color, 255))
        text_layer = Image.composite(fill_layer, text_layer, mask)
        return text_layer, bbox

    left_segment, left_bbox = render_segment(left_text)
    right_segment, right_bbox = render_segment(right_text)
    left_width = draw_dummy.textlength(left_text, font=font)
    right_width = draw_dummy.textlength(right_text, font=font)
    colon_width = draw_dummy.textlength(":", font=font) if right_text else 0
    total_width = max(1, int(round(left_width + colon_width + right_width)))
    max_height = max(left_segment.height, right_segment.height, 1)

    frame = Image.new("RGBA", canvas, tuple(background) + (255,))
    base_x = int((canvas[0] - total_width) // 2)
    base_y = int((canvas[1] - max_height) // 2)

    frame.alpha_composite(left_segment, (base_x - left_bbox[0], base_y - left_bbox[1]))
    if right_text:
        frame.alpha_composite(
            right_segment,
            (base_x + int(round(left_width + colon_width)) - right_bbox[0], base_y - right_bbox[1])
        )

    frame_rgb = frame.convert("RGB")
    if right_text:
        colon_center_x = base_x + left_width + colon_width / 2
        digit_bbox = draw_dummy.textbbox((0, 0), "0", font=font)
        digit_height = digit_bbox[3] - digit_bbox[1]
        baseline = base_y - digit_bbox[1] + digit_height / 2
        gap = digit_height * 0.35
        colon_column = int(round(colon_center_x))
        top = int(round(baseline - gap))
        bottom = int(round(baseline + gap))
        colon_column = max(0, min(canvas[0] - 1, colon_column))
        if colon_visible:
            for row in (top, bottom):
                if 0 <= row < canvas[1]:
                    frame_rgb.putpixel((colon_column, row), accent)
        else:
            for row in range(top, bottom + 1):
                if 0 <= row < canvas[1]:
                    frame_rgb.putpixel((colon_column, row), background)
    return frame_rgb


async def run_clock(config: AppConfig, preset_name: str, overrides: dict[str, Optional[str]]) -> None:
    preset = clock_options(config, preset_name, overrides)
    tz = resolve_timezone(config, overrides.get("timezone"))
    color = parse_color(overrides.get("color")) or parse_color(preset.color)
    accent = parse_color(overrides.get("accent")) or parse_color(preset.accent)
    background = parse_color(overrides.get("background")) or parse_color(preset.background)
    font_path = Path(overrides["font"]) if overrides.get("font") else Path(preset.font) if preset.font else None
    interval = preset.interval
    dot_flashing = preset.dot_flashing
    flash_period = preset.dot_flash_period
    last_stamp = ""
    last_colon = True
    loop = asyncio.get_running_loop()
    start_time = loop.time()
    try:
        async with PanelManager(config) as manager:
            canvas = manager.canvas_size
            while True:
                now = datetime.now(tz)
                if preset.format == "12h":
                    stamp = now.strftime("%I:%M")
                    if stamp.startswith("0"):
                        stamp = stamp[1:]
                else:
                    stamp = now.strftime("%H:%M")
                elapsed = loop.time() - start_time
                colon_visible = True
                if dot_flashing:
                    colon_visible = int(elapsed / flash_period) % 2 == 0
                if stamp != last_stamp or colon_visible != last_colon:
                    image = build_clock_image(
                        canvas,
                        stamp,
                        color,
                        accent,
                        background,
                        font_path,
                        preset.size,
                        colon_visible,
                        config.display.antialias_text,
                    )
                    await manager.send_image(image, delay=0.15)
                    last_stamp = stamp
                    last_colon = colon_visible
                await asyncio.sleep(interval)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        print("ERROR", str(error))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    parser.add_argument("--address")
    parser.add_argument("--preset")
    parser.add_argument("--timezone")
    parser.add_argument("--format", choices=("12h", "24h"))
    parser.add_argument("--color")
    parser.add_argument("--accent")
    parser.add_argument("--background")
    parser.add_argument("--font", type=Path)
    parser.add_argument("--size", type=int)
    parser.add_argument("--interval", type=float)
    parser.add_argument("--dot-flashing", choices=("on", "off"))
    parser.add_argument("--dot-flash-period", type=float)
    return parser.parse_args()


def build_override_map(args: argparse.Namespace) -> dict[str, Optional[str]]:
    overrides: dict[str, Optional[str]] = {
        "timezone": args.timezone,
        "format": args.format,
        "color": args.color,
        "accent": args.accent,
        "background": args.background,
        "font": str(args.font) if args.font else None,
        "size": args.size,
        "interval": args.interval,
        "dot_flash_period": args.dot_flash_period,
    }
    if args.dot_flashing == "on":
        overrides["dot_flashing"] = True
    elif args.dot_flashing == "off":
        overrides["dot_flashing"] = False
    return overrides


if __name__ == "__main__":
    args = parse_args()
    config = load_config(args.config)
    if args.address:
        config.device = replace(config.device, address=args.address)
    preset_name = args.preset or config.runtime.preset or "default"
    overrides = build_override_map(args)
    try:
        asyncio.run(run_clock(config, preset_name, overrides))
    except KeyboardInterrupt:
        pass

