"""Minimal Chrome DevTools Protocol client (stdlib only).

Used by the S01 UI E2E to type into the real React app and verify the note reaches
the backend. Deliberately small: WebSocket handshake + text frames + CDP request
correlation. No third-party dependency, no network access beyond loopback.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import time
import urllib.request


class CdpError(RuntimeError):
    pass


class WebSocket:
    def __init__(self, url: str, *, timeout: float = 10.0) -> None:
        if not url.startswith("ws://"):
            raise CdpError(f"unsupported websocket url: {url}")
        remainder = url[5:]
        host_port, _, path = remainder.partition("/")
        host, _, port_text = host_port.partition(":")
        port = int(port_text or 80)
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Origin: http://localhost\r\n"
            "\r\n"
        )
        self.sock.sendall(request.encode())
        response = self._read_http_headers()
        if "101" not in response.split("\r\n", 1)[0]:
            raise CdpError(f"websocket handshake failed: {response.splitlines()[0] if response else 'empty'}")

    def _read_http_headers(self) -> str:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            data += chunk
        return data.decode("latin-1", "replace")

    def _recv_exact(self, count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = self.sock.recv(count - len(data))
            if not chunk:
                raise CdpError("websocket closed")
            data += chunk
        return data

    def send_text(self, text: str) -> None:
        payload = text.encode()
        frame = bytearray([0x81])
        length = len(payload)
        if length < 126:
            frame.append(0x80 | length)
        elif length < 65536:
            frame.append(0x80 | 126)
            frame += struct.pack(">H", length)
        else:
            frame.append(0x80 | 127)
            frame += struct.pack(">Q", length)
        mask = os.urandom(4)
        frame += mask
        frame += bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(frame))

    def _send_control(self, opcode: int, payload: bytes = b"") -> None:
        mask = os.urandom(4)
        frame = bytearray([0x80 | opcode, 0x80 | len(payload)])
        frame += mask
        frame += bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(frame))

    def recv_text(self, timeout: float = 10.0) -> str:
        deadline = time.monotonic() + timeout
        fragments: list[bytes] = []
        while True:
            if time.monotonic() > deadline:
                raise CdpError("websocket receive timeout")
            header = self._recv_exact(2)
            fin = header[0] & 0x80
            opcode = header[0] & 0x0F
            masked = header[1] & 0x80
            length = header[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length) if length else b""
            if masked:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))

            if opcode == 0x9:  # ping
                self._send_control(0xA, payload)
                continue
            if opcode == 0x8:  # close
                raise CdpError("websocket closed by server")
            if opcode in (0x1, 0x0):
                fragments.append(payload)
                if fin:
                    return b"".join(fragments).decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self._send_control(0x8)
        except OSError:
            pass
        self.sock.close()


class CdpSession:
    def __init__(self, websocket_url: str, *, timeout: float = 15.0) -> None:
        self.ws = WebSocket(websocket_url, timeout=timeout)
        self._id = 0

    def call(self, method: str, params: dict | None = None, *, timeout: float = 15.0) -> dict:
        self._id += 1
        message_id = self._id
        self.ws.send_text(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        while True:
            raw = self.ws.recv_text(timeout=timeout)
            message = json.loads(raw)
            if message.get("id") == message_id:
                if "error" in message:
                    raise CdpError(f"{method}: {message['error']}")
                return message.get("result", {})

    def evaluate(self, expression: str, *, timeout: float = 15.0) -> object:
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            timeout=timeout,
        )
        if result.get("exceptionDetails"):
            raise CdpError(f"javascript error: {result['exceptionDetails']}")
        return result.get("result", {}).get("value")

    def navigate(self, url: str, *, timeout: float = 30.0) -> None:
        self.call("Page.navigate", {"url": url}, timeout=timeout)

    def wait_for(self, expression: str, *, timeout: float = 20.0, interval: float = 0.2) -> object:
        deadline = time.monotonic() + timeout
        last: object = None
        while time.monotonic() < deadline:
            last = self.evaluate(expression)
            if last:
                return last
            time.sleep(interval)
        raise CdpError(f"condition not met within {timeout}s: {expression} (last={last!r})")

    def close(self) -> None:
        self.ws.close()


def page_websocket_url(debug_port: int, *, timeout: float = 20.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json/list", timeout=2) as response:
                targets = json.loads(response.read().decode())
            for target in targets:
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                    return target["webSocketDebuggerUrl"]
        except OSError:
            pass
        time.sleep(0.25)
    raise CdpError("no debuggable Chrome page target appeared")
