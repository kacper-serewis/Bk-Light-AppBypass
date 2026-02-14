import argparse
import asyncio
import contextlib
import hashlib
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Optional

from PIL import Image, ImageOps

project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.append(str(project_root))

from bk_light.config import AppConfig, image_options, load_config
from bk_light.home_assistant import (
    HomeAssistantWS,
    ha_fetch_entity_picture_bytes,
    ha_get_state,
    normalize_base_url,
)
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


def render_minecraft_clock_sprite(sprite_sheet: Image.Image, index: int) -> Image.Image:
    idx = max(0, min(SPRITE_COUNT - 1, int(index)))
    left = (idx % SPRITES_PER_ROW) * SPRITE_SIZE
    top = (idx // SPRITES_PER_ROW) * SPRITE_SIZE
    return sprite_sheet.crop((left, top, left + SPRITE_SIZE, top + SPRITE_SIZE)).convert("RGB")


def prepare_image_obj(
    image: Image.Image,
    canvas: tuple[int, int],
    mode: str,
    rotate: int,
    mirror: bool,
    invert: bool,
    fit_resample: Image.Resampling,
    cover_resample: Image.Resampling,
    scale_resample: Image.Resampling,
) -> Image.Image:
    image = image.convert("RGB")
    if rotate:
        image = image.rotate(rotate % 360, expand=False)
    if mirror:
        image = ImageOps.mirror(image)
    if invert:
        image = ImageOps.invert(image)
    if mode == "fit":
        image = ImageOps.fit(image, canvas, method=fit_resample)
    elif mode == "cover":
        image = ImageOps.fit(image, canvas, method=cover_resample)
    else:
        image = image.resize(canvas, scale_resample)
    return image


def build_override_map(args: argparse.Namespace) -> dict[str, object]:
    overrides: dict[str, object] = {}
    if args.mode:
        overrides["mode"] = args.mode
    if args.rotate is not None:
        overrides["rotate"] = args.rotate
    if args.mirror:
        overrides["mirror"] = True
    if args.invert:
        overrides["invert"] = True
    return overrides


def is_playing_state(state_obj: Optional[dict]) -> bool:
    print(f"state_obj: {state_obj}")
    if not state_obj:
        return False
    return str(state_obj.get("state", "")).lower() == "playing"


async def run_watch(config: AppConfig, args: argparse.Namespace) -> None:
    base_url = normalize_base_url(args.ha_base_url)
    token = args.ha_token
    entity_id = args.ha_entity
    reconnect_delay = max(0.5, float(args.reconnect_delay))
    min_interval = max(0.0, float(args.min_interval))
    clock_interval = max(0.05, float(args.clock_interval))
    ble_delay = max(0.0, float(args.ble_delay))
    tz = resolve_timezone(config, args.timezone)

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

    preset_name = args.preset or config.runtime.preset or "default"
    overrides = build_override_map(args)
    preset = image_options(config, preset_name, overrides)

    mode = str(overrides.get("mode") or preset.mode)
    rotate = int(overrides["rotate"]) if "rotate" in overrides else int(preset.rotate)
    mirror = bool(overrides["mirror"]) if "mirror" in overrides else bool(preset.mirror)
    invert = bool(overrides["invert"]) if "invert" in overrides else bool(preset.invert)
    clock_mode = args.clock_fill or mode

    last_sent_cover_at = 0.0
    last_cover_hash: Optional[str] = None
    last_clock_index: Optional[int] = None
    showing_cover = False
    send_lock = asyncio.Lock()

    async with PanelManager(config) as manager:
        canvas = manager.canvas_size

        async def send_cover(force: bool = False) -> None:
            nonlocal last_sent_cover_at, last_cover_hash, last_clock_index, showing_cover

            now = time.monotonic()
            # Only throttle repeated cover updates; mode transitions should be immediate.
            if not force and showing_cover and min_interval and (now - last_sent_cover_at) < min_interval:
                return

            img_bytes = await asyncio.to_thread(
                ha_fetch_entity_picture_bytes,
                base_url,
                token,
                entity_id,
                insecure_ssl=bool(args.ha_insecure_ssl),
            )
            current_hash = hashlib.sha256(img_bytes).hexdigest()
            if not force and showing_cover and last_cover_hash == current_hash:
                return

            pil = Image.open(BytesIO(img_bytes))
            image = prepare_image_obj(
                pil,
                canvas,
                mode,
                rotate,
                mirror,
                invert,
                fit_resample=Image.Resampling.LANCZOS,
                cover_resample=Image.Resampling.LANCZOS,
                scale_resample=Image.Resampling.LANCZOS,
            )
            async with send_lock:
                await manager.send_image(image, delay=ble_delay)
                last_cover_hash = current_hash
                last_sent_cover_at = time.monotonic()
                showing_cover = True
                last_clock_index = None

        async def send_clock(force: bool = False) -> None:
            nonlocal last_clock_index, showing_cover

            clock_index = get_clock_index(datetime.now(tz))
            if not force and clock_index == last_clock_index:
                return

            frame = render_minecraft_clock_sprite(sprite_sheet, clock_index)
            image = prepare_image_obj(
                frame,
                canvas,
                clock_mode,
                rotate,
                mirror,
                invert,
                fit_resample=Image.Resampling.BOX,
                cover_resample=Image.Resampling.BOX,
                scale_resample=Image.Resampling.BOX,
            )
            async with send_lock:
                await manager.send_image(image, delay=ble_delay)
                last_clock_index = clock_index
                showing_cover = False

        async def refresh_by_state(force_cover: bool = False) -> None:
            try:
                state = await asyncio.to_thread(
                    ha_get_state,
                    base_url,
                    token,
                    entity_id,
                    insecure_ssl=bool(args.ha_insecure_ssl),
                )
            except Exception as e:
                print(f"[ha] state fetch failed: {e}")
                await send_clock(force=False)
                return

            if is_playing_state(state):
                try:
                    await send_cover(force=force_cover or not showing_cover)
                except Exception as e:
                    print(f"[ha] cover fetch failed: {e}")
                    await send_clock(force=not showing_cover)
            else:
                await send_clock(force=showing_cover)

        async def clock_loop() -> None:
            while True:
                if not showing_cover:
                    await send_clock(force=False)
                await asyncio.sleep(clock_interval)

        clock_task = asyncio.create_task(clock_loop())
        try:
            await refresh_by_state(force_cover=True)
            while True:
                try:
                    client = HomeAssistantWS(base_url, token)
                    async for ev in client.subscribe_state_changed(entity_id=entity_id):
                        if is_playing_state(ev.new_state):
                            try:
                                await send_cover(force=not showing_cover)
                            except Exception as e:
                                print(f"[ha] cover update failed: {e}")
                        else:
                            await send_clock(force=showing_cover)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    print(f"[ha-ws] error: {e}; reconnecting in {reconnect_delay:.1f}s")
                    await refresh_by_state(force_cover=False)
                    await asyncio.sleep(reconnect_delay)
        finally:
            clock_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await clock_task


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Show HA media cover art while playing; otherwise display a Minecraft clock sprite."
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--address")
    parser.add_argument("--preset")
    parser.add_argument("--mode", choices=("scale", "fit", "cover"))
    parser.add_argument("--rotate", type=int)
    parser.add_argument("--mirror", action="store_true")
    parser.add_argument("--invert", action="store_true")
    parser.add_argument(
        "--ble-delay",
        type=float,
        default=0.2,
        help="Delay (seconds) between BLE handshake stages / after frame send. Default: 0.2",
    )
    parser.add_argument(
        "--ha-base-url",
        required=True,
        help="Home Assistant base URL, e.g. https://ha.local:8123 (no trailing slash)",
    )
    parser.add_argument("--ha-token", required=True, help="Home Assistant long-lived access token")
    parser.add_argument("--ha-entity", required=True, help="e.g. media_player.living_room")
    parser.add_argument("--ha-insecure-ssl", action="store_true", help="Disable TLS verification (not recommended)")
    parser.add_argument(
        "--min-interval",
        type=float,
        default=1.0,
        help="Minimum seconds between cover-art updates while already showing cover. Default: 1.0",
    )
    parser.add_argument(
        "--reconnect-delay",
        type=float,
        default=2.0,
        help="Seconds to wait before reconnecting HA WebSocket after an error. Default: 2.0",
    )
    parser.add_argument(
        "--sprite-sheet",
        type=Path,
        default=project_root / "clock.png",
        help="Path to Minecraft clock sprite sheet. Default: ./clock.png",
    )
    parser.add_argument(
        "--clock-fill",
        choices=("scale", "fit", "cover"),
        default="fit",
        help="How clock sprites are transformed to canvas. Default: fit",
    )
    parser.add_argument(
        "--clock-interval",
        type=float,
        default=1.0,
        help="Seconds between idle clock checks. Default: 1.0",
    )
    parser.add_argument("--timezone", help="Timezone name (e.g. Europe/Warsaw). Default: device.timezone")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    config = load_config(args.config)
    if args.address:
        config.device = replace(config.device, address=args.address)
    asyncio.run(run_watch(config, args))


