"""Userspace Tailscale process management."""

import asyncio
import ipaddress
import json
import os
import tempfile
from pathlib import Path


class TailscaleError(RuntimeError):
    pass


async def _run(*argv):
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    output = bytearray()

    async def collect():
        while chunk := await proc.stdout.read(8192):
            if len(output) < 65536:
                output.extend(chunk[:65536 - len(output)])
        return await proc.wait()

    try:
        code = await asyncio.wait_for(collect(), 60)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return code, bytes(output)


class TailscaleManager:
    def __init__(self, state_dir, socket_path, *, tailscaled="tailscaled", tailscale="tailscale",
                 run=None, spawn=None):
        self.state_dir = str(state_dir)
        self.socket_path = str(socket_path)
        self.tailscaled = tailscaled
        self.tailscale = tailscale
        self.run = run or _run
        self.spawn = spawn or self._spawn
        self.process = None

    async def _spawn(self, *argv):
        return await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.DEVNULL)

    def _socket_args(self):
        return [self.tailscale, "--socket=" + self.socket_path]

    async def start(self):
        Path(self.state_dir).mkdir(parents=True, exist_ok=True)
        self.process = await self.spawn(self.tailscaled, "--tun=userspace-networking",
                                        "--statedir=" + self.state_dir, "--socket=" + self.socket_path,
                                        "--socks5-server=127.0.0.1:1055", "--no-logs-no-support")
        deadline = asyncio.get_running_loop().time() + 30
        while (remaining := deadline - asyncio.get_running_loop().time()) > 0:
            try:
                code, output = await asyncio.wait_for(
                    self.run(*self._socket_args(), "status", "--json"), remaining)
            except TimeoutError:
                break
            try:
                status = json.loads(output) if code == 0 else None
                if isinstance(status, dict) and isinstance(status.get("BackendState"), str):
                    return
            except (ValueError, UnicodeError, RecursionError):
                pass
            await asyncio.sleep(min(1, max(0, deadline - asyncio.get_running_loop().time())))
        raise TailscaleError("tailscaled did not start")

    async def up(self, hostname, auth_key=None):
        args = [*self._socket_args(), "up", "--hostname=" + hostname,
                "--advertise-tags=tag:darak-home", "--accept-dns=false", "--accept-routes=false",
                "--ssh=false", "--timeout=45s"]
        temporary = None
        try:
            if auth_key is not None:
                fd, temporary = tempfile.mkstemp(dir=self.state_dir, prefix=".auth-")
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="ascii") as stream:
                    stream.write(auth_key)
                args.append("--auth-key=file:" + temporary)
            code, _ = await self.run(*args)
            if code:
                raise TailscaleError("tailscale up failed")
        finally:
            if temporary is not None:
                os.unlink(temporary)

    async def status(self):
        code, output = await self.run(*self._socket_args(), "status", "--json")
        try:
            payload = json.loads(output) if code == 0 else {}
        except (ValueError, UnicodeError, RecursionError):
            payload = {}
        state = payload.get("BackendState") if isinstance(payload, dict) else None
        # Before login, tailscaled reports "Self": null or "TailscaleIPs": null.
        peers = payload.get("Self") if isinstance(payload, dict) else None
        addresses = peers.get("TailscaleIPs") if isinstance(peers, dict) else None
        if not isinstance(addresses, list):
            addresses = []
        ip = next((str(address) for address in addresses if _tailnet_ip(address)), None)
        return {"backend_state": state, "tailnet_ip": ip}

    async def stop(self):
        if self.process is None or self.process.returncode is not None:
            return
        self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), 10)
        except TimeoutError:
            self.process.kill()
            await self.process.wait()


def _tailnet_ip(value):
    try:
        return ipaddress.ip_address(value) in ipaddress.ip_network("100.64.0.0/10")
    except (ValueError, TypeError):
        return False
