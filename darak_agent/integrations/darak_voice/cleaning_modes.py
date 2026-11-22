"""Vacuum cleaning mode detection and override validation.

This module deliberately has no Home Assistant imports so the detection rules
can be exercised in a plain Python test environment.
"""

import re
from collections.abc import Mapping

DIMENSIONS = {
    "suction": ("quiet", "normal", "strong", "max"),
    "type": ("vacuum", "mop", "vacuum_and_mop", "smart", "smart_deep"),
    "water": ("low", "medium", "high"),
}
SMART_TYPES = ("smart", "smart_deep")
FAN_SPEED = 32
_ENTITY_ID = re.compile(r"^(select|vacuum)\.[a-z0-9_]{1,128}$")
_ROOM_SELECT = re.compile(r"_room_\d+_")
_IGNORED_FAN = {"off", "custom", "smart", "mop", "gentle_mop"}
_QUIET = {"silent", "quiet", "eco", "low", "gentle"}
_NORMAL = {"standard", "balanced", "normal", "medium", "automatic", "auto"}
_MAX = {"max_plus", "max", "turbo", "performance", "ultra", "full", "high", "strong"}

_TABLE = {
    ("dreame_vacuum", "cleaning_mode"): ("type", {
        "vacuum": "sweeping", "mop": "mopping", "vacuum_and_mop": "sweeping_and_mopping"}),
    ("dreame_vacuum", "mop_pad_humidity"): ("water", {
        "low": "slightly_dry", "medium": "moist", "high": "wet"}),
    ("dreame_vacuum", "water_volume"): ("water", {"low": "low", "medium": "medium", "high": "high"}),
    ("dreame_vacuum", "cleangenius"): ("smart_mode_off", {"off": "off"}),
    ("roborock", "cleaning_mode"): ("type", {
        "vacuum": "vacuum", "mop": "mop", "vacuum_and_mop": "vac_and_mop"}),
    ("roborock", "water_flow"): ("water", {"low": "low", "medium": "medium", "high": "high"}),
    ("ecovacs", "work_mode"): ("type", {
        "vacuum": "vacuum", "mop": "mop", "vacuum_and_mop": "vacuum_and_mop"}),
    ("ecovacs", "water_amount"): ("water", {"low": "low", "medium": "medium", "high": "high"}),
}
_DREAME_SMART = {"smart": "routine_cleaning", "smart_deep": "deep_cleaning"}
_FALLBACK_OPTIONS = {
    ("dreame_vacuum", "cleaning_mode"): ("sweeping", "mopping", "sweeping_and_mopping"),
    ("dreame_vacuum", "mop_pad_humidity"): ("slightly_dry", "moist", "wet"),
    ("dreame_vacuum", "water_volume"): ("low", "medium", "high"),
    ("dreame_vacuum", "cleangenius"): ("off", "routine_cleaning", "deep_cleaning"),
    ("roborock", "cleaning_mode"): ("vacuum", "mop", "vac_and_mop"),
    ("roborock", "water_flow"): ("low", "medium", "high"),
    ("roborock", "mop_intensity"): ("mild", "moderate", "intense"),
    ("ecovacs", "work_mode"): ("vacuum", "mop", "vacuum_and_mop"),
    ("ecovacs", "water_amount"): ("low", "medium", "high", "ultrahigh"),
}


def _norm(value):
    return str(value).strip().casefold().replace("+", "_plus").replace("-", "").replace(" ", "")


def _empty_profile():
    return {dimension: {name: [] for name in names} for dimension, names in DIMENSIONS.items()} | {
        "smart_mode_off": []}


def _assignment(entity_id, option):
    return [{"entity_id": entity_id, "option": option}]


def is_room_select(platform, unique_id=None, entity_id=None):
    """Return whether a Dreame select belongs to a per-room setting."""
    if platform != "dreame_vacuum":
        return False
    value = unique_id if isinstance(unique_id, str) and unique_id else entity_id
    return isinstance(value, str) and _ROOM_SELECT.search(value) is not None


