"""Small bounded raw TCP forwarders."""

import asyncio
import logging

LOGGER = logging.getLogger("darak_agent.forward")
IDLE_TIMEOUT = 300


class ForwardError(Exception):
    def __init__(self):
        super().__init__("socks5 connect failed")


async def pipe(reader, writer, idle_timeout=IDLE_TIMEOUT):
    try:
        while True:
            try:
                async with asyncio.timeout(idle_timeout):
                    chunk = await reader.read(65536)
                    if not chunk:
                        break
                    writer.write(chunk)
                    await writer.drain()
            except TimeoutError:
                break
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    try:
        if writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, OSError):
        pass


async def socks5_open(reader, writer, host, port):
    try:
        encoded = host.encode("ascii")
        if not 1 <= len(encoded) <= 253 or type(port) is not int or not 1 <= port <= 65535:
            raise ForwardError()
        async with asyncio.timeout(10):
            writer.write(b"\x05\x01\x00")
            await writer.drain()
            if await reader.readexactly(2) != b"\x05\x00":
                raise ForwardError()
            writer.write(b"\x05\x01\x00\x03" + bytes((len(encoded),)) + encoded + port.to_bytes(2, "big"))
            await writer.drain()
            version, reply, reserved, atyp = await reader.readexactly(4)
            if version != 5 or reply != 0 or reserved != 0:
                raise ForwardError()
            if atyp == 1:
                await reader.readexactly(4)
            elif atyp == 3:
                await reader.readexactly((await reader.readexactly(1))[0])
            elif atyp == 4:
                await reader.readexactly(16)
            else:
                raise ForwardError()
            await reader.readexactly(2)
    except ForwardError:
        raise
    except (OSError, asyncio.IncompleteReadError, TimeoutError, UnicodeError, ValueError):
        raise ForwardError() from None


def direct(host, port):
    async def connect():
        try:
            async with asyncio.timeout(10):
                return await asyncio.open_connection(host, port)
        except (OSError, TimeoutError):
            raise ForwardError() from None
    return connect


def via_socks5(proxy_host, proxy_port, host, port):
    async def connect():
        try:
            async with asyncio.timeout(10):
                reader, writer = await asyncio.open_connection(proxy_host, proxy_port)
            await socks5_open(reader, writer, host, port)
            return reader, writer
        except Exception:
            if "writer" in locals():
                writer.close()
            raise ForwardError() from None
    return connect


async def _close(writer):
    writer.close()
    wait_closed = getattr(writer, "wait_closed", None)
    if wait_closed is not None:
        try:
            await asyncio.wait_for(wait_closed(), timeout=1)
        except (ConnectionError, OSError, TimeoutError):
            pass


class Forwarder:
    def __init__(self, name, connector, *, limit=32, idle_timeout=IDLE_TIMEOUT):
        self.name = name
        self.connector = connector
        self.limit = limit
        self.idle_timeout = idle_timeout
        self._active = 0
        self._tasks = set()
        self._writers = set()

    @property
    def active(self):
        return self._active

    async def handle(self, client_reader, client_writer):
        if self._active >= self.limit:
            await _close(client_writer)
            return
        self._active += 1
        task = asyncio.current_task()
        self._tasks.add(task)
        self._writers.add(client_writer)
        upstream_writer = None
        try:
            try:
                upstream_reader, upstream_writer = await self.connector()
            except Exception:
                await _close(client_writer)
                LOGGER.warning('{"forward": "%s", "event": "upstream_failed"}', self.name)
                return
            self._writers.add(upstream_writer)
            tasks = {asyncio.create_task(pipe(client_reader, upstream_writer, self.idle_timeout)),
                     asyncio.create_task(pipe(upstream_reader, client_writer, self.idle_timeout))}
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, *done, return_exceptions=True)
        finally:
            if upstream_writer is not None:
                self._writers.discard(upstream_writer)
                await _close(upstream_writer)
            self._writers.discard(client_writer)
            await _close(client_writer)
            self._tasks.discard(task)
            self._active -= 1

    def abort_clients(self):
        for writer in tuple(self._writers):
            writer.close()
        for task in tuple(self._tasks):
            if task is not asyncio.current_task():
                task.cancel()

    async def wait_clients(self, timeout):
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=timeout)

    async def start(self, host, port, *, server_factory=asyncio.start_server):
        return await server_factory(self.handle, host, port)
