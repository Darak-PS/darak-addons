"""Strict configuration with a private, per-home HMAC signing key."""

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from .enrollment import EnrollmentError, load_state


class ConfigError(ValueError):
    def __init__(self):
        super().__init__("Invalid agent configuration")


@dataclass(frozen=True)
class AgentConfig:
    home_id: str = ""
    signing_key: str = field(default="", repr=False)
    enrollment_bundle: str = field(default="", repr=False)
    hub_hostname: str = "darak-hub"
    tailnet_hostname: str = ""
    listen_host: str = "127.0.0.1"
    listen_port: int = 8099
    ha_upstream_host: str = "homeassistant"
    ha_upstream_port: int = 8123
    ha_forward_port: int = 8123
    voice_forward_port: int = 10300
    voice_hub_port: int = 10300
    socks5_port: int = 1055
    supervisor_url: str = "http://supervisor"
    supervisor_token: str = field(default="", repr=False)
    support_sessions: str = "allow"

    def __post_init__(self):
        try:
            if not isinstance(self.home_id, str):
                raise ValueError
            if self.home_id:
                if str(UUID(self.home_id)) != self.home_id:
                    raise ValueError
            elif not self.enrollment_bundle:
                raise ValueError
            if not isinstance(self.signing_key, str) or (
                self.signing_key and not re.fullmatch(r"[0-9a-f]{64}", self.signing_key)
            ):
                raise ValueError
            if not isinstance(self.enrollment_bundle, str):
                raise ValueError
            if not isinstance(self.hub_hostname, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", self.hub_hostname):
                raise ValueError
            if not isinstance(self.tailnet_hostname, str) or (self.tailnet_hostname and not re.fullmatch(
                r"darak-home-[a-z][a-z0-9-]{1,31}", self.tailnet_hostname
            )):
                raise ValueError
            for port in (self.listen_port, self.ha_upstream_port, self.ha_forward_port,
                         self.voice_forward_port, self.voice_hub_port, self.socks5_port):
                if type(port) is not int or not 1 <= port <= 65535:
                    raise ValueError
            for host in (self.listen_host, self.ha_upstream_host):
                if not isinstance(host, str) or not host:
                    raise ValueError
                try:
                    ip_address(host)
                except ValueError:
                    if not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?", host):
                        raise ValueError
            if not isinstance(self.supervisor_url, str):
                raise ValueError
            url = urlsplit(self.supervisor_url)
            if (url.scheme not in {"http", "https"} or not url.hostname
                    or url.username is not None or url.password is not None
                    or url.path not in {"", "/"} or url.query or url.fragment
                    or any(char.isspace() for char in self.supervisor_url)):
                raise ValueError
            if url.port is not None and not 1 <= url.port <= 65535:
                raise ValueError
            if not isinstance(self.supervisor_token, str) or any(
                ord(char) < 33 or ord(char) > 126 for char in self.supervisor_token
            ):
                raise ValueError
            if self.support_sessions not in {"allow", "ask", "block"}:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ConfigError() from None


def load_config(options_path: str | Path = "/data/options.json", *, state_path: str | Path = "/data/darak.json",
                environ: Mapping[str, str] | None = None) -> AgentConfig:
    environ = os.environ if environ is None else environ
    allowed = {"home_id", "signing_key", "listen_host", "listen_port", "supervisor_url",
               "enrollment_bundle", "hub_hostname", "tailnet_hostname", "ha_upstream_host",
               "ha_upstream_port", "ha_forward_port", "voice_forward_port", "voice_hub_port", "socks5_port",
               "support_sessions"}
    try:
        try:
            options = json.loads(Path(options_path).read_text())
        except FileNotFoundError:
            options = {}
        if not isinstance(options, dict) or options.keys() - allowed:
            raise ValueError
        values = dict(options)
        state = load_state(state_path)
        if state is not None:
            values.update(home_id=state.home_id, signing_key=state.signing_key,
                          hub_hostname=state.hub_hostname, tailnet_hostname=state.hostname)
        for name in allowed - {"enrollment_bundle"}:
            key = "DARAK_AGENT_" + name.upper()
            if key in environ:
                value = environ[key]
                if name.endswith("_port"):
                    if not re.fullmatch(r"[0-9]{1,5}", value):
                        raise ValueError
                    value = int(value)
                values[name] = value
        if not values.get("home_id") and not values.get("enrollment_bundle"):
            raise ValueError
        return AgentConfig(**values, supervisor_token=environ.get("SUPERVISOR_TOKEN", ""))
    except EnrollmentError:
        raise
    except (OSError, ValueError, TypeError, RecursionError):
        raise ConfigError() from None