def _matches(options, words):
    words = {_norm(word) for word in words}
    return next((option for option in options if _norm(option) in words), None)


def _selects_of(selects, platform, key):
    return [item for item in selects if item.get("platform") == platform
            and item.get("translation_key") == key]


def detect_profile(vacuum_state_attrs, selects, vacuum_entity_id=None):
    """Return a complete detected profile from vacuum attributes and device selects."""
    attrs = vacuum_state_attrs if isinstance(vacuum_state_attrs, Mapping) else {}
    selects = [item for item in selects if isinstance(item, dict)
               and not is_room_select(item.get("platform"), item.get("unique_id"), item.get("entity_id"))]
    profile = _empty_profile()

    speeds = attrs.get("fan_speed_list")
    if (isinstance(attrs.get("supported_features"), int)
            and not isinstance(attrs.get("supported_features"), bool)
            and attrs["supported_features"] & FAN_SPEED
            and isinstance(speeds, list) and speeds):
        candidates = [name for name in speeds if isinstance(name, str) and _norm(name) not in _IGNORED_FAN]
        quiet = _matches(candidates, _QUIET)
        normal = _matches(candidates, _NORMAL)
        max_speed = next((name for name in reversed(candidates) if _norm(name) in _MAX), None)
        strong = None
        if max_speed is not None:
            index = candidates.index(max_speed)
            if index > 0 and candidates[index - 1] not in (quiet, normal):
                strong = candidates[index - 1]
        if not any((quiet, normal, strong, max_speed)):
            n = len(candidates)
            if n >= 4:
                quiet, normal, strong, max_speed = candidates[0], candidates[1], candidates[-2], candidates[-1]
            elif n == 3:
                quiet, normal, max_speed = candidates
            elif n == 2:
                normal, max_speed = candidates
        for name, option in (("quiet", quiet), ("normal", normal), ("strong", strong), ("max", max_speed)):
            if option is not None:
                profile["suction"][name] = _assignment(vacuum_entity_id or "vacuum.unknown", option)

    for item in selects:
        platform, key, entity_id = item.get("platform"), item.get("translation_key"), item.get("entity_id")
        options = item.get("options") if isinstance(item.get("options"), list) else []
        if (item.get("state") == "unavailable"
                or (len(options) == 1 and isinstance(options[0], str)
                    and _norm(options[0]) == "unavailable")):
            options = list(_FALLBACK_OPTIONS.get((platform, key), ()))
        if not isinstance(entity_id, str) or not entity_id.startswith("select."):
            continue
        table = _TABLE.get((platform, key))
        if table:
            dimension, mapping = table
            if (platform, key) == ("dreame_vacuum", "water_volume") and _selects_of(
                    selects, "dreame_vacuum", "mop_pad_humidity"):
                continue
            for canonical, known_option in mapping.items():
                if dimension == "smart_mode_off":
                    profile[dimension] = _assignment(entity_id, known_option)
                    # Only Dreame's option names are known; other brands get no smart type.
                    for smart, smart_option in _DREAME_SMART.items():
                        option = _matches(options, {smart_option})
                        if option is not None:
                            profile["type"][smart] = _assignment(entity_id, option)
                elif canonical == "high" and platform == "ecovacs" and key == "water_amount":
                    option = _matches(options, {known_option})
                    if option is not None and _norm(option) == "ultrahigh":
                        continue
                    if option is not None:
                        profile[dimension][canonical] = _assignment(entity_id, option)
                else:
                    option = _matches(options, {known_option})
                    if option is not None:
                        profile[dimension][canonical] = _assignment(entity_id, option)

    # Matter options are user-facing labels and use first matching label per type.
    for item in selects:
        if (item.get("platform"), item.get("translation_key")) != ("matter", "clean_mode"):
            continue
        entity_id = item.get("entity_id")
        if not isinstance(entity_id, str) or not entity_id.startswith("select."):
            continue
        for option in item.get("options", ()):
            label = _norm(option)
            vacuum = "vacuum" in label or "sweep" in label
            mop = "mop" in label
            canonical = "vacuum_and_mop" if vacuum and mop else "mop" if mop else "vacuum" if vacuum else None
            if canonical and not profile["type"][canonical]:
                profile["type"][canonical] = _assignment(entity_id, option)

    # Roborock mop intensity is a legacy combined control, used absent cleaning_mode.
    if not _selects_of(selects, "roborock", "cleaning_mode"):
        for item in _selects_of(selects, "roborock", "mop_intensity"):
            entity_id = item.get("entity_id")
            options = item.get("options") if isinstance(item.get("options"), list) else []
            if not isinstance(entity_id, str):
                continue
            water_words = {"low": {"mild", "low"}, "medium": {"moderate", "medium", "standard"},
                           "high": {"intense", "high"}}
            for name, words in water_words.items():
                option = _matches(options, words)
                if option is not None:
                    profile["water"][name] = _assignment(entity_id, option)
            medium = profile["water"]["medium"]
            if medium:
                off_option = _matches(options, {"off"})
                if off_option is not None:
                    profile["type"]["vacuum"] = _assignment(entity_id, off_option)
                off = _matches(speeds if isinstance(speeds, list) else [], {"off"})
                if off:
                    profile["type"]["mop"] = _assignment(vacuum_entity_id or "vacuum.unknown", off) + medium
                profile["type"]["vacuum_and_mop"] = medium
    return profile


