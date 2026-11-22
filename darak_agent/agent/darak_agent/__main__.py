"""Enroll once, start userspace Tailscale, and serve the agent API."""

import asyncio
import json
import logging
import os
import signal
from datetime import timedelta
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

from .config import ConfigError, load_config
from .enrollment import EnrollmentError, apply_bundle, load_state, parse_bundle
from .forward import Forwarder, direct, via_socks5
from .server import create_app
from .supervisor import clear_enrollment_option, default_transport
from .supervisor import SupervisorClient
from .tech_sessions import ssh_close
from .consent import disable_with_consent, drain_pushes, end_if_blocked, end_ssh_session, expire_pending, revoke_session
from .ingress import create_ingress_app
from .tailscale import TailscaleManager
from .tailscale import TailscaleError

LOGGER = logging.getLogger("darak_agent.startup")
INGRESS_PORT = 8098
SSH_SERVER_CLOSE_TIMEOUT = 5
SHUTDOWN_TIMEOUT = 5


async def _bounded_cleanup(awaitable, event):
    task = asyncio.ensure_future(awaitable)
    done, _ = await asyncio.wait({task}, timeout=SHUTDOWN_TIMEOUT)
    if task not in done:
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=1)
    if task in done:
        try:
            task.result()
        except BaseException:
            pass
    else:
        LOGGER.warning('{"event": "%s_cleanup_timeout"}', event)


async def _stop_forwarder(forwarder, server):
    server.close()
    if hasattr(forwarder, "abort_clients"):
        forwarder.abort_clients()
    try:
        await asyncio.wait_for(server.wait_closed(), timeout=SHUTDOWN_TIMEOUT)
    except TimeoutError:
        LOGGER.warning('{"event": "forwarder_close_timeout"}')
    if hasattr(forwarder, "wait_clients"):
        await forwarder.wait_clients(SHUTDOWN_TIMEOUT)


TAILSCALE_STATE_DIR = "/data/tailscale"


def _state_or_none(path):
    try:
        return load_state(path)
    except EnrollmentError:
        return None  # load_config reports an unreadable state; a fresh bundle replaces it


async def core_port(client, default):
    """Core's HTTP port from the Supervisor.

    Home Assistant OS 18 installs run Core on port 80 and answer 8123 only with a
    redirect; older installs use 8123. Any failure keeps the configured default.
    """
    try:
        port = (await client.get_json("/core/info")).get("port")
    except Exception:
        return default
    return port if type(port) is int and 0 < port < 65536 else default


