"""systemd sd_notify over $NOTIFY_SOCKET. No-op when the socket is absent."""

from __future__ import annotations

import os
import socket


class Notifier:
    def __init__(self, path: str | None = None) -> None:
        self.path = path if path is not None else os.environ.get("NOTIFY_SOCKET")

    def send(self, message: str) -> None:
        family = getattr(socket, "AF_UNIX", None)
        if not self.path or family is None:
            return
        address = self.path
        if address.startswith("@"):
            address = "\0" + address[1:]
        sock = socket.socket(family, socket.SOCK_DGRAM)
        try:
            sock.sendto(message.encode(), address)
        except OSError:
            pass
        finally:
            sock.close()

    def ready(self) -> None:
        self.send("READY=1")

    def watchdog(self) -> None:
        self.send("WATCHDOG=1")

    def status(self, text: str) -> None:
        self.send(f"STATUS={text}")

    def stopping(self) -> None:
        self.send("STOPPING=1")