def _replace_vacuum(recipe, vacuum_entity_id):
    return [{"entity_id": vacuum_entity_id if item["entity_id"] == "vacuum.unknown" else item["entity_id"],
             "option": item["option"]} for item in recipe]


def effective_profile(detected, override, vacuum_entity_id=None):
    detected = detected if isinstance(detected, dict) else _empty_profile()
    override = override if validate_override(override) else {}
    result = _empty_profile()
    for dimension, names in DIMENSIONS.items():
        for name in names:
            recipe = override.get(dimension, {}).get(name, detected.get(dimension, {}).get(name, []))
            result[dimension][name] = _replace_vacuum(recipe, vacuum_entity_id) if vacuum_entity_id else recipe
    recipe = override.get("smart_mode_off", detected.get("smart_mode_off", []))
    result["smart_mode_off"] = _replace_vacuum(recipe, vacuum_entity_id) if vacuum_entity_id else recipe
    return result


def catalog_names(effective):
    return {dimension: [name for name in names
                        if isinstance(effective, dict)
                        and isinstance(effective.get(dimension), dict)
                        and effective[dimension].get(name)]
            for dimension, names in DIMENSIONS.items()}


def controls(vacuum_entity_id, vacuum_attrs, vacuum_name, select_entries, states):
    """Build bounded editor controls from registry entries and current states."""
    result = []
    attrs = vacuum_attrs if isinstance(vacuum_attrs, Mapping) else {}
    features = attrs.get("supported_features")
    if isinstance(features, int) and not isinstance(features, bool) and features & FAN_SPEED:
        options = attrs.get("fan_speed_list")
        if not isinstance(options, list):
            options = []
        result.append({"entity_id": vacuum_entity_id, "name": vacuum_name or vacuum_entity_id,
                       "options": [value for value in options[:60] if isinstance(value, str)],
                       "state": attrs.get("fan_speed") if isinstance(attrs.get("fan_speed"), str) else None})
    for entry in select_entries:
        entity_id = getattr(entry, "entity_id", None)
        if is_room_select(getattr(entry, "platform", None), getattr(entry, "unique_id", None), entity_id):
            continue
        state = states.get(entity_id) if isinstance(entity_id, str) else None
        state_value = getattr(state, "state", None) if state else None
        state_attrs = getattr(state, "attributes", {}) if state else {}
        options = state_attrs.get("options", []) if isinstance(state_attrs, Mapping) else []
        if not isinstance(options, list):
            options = []
        platform, key = getattr(entry, "platform", None), getattr(entry, "translation_key", None)
        if (state_value == "unavailable"
                or (len(options) == 1 and isinstance(options[0], str)
                    and _norm(options[0]) == "unavailable")):
            options = list(_FALLBACK_OPTIONS.get((platform, key), ()))
        name = getattr(entry, "name", None) or getattr(state, "name", None) or entity_id
        result.append({"entity_id": entity_id, "name": str(name)[:255],
                       "options": [value for value in options[:60] if isinstance(value, str)],
                       "state": state_value if isinstance(state_value, str) else None})
        if len(result) >= 60:
            break
    return result[:60]


