import asyncio
import socket
import sys

import uvicorn

from . import access, config
from .main import app


def _port_busy(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return False
        except OSError:
            return True


async def main() -> None:
    busy = [p for h, p in [("127.0.0.1", config.PORT)] + ([("0.0.0.0", config.HTTPS_PORT)] if config.LAN else [])
            if _port_busy(h, p)]
    if busy:
        print(f"\n  JARVIS can't start: port {', '.join(map(str, busy))} is already in use, most likely by JARVIS itself "
              f"running in another window.\n  Open http://localhost:{config.PORT}, or close the other window and try again.\n")
        sys.exit(1)
    servers = [uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=config.PORT, timeout_keep_alive=75))]
    print(f"\n  YOLO-JARVIS on this PC:   http://localhost:{config.PORT}", flush=True)
    if config.LAN:
        from . import certs

        cert, key = certs.ensure()
        # Same app, so the background jobs started by the first server serve both.
        servers.append(uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=config.HTTPS_PORT, ssl_certfile=cert,
                                                     ssl_keyfile=key, timeout_keep_alive=75, lifespan="off")))
        print(f"  Phone / other devices:    {access.link() or 'no network found'}", flush=True)
        print("  That link is the key to this PC. Only open it on your own devices.\n", flush=True)
    await asyncio.gather(*(s.serve() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
