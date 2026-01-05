#!/usr/bin/env python3

import argparse
import asyncio
import sys
from pathlib import Path

try:
    import websockets
except ImportError:
    print("Error: websockets library required")
    print("Install with: pip install websockets")
    sys.exit(1)

project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from bk_light.display_session import BleDisplaySession, build_frame

HTML_FILE = Path(__file__).parent / "snake.html"
MIN_FRAME_INTERVAL = 0.08

panel = None
last_frame_time = 0


async def handle_websocket(websocket):
    global last_frame_time
    print(f"Client connected: {websocket.remote_address}")
    try:
        async for message in websocket:
            if isinstance(message, bytes) and panel:
                now = asyncio.get_event_loop().time()
                if now - last_frame_time >= MIN_FRAME_INTERVAL:
                    last_frame_time = now
                    frame = build_frame(message)
                    await panel.send_frame(frame, delay=0.1)
    except websockets.exceptions.ConnectionClosed:
        pass
    print(f"Client disconnected: {websocket.remote_address}")


async def handle_http(reader, writer):
    try:
        data = await reader.read(4096)
        request = data.decode("utf-8", errors="ignore")

        path = None
        for line in request.split("\n"):
            if line.startswith("GET "):
                parts = line.split()
                if len(parts) >= 2:
                    path = parts[1]
                break

        if path in ("/", "/snake.html"):
            if HTML_FILE.exists():
                content = HTML_FILE.read_bytes()
            else:
                content = b"<!DOCTYPE html><html><head><title>BLE Panel Server</title></head><body><h1>BLE Panel Server</h1><p>Server is running. snake.html not found.</p></body></html>"
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/html; charset=utf-8\r\n"
                b"Content-Length: " + str(len(content)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + content
            )
        else:
            response = b"HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\nNot found"

        writer.write(response)
        await writer.drain()
    except Exception as e:
        print(f"HTTP error: {e}")
    finally:
        writer.close()
        await writer.wait_closed()


async def main():
    global panel

    parser = argparse.ArgumentParser()
    parser.add_argument("address")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--ws-port", type=int, default=8765)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    panel = BleDisplaySession(
        address=args.address,
        auto_reconnect=True,
        log_notifications=args.verbose,
    )

    try:
        await panel._connect()
        print("Connected to panel")

        http_server = await asyncio.start_server(handle_http, "0.0.0.0", args.port)
        print(f"HTTP: http://localhost:{args.port}/")

        ws_server = await websockets.serve(handle_websocket, "0.0.0.0", args.ws_port)
        print(f"WebSocket: ws://localhost:{args.ws_port}/")

        await asyncio.gather(
            http_server.serve_forever(),
            ws_server.wait_closed(),
        )
    except KeyboardInterrupt:
        pass
    finally:
        await panel._safe_disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped")
