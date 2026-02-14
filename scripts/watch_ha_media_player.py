import argparse
import asyncio
import sys
import time
import hashlib
from dataclasses import replace
from io import BytesIO
from pathlib import Path

from PIL import Image

project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.append(str(project_root))

from bk_light.config import AppConfig, image_options, load_config
from bk_light.panel_manager import PanelManager
from bk_light.home_assistant import HomeAssistantWS, ha_fetch_entity_picture_bytes, normalize_base_url


def prepare_image_obj(
    image: Image.Image,
    canvas: tuple[int, int],
    mode: str,
    rotate: int,
    mirror: bool,
    invert: bool,
) -> Image.Image:
    # Import inside to avoid adding a hard dependency for users that only want WS detection.
    from PIL import ImageOps

    image = image.convert("RGB")
    if rotate:
        image = image.rotate(rotate % 360, expand=False)
    if mirror:
        image = ImageOps.mirror(image)
    if invert:
        image = ImageOps.invert(image)
    if mode == "fit":
        image = ImageOps.fit(image, canvas, method=Image.Resampling.LANCZOS)
    elif mode == "cover":
        image = ImageOps.fit(image, canvas, method=Image.Resampling.BICUBIC)
    else:
        image = image.resize(canvas, Image.Resampling.LANCZOS)
    return image


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
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
        help="Delay (seconds) between BLE handshake stages / after frame send. Try 0.05 for less multi-panel lag. Default: 0.2",
    )

    parser.add_argument("--ha-base-url", required=True, help="e.g. https://ha.local:8123 (no trailing slash)")
    parser.add_argument("--ha-token", required=True, help="Home Assistant long-lived access token")
    parser.add_argument("--ha-entity", required=True, help="e.g. media_player.living_room")
    parser.add_argument("--ha-insecure-ssl", action="store_true", help="Disable TLS verification (not recommended)")

    parser.add_argument(
        "--min-interval",
        type=float,
        default=1.0,
        help="Minimum seconds between panel updates (throttle). Default: 1.0",
    )
    parser.add_argument(
        "--reconnect-delay",
        type=float,
        default=2.0,
        help="Seconds to wait before reconnecting HA WebSocket after an error. Default: 2.0",
    )
    return parser.parse_args()


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


async def run_watch(config: AppConfig, args: argparse.Namespace) -> None:
    base_url = normalize_base_url(args.ha_base_url)
    token = args.ha_token
    entity_id = args.ha_entity
    reconnect_delay = max(0.5, float(args.reconnect_delay))
    min_interval = max(0.0, float(args.min_interval))

    preset_name = args.preset or config.runtime.preset or "default"
    overrides = build_override_map(args)
    preset = image_options(config, preset_name, overrides)
    ble_delay = max(0.0, float(args.ble_delay))

    print(f"ble_delay: {ble_delay}")

    # Overrides win, else preset defaults.
    mode = overrides.get("mode") or preset.mode
    rotate = int(overrides["rotate"]) if "rotate" in overrides else preset.rotate
    mirror = bool(overrides["mirror"]) if "mirror" in overrides else preset.mirror
    invert = bool(overrides["invert"]) if "invert" in overrides else preset.invert

    last_sent_at = 0.0
    last_hash: str | None = None

    async with PanelManager(config) as manager:
        canvas = manager.canvas_size

        async def maybe_update() -> None:
            nonlocal last_sent_at, last_hash

            now = time.monotonic()
            if min_interval and (now - last_sent_at) < min_interval:
                return

            img_bytes = await asyncio.to_thread(
                ha_fetch_entity_picture_bytes,
                base_url,
                token,
                entity_id,
                insecure_ssl=bool(args.ha_insecure_ssl),
            )
            h = hashlib.sha256(img_bytes).hexdigest()
            if last_hash == h:
                return

            pil = Image.open(BytesIO(img_bytes))
            image = prepare_image_obj(pil, canvas, str(mode), rotate, mirror, invert)
            await manager.send_image(image, delay=ble_delay)

            last_hash = h
            last_sent_at = time.monotonic()

        # Prime once at startup.
        await maybe_update()

        while True:
            try:
                client = HomeAssistantWS(base_url, token)
                async for _ev in client.subscribe_state_changed(entity_id=entity_id):
                    await maybe_update()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[ha-ws] error: {e}; reconnecting in {reconnect_delay:.1f}s")
                await asyncio.sleep(reconnect_delay)


if __name__ == "__main__":
    args = parse_args()
    config = load_config(args.config)
    if args.address:
        config.device = replace(config.device, address=args.address)
    asyncio.run(run_watch(config, args))


