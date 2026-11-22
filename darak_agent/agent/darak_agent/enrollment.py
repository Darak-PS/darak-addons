"""One-time device enrollment bundles and persistent agent identity."""

import base64
import binascii
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID


class EnrollmentError(ValueError):
    pass


@dataclass(frozen=True)
class Bundle:
    home_id: str
    slug: str
    hostname: str
    hub_hostname: str
    signing_key: str = field(repr=False)
    tailscale_auth_key: str = field(repr=False)
    expires_at: str


@dataclass(frozen=True)
class State:
    home_id: str
    slug: str
    hostname: str
    hub_hostname: str
    signing_key: str = field(repr=False)
    enrolled_at: str


_BUNDLE_KEYS = {"v", "home_id", "slug", "hostname", "hub_hostname", "signing_key",
                "tailscale_auth_key", "expires_at"}
_STATE_KEYS = {"home_id", "slug", "hostname", "hub_hostname", "signing_key", "enrolled_at"}
_SLUG = re.compile(r"[a-z][a-z0-9-]{1,31}")
_HUB = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


def _valid_identity(value, *, state=False):
    keys = _STATE_KEYS if state else _BUNDLE_KEYS
    if not isinstance(value, dict) or value.keys() != keys:
        raise ValueError
    if not state and (type(value["v"]) is not int or value["v"] != 1):
        raise ValueError
    home_id, slug = value["home_id"], value["slug"]
    if not isinstance(home_id, str) or str(UUID(home_id)) != home_id:
        raise ValueError
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
        raise ValueError
    if value["hostname"] != "darak-home-" + slug:
        raise ValueError
    if not isinstance(value["hub_hostname"], str) or not _HUB.fullmatch(value["hub_hostname"]):
        raise ValueError
    if not isinstance(value["signing_key"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["signing_key"]):
        raise ValueError
    stamp = value["enrolled_at" if state else "expires_at"]
    if not isinstance(stamp, str) or not _STAMP.fullmatch(stamp):
        raise ValueError
    parsed = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    if state:
        return parsed
    auth = value["tailscale_auth_key"]
    if not isinstance(auth, str) or not 1 <= len(auth) <= 200 or any(
        ord(char) < 33 or ord(char) > 126 for char in auth
    ):
        raise ValueError
    return parsed


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def parse_bundle(text, *, now):
    try:
        if not isinstance(text, str) or not text.startswith("darak1."):
            raise ValueError
        encoded = text[7:]
        if not encoded or not re.fullmatch(r"[A-Za-z0-9_-]+", encoded) or len(encoded) % 4 == 1:
            raise ValueError
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        value = json.loads(raw, object_pairs_hook=_object)
        expires = _valid_identity(value)
        if now.tzinfo is None or expires <= now.astimezone(timezone.utc):
            raise TimeoutError
        return Bundle(value["home_id"], value["slug"], value["hostname"], value["hub_hostname"],
                      value["signing_key"], value["tailscale_auth_key"], value["expires_at"])
    except TimeoutError:
        raise EnrollmentError("Enrollment bundle expired") from None
    except (ValueError, TypeError, AttributeError, UnicodeError, binascii.Error, RecursionError):
        raise EnrollmentError("Invalid enrollment bundle") from None


def _state_from(value):
    _valid_identity(value, state=True)
    return State(value["home_id"], value["slug"], value["hostname"], value["hub_hostname"],
                 value["signing_key"], value["enrolled_at"])


def load_state(path):
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    try:
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError
        return _state_from(json.loads(path.read_text(), object_pairs_hook=_object))
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise EnrollmentError("Invalid agent state") from None


def write_state(path, state):
    value = {name: getattr(state, name) for name in ("home_id", "slug", "hostname", "hub_hostname",
                                                      "signing_key", "enrolled_at")}
    _state_from(value)
    write_private_json(path, value)


def write_private_json(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix="." + path.name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, separators=(",", ":")))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def apply_bundle(bundle, state_path, *, now):
    enrolled = now.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = State(bundle.home_id, bundle.slug, bundle.hostname, bundle.hub_hostname,
                  bundle.signing_key, enrolled)
    write_state(state_path, state)
    return state