async def run(*, options_path="/data/options.json", state_path="/data/darak.json", environ=None,
              transport=default_transport, tailscale_factory=TailscaleManager,
              runner_factory=web.AppRunner, site_factory=web.TCPSite, shutdown_event=None, now=None,
              forwarder_factory=Forwarder, forward_server_factory=asyncio.start_server,
              session_clock=None, session_interval=30, tech_path="/data/tech.json", ssh_path="/data/ssh.json",
              client_factory=SupervisorClient):
    environ = os.environ if environ is None else environ
    try:
        options = json.loads(Path(options_path).read_text())
    except FileNotFoundError:
        options = {}
    except (OSError, ValueError, RecursionError):
        options = {}
    if not isinstance(options, dict):
        options = {}
    bundle_text = options.get("enrollment_bundle", "")
    auth_key = None
    reauth = False
    if not bundle_text and not environ.get("DARAK_AGENT_HOME_ID") and load_state(state_path) is None:
        raise EnrollmentError("Not enrolled: paste the enrollment bundle from Darak admin into "
                              "this add-on's configuration, then start it")
    if bundle_text:
        now = now or datetime.now(timezone.utc)
        try:
            bundle = parse_bundle(bundle_text, now=now)
        except EnrollmentError:
            if load_state(state_path) is None:
                raise
            LOGGER.warning('{"event": "enrollment_bundle_ignored"}')
        else:
            previous = _state_or_none(state_path)
            apply_bundle(bundle, state_path, now=now)
            auth_key = bundle.tailscale_auth_key
            # A bundle for another control server (the move to Headscale) joins it as a new node.
            reauth = previous is not None and previous.login_server != bundle.login_server
    state = _state_or_none(state_path)
    login_server = state.login_server if state is not None else None

    config = load_config(options_path, state_path=state_path, environ=environ)
    if auth_key is not None:
        config = replace(config, enrollment_bundle="")
    manager = tailscale_factory(TAILSCALE_STATE_DIR, "/var/run/tailscale/tailscaled.sock")
    runner = None
    ingress_runner = None
    forward_servers = []
    ssh_server = None
    sweep = None
    waiter = None
    client = client_factory(config.supervisor_url, config.supervisor_token, transport,
                              ha_upstream_host=config.ha_upstream_host,
                              ha_upstream_port=config.ha_upstream_port,
                              voice_forward_port=config.voice_forward_port, tech_path=tech_path, ssh_path=ssh_path,
                              session_clock=session_clock)
    async def start_ssh(host):
        nonlocal ssh_server
        if ssh_server is None:
            ssh_server = await forwarder_factory("ssh", direct(host, 22)).start(
                "127.0.0.1", 2222, server_factory=forward_server_factory)
    async def stop_ssh():
        nonlocal ssh_server
        server, ssh_server = ssh_server, None
        if server is not None:
            server.close()
            server.abort_clients()
            try:
                await asyncio.wait_for(server.wait_closed(), timeout=SSH_SERVER_CLOSE_TIMEOUT)
            except TimeoutError:
                LOGGER.warning('{"event": "ssh_forwarder_close_timeout"}')
    client.start_ssh_forwarder, client.stop_ssh_forwarder = start_ssh, stop_ssh
    async def end_sessions():
        if Path(tech_path).exists() or Path(ssh_path).exists():
            await revoke_session(client)
            await drain_pushes()
    try:
        if reauth:
            manager.forget()
        await manager.start()
        status = await manager.status()
        if status.get("backend_state") == "Running" and not reauth:
            await manager.up(config.tailnet_hostname, login_server=login_server)
        else:
            await manager.up(config.tailnet_hostname, auth_key, login_server=login_server)
        # Cleared only after joining, so a failed start keeps the bundle for a retry.
        if bundle_text:
            try:
                await clear_enrollment_option(
                    environ.get("DARAK_AGENT_SUPERVISOR_URL", options.get("supervisor_url", "http://supervisor")),
                    environ.get("SUPERVISOR_TOKEN", ""), transport,
                )
            except Exception:
                LOGGER.warning("Could not clear enrollment option")
        if "ha_upstream_port" not in options and "DARAK_AGENT_HA_UPSTREAM_PORT" not in environ:
            port = await core_port(client, config.ha_upstream_port)
            if port != config.ha_upstream_port:
                config = replace(config, ha_upstream_port=port)
                client.ha_upstream_port = port
        forwarders = (
            (forwarder_factory("ha", direct(config.ha_upstream_host, config.ha_upstream_port)),
             "127.0.0.1", config.ha_forward_port),
            (forwarder_factory("voice", via_socks5("127.0.0.1", config.socks5_port,
                                                    config.hub_hostname, config.voice_hub_port)),
             "0.0.0.0", config.voice_forward_port),
        )
        for forwarder, host, port in forwarders:
            forward_servers.append((forwarder, await forwarder.start(host, port,
                                                                      server_factory=forward_server_factory)))
        def current_time():
            return (session_clock or (lambda: datetime.now(timezone.utc)))()
        async def sweep_sessions():
            try:
                await expire_pending(client)
            except Exception:
                LOGGER.warning('{"event": "technician_request_expiry_failed"}')
            timestamp = current_time()
            try:
                saved = json.loads(Path(ssh_path).read_text())
                expiry = datetime.fromisoformat(saved["expires_at"].replace("Z", "+00:00"))
                if expiry > timestamp and ssh_server is None:
                    await start_ssh(saved["addon"].replace("_", "-"))
            except FileNotFoundError:
                pass
            except Exception:
                LOGGER.warning('{"event": "technician_session_restore_failed", "kind": "ssh"}')
            for path, kind, command in ((tech_path, "user", "tech_user_disable"),
                                        (ssh_path, "ssh", "ssh_close")):
                try:
                    state = json.loads(Path(path).read_text())
                    expiry = datetime.fromisoformat(state["expires_at"].replace("Z", "+00:00"))
                    if expiry <= timestamp:
                        if command == "ssh_close":
                            await end_ssh_session(client)
                        else:
                            await disable_with_consent(client)
                        LOGGER.info('{"event": "technician_session_expired", "kind": "%s"}', kind)
                except FileNotFoundError:
                    pass
                except Exception:
                    LOGGER.warning('{"event": "technician_session_expiry_failed", "kind": "%s"}', kind)
            try:
                # Add-on options are read live from the Supervisor, so a switch to block is seen here.
                if await end_if_blocked(client):
                    LOGGER.info('{"event": "technician_session_ended_by_block"}')
            except Exception:
                LOGGER.warning('{"event": "technician_block_check_failed"}')

        async def sessions():
            while True:
                await asyncio.sleep(session_interval)
                await sweep_sessions()
        await sweep_sessions()
        sweep = asyncio.create_task(sessions())
        runner = runner_factory(create_app(config, client=client,
                                           replay_path=Path(state_path).with_name("replay-cache.json")))
        await runner.setup()
        site = site_factory(runner, config.listen_host, config.listen_port)
        await site.start()
        ingress_runner = runner_factory(create_ingress_app(client))
        await ingress_runner.setup()
        ingress_site = site_factory(ingress_runner, "0.0.0.0", INGRESS_PORT)
        await ingress_site.start()
        event = shutdown_event or asyncio.Event()
        loop = asyncio.get_running_loop()
        installed = []
        if shutdown_event is None:
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(sig, event.set)
                    installed.append(sig)
                except NotImplementedError:
                    pass
        waiter = asyncio.create_task(event.wait())
        await asyncio.wait((waiter, sweep), return_when=asyncio.FIRST_COMPLETED)
        sweep.cancel()
        await _bounded_cleanup(asyncio.gather(sweep, return_exceptions=True), "session_sweep")
        waiter.cancel()
        await _bounded_cleanup(asyncio.gather(waiter, return_exceptions=True), "shutdown_waiter")
        for sig in installed:
            loop.remove_signal_handler(sig)
    finally:
        try:
            if sweep is not None and not sweep.done():
                sweep.cancel()
                await _bounded_cleanup(asyncio.gather(sweep, return_exceptions=True), "session_sweep")
            if waiter is not None and not waiter.done():
                waiter.cancel()
                await _bounded_cleanup(asyncio.gather(waiter, return_exceptions=True), "shutdown_waiter")
            # Technician access must not outlive the agent.
            await _bounded_cleanup(end_sessions(), "session_revoke")
            if ssh_server is not None:
                await stop_ssh()
            for forwarder, server in reversed(forward_servers):
                await _stop_forwarder(forwarder, server)
        finally:
            try:
                if ingress_runner is not None:
                    await _bounded_cleanup(ingress_runner.cleanup(), "ingress_runner")
            finally:
                try:
                    if runner is not None:
                        await _bounded_cleanup(runner.cleanup(), "runner")
                finally:
                    await manager.stop()
    return config


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        asyncio.run(run())
    except ConfigError:
        raise SystemExit("Invalid agent configuration") from None
    except EnrollmentError as error:
        raise SystemExit(str(error)) from None
    except TailscaleError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
