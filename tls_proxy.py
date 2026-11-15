#!/usr/bin/env python3
"""
Dual HTTP / HTTPS socket multiplexer for Sonorius (used by run_app.sh when TLS is enabled).

Inspects the first byte of each incoming TCP connection on the external port (default 8501):
- 0x16 (TLS ClientHello): terminates TLS using the configured certificate/key and forwards
  requests to the internal loopback Streamlit server with `X-Forwarded-Proto: https`.
- Plain HTTP request methods: forwards directly to the internal loopback Streamlit server
  with `X-Forwarded-Proto: http` (allowing local loopback access at http://localhost:8501
  while app.py's enforce_client_transport_security() blocks plain HTTP from non-loopback hosts).
"""

import asyncio
import re
import signal
import socket
import ssl
import subprocess
import sys

HTTP_METHODS = (b"GET ", b"POST ", b"HEAD ", b"PUT ", b"DELETE ", b"OPTIONS ", b"PATCH ")
XFP_RE = re.compile(rb"(?im)^x-forwarded-proto:[^\r\n]*\r?\n")


def _pick_free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def wait_peek(loop: asyncio.AbstractEventLoop, sock: socket.socket) -> bytes:
    fut = loop.create_future()
    fd = sock.fileno()

    def on_readable():
        loop.remove_reader(fd)
        try:
            data = sock.recv(1, socket.MSG_PEEK)
            if not fut.done():
                fut.set_result(data)
        except BlockingIOError:
            loop.add_reader(fd, on_readable)
        except Exception as exc:
            if not fut.done():
                fut.set_exception(exc)

    try:
        data = sock.recv(1, socket.MSG_PEEK)
        return data
    except BlockingIOError:
        pass

    loop.add_reader(fd, on_readable)
    try:
        return await asyncio.wait_for(fut, timeout=15.0)
    finally:
        loop.remove_reader(fd)


async def wrap_accepted_socket(
    loop: asyncio.AbstractEventLoop,
    sock: socket.socket,
    ssl_ctx: ssl.SSLContext | None = None,
):
    reader = asyncio.StreamReader(limit=65536, loop=loop)
    protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
    transport, _ = await loop.connect_accepted_socket(
        lambda: protocol, sock, ssl=ssl_ctx
    )
    writer = asyncio.StreamWriter(transport, protocol, reader, loop)
    return reader, writer


async def handle_client(
    loop: asyncio.AbstractEventLoop,
    client_sock: socket.socket,
    ssl_ctx: ssl.SSLContext,
    internal_port: int,
):
    client_writer = None
    backend_writer = None
    try:
        client_sock.setblocking(False)
        try:
            client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

        first_byte = await wait_peek(loop, client_sock)
        if not first_byte:
            client_sock.close()
            return

        is_tls = first_byte == b"\x16"
        client_reader, client_writer = await wrap_accepted_socket(
            loop, client_sock, ssl_ctx if is_tls else None
        )

        # Connect to internal Streamlit server (with brief retry during startup)
        for attempt in range(20):
            try:
                backend_reader, backend_writer = await asyncio.open_connection(
                    "127.0.0.1", internal_port
                )
                break
            except OSError:
                if attempt == 19:
                    raise
                await asyncio.sleep(0.15)

        proto_hdr = b"X-Forwarded-Proto: https\r\n" if is_tls else b"X-Forwarded-Proto: http\r\n"

        async def pipe_c2b():
            upgraded = False
            try:
                while True:
                    chunk = await client_reader.read(65536)
                    if not chunk:
                        break
                    if not upgraded and chunk.startswith(HTTP_METHODS) and b"\r\n" in chunk:
                        req_line, rest = chunk.split(b"\r\n", 1)
                        if b" HTTP/1." in req_line:
                            if b"\r\n\r\n" in rest:
                                hdrs, body = rest.split(b"\r\n\r\n", 1)
                                hdrs = XFP_RE.sub(b"", hdrs)
                                if b"upgrade: websocket" in hdrs.lower():
                                    upgraded = True
                                chunk = req_line + b"\r\n" + proto_hdr + hdrs + b"\r\n\r\n" + body
                            else:
                                chunk = req_line + b"\r\n" + proto_hdr + rest
                    backend_writer.write(chunk)
                    await backend_writer.drain()
            except Exception:
                pass
            finally:
                try:
                    backend_writer.close()
                except Exception:
                    pass

        async def pipe_b2c():
            try:
                while True:
                    chunk = await backend_reader.read(65536)
                    if not chunk:
                        break
                    client_writer.write(chunk)
                    await client_writer.drain()
            except Exception:
                pass
            finally:
                try:
                    client_writer.close()
                except Exception:
                    pass

        await asyncio.gather(pipe_c2b(), pipe_b2c())
    except Exception:
        try:
            client_sock.close()
        except Exception:
            pass
    finally:
        for w in (backend_writer, client_writer):
            if w is not None:
                try:
                    w.close()
                except Exception:
                    pass


async def main():
    if len(sys.argv) < 6:
        sys.exit("Usage: tls_proxy.py <streamlit_bin> <bind_addr> <port> <cert_file> <key_file>")

    streamlit_bin, bind_addr, port_str, cert_file, key_file = sys.argv[1:6]
    ext_port = int(port_str)
    internal_port = _pick_free_loopback_port()

    proc = subprocess.Popen(
        [
            streamlit_bin,
            "run",
            "app.py",
            "--server.address",
            "127.0.0.1",
            "--server.port",
            str(internal_port),
            "--server.headless",
            "true",
        ]
    )

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _shutdown():
        stop_event.set()
        if proc.poll() is None:
            proc.terminate()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass

    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ssl_ctx.load_cert_chain(certfile=cert_file, keyfile=key_file)

    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind((bind_addr, ext_port))
    server_sock.listen(256)
    server_sock.setblocking(False)

    active_tasks: set[asyncio.Task] = set()

    async def accept_loop():
        while not stop_event.is_set():
            try:
                client_sock, _ = await loop.sock_accept(server_sock)
                task = asyncio.create_task(
                    handle_client(loop, client_sock, ssl_ctx, internal_port)
                )
                active_tasks.add(task)
                task.add_done_callback(active_tasks.discard)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(0.05)

    accept_task = asyncio.create_task(accept_loop())
    try:
        while not stop_event.is_set():
            if proc.poll() is not None:
                break
            await asyncio.sleep(0.5)
    finally:
        accept_task.cancel()
        server_sock.close()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    asyncio.run(main())
