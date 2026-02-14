import argparse
import asyncio
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from PIL import Image, ImageOps

project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.append(str(project_root))

from bk_light.config import AppConfig, load_config
from bk_light.panel_manager import PanelManager

SPRITE_SIZE = 16
SPRITES_PER_ROW = 8
SPRITE_COUNT = 64


def resolve_timezone(config: AppConfig, override: Optional[str]) -> timezone:
    tz_name = override or config.device.timezone
    if not tz_name or tz_name == "auto":
        return datetime.now().astimezone().tzinfo or timezone.utc
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(tz_name)
    except Exception:
        return datetime.now().astimezone().tzinfo or timezone.utc


def get_clock_index(now: datetime) -> int:
    total_minutes = now.hour * 60 + now.minute
    normalized_minutes = (total_minutes + 720) % 1440
    return int(normalized_minutes / 1440 * SPRITE_COUNT)


def render_minecraft_clock_sprite(
    sprite_sheet: Image.Image, index: int
) -> Image.Image:
    idx = max(0, min(SPRITE_COUNT - 1, int(index)))
    left = (idx % SPRITES_PER_ROW) * SPRITE_SIZE
    top = (idx // SPRITES_PER_ROW) * SPRITE_SIZE
    return sprite_sheet.crop((left, top, left + SPRITE_SIZE, top + SPRITE_SIZE)).convert("RGB")


def prepare_image_obj(
    image: Image.Image,
    canvas: tuple[int, int],
    fill_mode: str,
    rotate: int,
    mirror: bool,
    invert: bool,
) -> Image.Image:
    print(f"fill_mode: {fill_mode}")

    image = image.convert("RGB")
    if rotate:
        image = image.rotate(rotate % 360, expand=False)
    if mirror:
        image = ImageOps.mirror(image)
    if invert:
        image = ImageOps.invert(image)
    if fill_mode == "fit":
        image = ImageOps.fit(image, canvas, method=Image.Resampling.BOX)
    elif fill_mode == "cover":
        image = ImageOps.fit(image, canvas, method=Image.Resampling.BICUBIC)
    else:
        image = image.resize(canvas, Image.Resampling.LANCZOS)
    return image


async def run_clock(config: AppConfig, args: argparse.Namespace) -> None:
    sheet_path = args.sprite_sheet
    if not sheet_path.exists():
        raise FileNotFoundError(f"Sprite sheet not found: {sheet_path}")

    sprite_sheet = Image.open(sheet_path).convert("RGBA")
    min_w = SPRITE_SIZE * SPRITES_PER_ROW
    min_h = SPRITE_SIZE * (SPRITE_COUNT // SPRITES_PER_ROW)
    if sprite_sheet.width < min_w or sprite_sheet.height < min_h:
        raise ValueError(
            f"Sprite sheet is too small ({sprite_sheet.width}x{sprite_sheet.height}). "
            f"Expected at least {min_w}x{min_h} for 8x8 sprites."
        )

    interval = float(args.interval)
    step = max(1, int(args.step))
    start_index = max(0, min(SPRITE_COUNT - 1, int(args.start_index)))
    fixed_index = max(0, min(SPRITE_COUNT - 1, int(args.index)))
    ble_delay = max(0.0, float(args.ble_delay))
    rotate = int(args.rotate) if args.rotate is not None else 0
    fill_mode = args.fill
    tz = resolve_timezone(config, args.timezone)

    async with PanelManager(config) as manager:
        canvas = manager.canvas_size
        last_index: Optional[int] = None
        cycle_index = start_index

        while True:
            if args.clock_mode == "index":
                current_index = fixed_index
            elif args.clock_mode == "cycle":
                current_index = cycle_index
                cycle_index = (cycle_index + step) % SPRITE_COUNT
            else:
                current_index = get_clock_index(datetime.now(tz))

            if args.clock_mode != "realtime" or current_index != last_index:
                frame = render_minecraft_clock_sprite(sprite_sheet, current_index)

                image = prepare_image_obj(
                    frame,
                    canvas,
                    fill_mode,
                    rotate,
                    bool(args.mirror),
                    bool(args.invert),
                )
                await manager.send_image(image, delay=ble_delay)
                last_index = current_index

            if args.once or args.clock_mode == "index":
                break

            await asyncio.sleep(interval)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render and send Minecraft clock frames from a 8x8 sprite sheet."
    )
    parser.add_argument(
        "--sprite-sheet",
        type=Path,
        default=project_root / "clock.png",
        help="Path to the 8x8 sprite sheet (16x16 per sprite). Default: ./clock.png",
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--address")
    parser.add_argument(
        "--clock-mode",
        choices=("realtime", "cycle", "index"),
        default="realtime",
        help="realtime = local time index, cycle = loop all 64 frames, index = send one fixed frame",
    )
    parser.add_argument(
        "--fill",
        choices=("scale", "fit", "cover"),
        default="fit",
        help="How the 16x16 sprite is transformed to canvas, same behavior as send_image.py",
    )
    parser.add_argument("--rotate", type=int)
    parser.add_argument("--mirror", action="store_true")
    parser.add_argument("--invert", action="store_true")
    parser.add_argument("--index", type=int, default=0, help="Frame index for --clock-mode index (0..63)")
    parser.add_argument(
        "--start-index",
        type=int,
        default=0,
        help="Starting frame for --clock-mode cycle (0..63)",
    )
    parser.add_argument("--step", type=int, default=1, help="Frame increment for --clock-mode cycle")
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Seconds between updates (realtime/cycle). Use 0.25 to match JS cycle speed.",
    )
    parser.add_argument("--timezone", help="Timezone name (e.g. Europe/Warsaw). Default: device.timezone")
    parser.add_argument(
        "--ble-delay",
        type=float,
        default=0.2,
        help="Delay (seconds) used by BLE frame send. Try 0.05 for less multi-panel lag. Default: 0.2",
    )
    parser.add_argument("--once", action="store_true", help="Send one frame and exit")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    config = load_config(args.config)
    if args.address:
        config.device = replace(config.device, address=args.address)
    try:
        asyncio.run(run_clock(config, args))
    except KeyboardInterrupt:
        pass

