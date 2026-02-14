import argparse
import asyncio
import sys
from dataclasses import replace
from pathlib import Path
from io import BytesIO
import json
import ssl
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen
from typing import Optional
from PIL import Image, ImageOps

project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.append(str(project_root))

from bk_light.config import AppConfig, image_options, load_config
from bk_light.panel_manager import PanelManager


def parse_bool(value: Optional[bool]) -> Optional[bool]:
    return value if value is not None else None


def prepare_image_obj(
    image: Image.Image,
    canvas: tuple[int, int],
    mode: str,
    rotate: int,
    mirror: bool,
    invert: bool,
) -> Image.Image:
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


def _normalize_base_url(base_url: str) -> str:
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("Home Assistant base URL must start with http:// or https://")
    return base_url


def _to_absolute_url(base_url: str, maybe_relative: str) -> str:
    if maybe_relative.startswith(("http://", "https://")):
        return maybe_relative
    # urljoin handles relative + absolute-path forms cleanly.
    return urljoin(base_url + "/", maybe_relative.lstrip("/"))


def _http_get_bytes(url: str, token: str, *, insecure_ssl: bool = False) -> bytes:
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "bk-light-send-image/1.0",
    }
    req = Request(url, headers=headers, method="GET")
    context = ssl._create_unverified_context() if insecure_ssl else None
    with urlopen(req, context=context, timeout=20) as resp:
        return resp.read()


def _ha_get_state(base_url: str, token: str, entity_id: str, *, insecure_ssl: bool = False) -> dict:
    url = _to_absolute_url(base_url, f"/api/states/{quote(entity_id)}")
    raw = _http_get_bytes(url, token, insecure_ssl=insecure_ssl)
    return json.loads(raw.decode("utf-8"))


def _ha_get_entity_picture_url(base_url: str, token: str, entity_id: str, *, insecure_ssl: bool = False) -> str:
    st = _ha_get_state(base_url, token, entity_id, insecure_ssl=insecure_ssl)
    attr = (st or {}).get("attributes") or {}
    rel = attr.get("entity_picture") or attr.get("entity_picture_local")
    if not rel:
        rel = f"/api/media_player_proxy/{quote(entity_id)}"
    return _to_absolute_url(base_url, rel)


async def send_image(
    config: AppConfig,
    source: Optional[Path],
    preset_name: str,
    overrides: dict[str, Optional[str]],
    *,
    ble_delay: float = 0.2,
    ha_base_url: Optional[str] = None,
    ha_token: Optional[str] = None,
    ha_entity: Optional[str] = None,
    ha_insecure_ssl: bool = False,
) -> None:
    preset = image_options(config, preset_name, overrides)
    rotate_override = overrides.get("rotate")
    mirror_override = overrides.get("mirror")
    invert_override = overrides.get("invert")
    mode = overrides.get("mode") or preset.mode
    rotate = int(rotate_override) if rotate_override is not None else preset.rotate
    mirror = bool(mirror_override) if mirror_override is not None else preset.mirror
    invert = bool(invert_override) if invert_override is not None else preset.invert
    async with PanelManager(config) as manager:
        canvas = manager.canvas_size
        if ha_entity:
            if not ha_base_url or not ha_token:
                raise ValueError("--ha-base-url and --ha-token are required when using --ha-entity")
            base_url = _normalize_base_url(ha_base_url)
            url = _ha_get_entity_picture_url(base_url, ha_token, ha_entity, insecure_ssl=ha_insecure_ssl)
            img_bytes = _http_get_bytes(url, ha_token, insecure_ssl=ha_insecure_ssl)
            pil = Image.open(BytesIO(img_bytes))
        else:
            if source is None:
                raise ValueError("Missing image path (or pass --ha-entity to fetch from Home Assistant)")
            pil = Image.open(source)
        image = prepare_image_obj(pil, canvas, mode, rotate, mirror, invert)
        await manager.send_image(image, delay=max(0.0, float(ble_delay)))
        await asyncio.sleep(0.2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", nargs="?", type=Path, help="Local image path (omit when using --ha-entity)")
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
    parser.add_argument(
        "--ha-base-url", help="Home Assistant base URL, e.g. https://ha.local:8123 (no trailing slash)"
    )
    parser.add_argument("--ha-token", help="Home Assistant long-lived access token")
    parser.add_argument("--ha-entity", help="Entity ID to fetch picture from, e.g. media_player.living_room")
    parser.add_argument(
        "--ha-insecure-ssl",
        action="store_true",
        help="Disable TLS certificate verification (useful for self-signed HA; not recommended)",
    )
    return parser.parse_args()


def build_override_map(args: argparse.Namespace) -> dict[str, Optional[str]]:
    overrides: dict[str, Optional[str]] = {}
    if args.mode:
        overrides["mode"] = args.mode
    if args.rotate is not None:
        overrides["rotate"] = str(args.rotate)
    if args.mirror:
        overrides["mirror"] = True
    if args.invert:
        overrides["invert"] = True
    return overrides


if __name__ == "__main__":
    args = parse_args()
    config = load_config(args.config)
    if args.address:
        config.device = replace(config.device, address=args.address)
    preset_name = args.preset or config.runtime.preset or "default"
    overrides = build_override_map(args)
    asyncio.run(
        send_image(
            config,
            args.image,
            preset_name,
            overrides,
            ha_base_url=args.ha_base_url,
            ha_token=args.ha_token,
            ha_entity=args.ha_entity,
            ha_insecure_ssl=args.ha_insecure_ssl,
            ble_delay=args.ble_delay,
        )
    )