def async_setup_cleaning_modes(hass):
    """Register the admin-only websocket command."""
    import voluptuous as vol
    from homeassistant.components import websocket_api

    schema = {vol.Required("type"): "darak_voice/cleaning_modes",
              vol.Required("entity_id"): str}

    @websocket_api.websocket_command(schema)
    @websocket_api.require_admin
    @websocket_api.async_response
    async def _websocket_cleaning_modes(hass, connection, msg):
        from homeassistant.helpers import entity_registry as er

        entity_id = msg["entity_id"]
        vacuum_state = hass.states.get(entity_id)
        entities = er.async_get(hass)
        vacuum_entry = entities.async_get(entity_id)
        if not entity_id.startswith("vacuum.") or vacuum_state is None or vacuum_entry is None:
            connection.send_error(msg["id"], "not_found", "Entity not found")
            return
        device_id = getattr(vacuum_entry, "device_id", None)
        select_entries = [entry for entry in entities.entities.values()
                          if entry.entity_id.startswith("select.") and device_id
                          and getattr(entry, "device_id", None) == device_id
                          and not is_room_select(getattr(entry, "platform", None),
                                                 getattr(entry, "unique_id", None), entry.entity_id)]
        select_data = [{"entity_id": entry.entity_id, "platform": entry.platform,
                        "translation_key": entry.translation_key,
                        "state": (hass.states.get(entry.entity_id).state
                                  if hass.states.get(entry.entity_id) else None),
                        "options": (hass.states.get(entry.entity_id).attributes.get("options", [])
                                    if hass.states.get(entry.entity_id) else []),
                        "name": entry.name} for entry in select_entries]
        attrs = vacuum_state.attributes
        detected = detect_profile(attrs, select_data, entity_id)
        stored = getattr(vacuum_entry, "options", {})
        override_domain = stored.get("darak_vacuum_modes", {}) if isinstance(stored, Mapping) else {}
        override = override_domain.get("modes", {}) if isinstance(override_domain, Mapping) else {}
        if not validate_override(override):
            override = {}
        effective = effective_profile(detected, override, entity_id)
        controls_list = controls(entity_id, attrs, getattr(vacuum_state, "name", None),
                                 select_entries, hass.states)
        connection.send_result(msg["id"], {"entity_id": entity_id, "detected": detected,
                                           "override": override, "effective": effective,
                                           "controls": controls_list})

    websocket_api.async_register_command(hass, _websocket_cleaning_modes)


def validate_override(obj):
    if not isinstance(obj, dict) or set(obj) - {*DIMENSIONS, "smart_mode_off"}:
        return False
    for dimension in DIMENSIONS:
        if dimension not in obj:
            continue
        values = obj[dimension]
        if not isinstance(values, dict) or set(values) - set(DIMENSIONS[dimension]):
            return False
        if any(not _valid_recipe(recipe) for recipe in values.values()):
            return False
    if "smart_mode_off" in obj and not _valid_recipe(obj["smart_mode_off"]):
        return False
    return True


def _valid_recipe(recipe):
    return (isinstance(recipe, list) and len(recipe) <= 3
            and all(isinstance(item, dict) and set(item) == {"entity_id", "option"}
                    and isinstance(item["entity_id"], str) and _ENTITY_ID.fullmatch(item["entity_id"])
                    and isinstance(item["option"], str) and 1 <= len(item["option"]) <= 100
                    and item["option"].isprintable()
                    for item in recipe))
