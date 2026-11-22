"""Home Assistant automation context and Darak-owned automation commands."""

import asyncio
import json
import re
import statistics
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DROP_EVENT_DOMAINS = frozenset(("update", "sun", "zone", "persistent_notification", "event", "button", "number",
    "select", "text", "image", "camera", "tts", "stt", "conversation", "wake_word", "assist_satellite", "sensor"))
DROP_ENTITY_DOMAINS = DROP_EVENT_DOMAINS | frozenset(("automation", "script", "scene"))
KEEP_EVENT_DOMAINS = frozenset(("binary_sensor", "person", "light", "switch", "fan", "cover",
    "climate", "media_player", "lock", "vacuum", "input_boolean", "scene", "script", "automation",
    "alarm_control_panel", "water_heater", "humidifier", "valve"))
NOISY_EVENTS_PER_DAY = 100
# ponytail: flat 100/day noise threshold; make it per-domain if real routines get dropped
ACTIONABLE_DOMAINS = frozenset(("light", "switch", "fan", "cover", "climate", "media_player", "vacuum",
    "input_boolean", "scene", "script", "water_heater", "humidifier", "valve"))
ON_WHILE_AWAY_DOMAINS = frozenset(("light", "switch", "fan", "media_player", "climate", "humidifier", "water_heater"))
SEQUENCE_WINDOW_S = 120
PRESENCE_WINDOW_S = 600
ROUTINE_WINDOW_MIN = 60
MIN_PATTERN_DAYS = 3
MAX_PATTERNS = 40
MAX_ACTIONS_PER_DAY = 20
MIN_SEQUENCE_DELAY_S = 2
MIN_AWAY_S = 30 * 60
MIN_LIFT = 3.0
UNSETTLED_STATES = frozenset(("unavailable", "unknown"))
AUTOMATION_CONFIG_ID_RE = re.compile(r"darak_ai_[0-9a-f]{32}\Z")
AUTOMATION_CONFIG_KEYS = frozenset(("alias", "description", "triggers", "conditions", "actions", "mode"))
MAX_AUTOMATION_CONFIG_BYTES = 64 * 1024


async def _login(client):
    from .ha_auth import _login as login
    return await login(client)


def classify_source(entry):
    if entry.get("context_event_type") == "automation_triggered" or entry.get("context_domain") == "automation":
        return "automation"
    if entry.get("context_domain") == "script":
        return "script"
    if entry.get("context_user_id") is not None:
        return "user"
    if not any(key in entry for key in ("context_user_id", "context_event_type", "context_domain")):
        return "physical"
    return "other"


def _entity_domain(entity_id):
    return entity_id.partition(".")[0] if isinstance(entity_id, str) else ""


def build_entities(registry, devices, areas, states):
    devices_by_id = {item.get("id"): item for item in devices if isinstance(item, dict) and item.get("id")}
    areas_by_id = {item.get("area_id"): item.get("name") for item in areas
                   if isinstance(item, dict) and item.get("area_id") and isinstance(item.get("name"), str)}
    states_by_id = {item.get("entity_id"): item for item in states
                    if isinstance(item, dict) and isinstance(item.get("entity_id"), str)}
    entities = []
    for item in registry:
        if not isinstance(item, dict) or not isinstance(item.get("entity_id"), str):
            continue
        if item.get("disabled_by") is not None or item.get("hidden_by") is not None:
            continue
        entity_id = item["entity_id"]
        domain = _entity_domain(entity_id)
        if domain in DROP_ENTITY_DOMAINS:
            continue
        state = states_by_id.get(entity_id, {})
        attrs = state.get("attributes") if isinstance(state.get("attributes"), dict) else {}
        name = item.get("name") or item.get("original_name") or attrs.get("friendly_name")
        area_id = item.get("area_id")
        if not area_id and item.get("device_id"):
            device = devices_by_id.get(item["device_id"], {})
            area_id = device.get("area_id") if isinstance(device, dict) else None
        entities.append({"entity_id": entity_id, "name": name if isinstance(name, str) else None,
                         "domain": domain,
                         "device_class": attrs.get("device_class") if isinstance(attrs.get("device_class"), str) else None,
                         "area": areas_by_id.get(area_id)})
    entities.sort(key=lambda item: item["entity_id"])
    return entities[:1500], len(entities) > 1500


def _local_time(value, zone):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        parsed = datetime.fromtimestamp(value, timezone.utc)
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("timestamp")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(zone).isoformat(timespec="seconds")


def normalize_events(entries, zone):
    events = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entity_id, state = entry.get("entity_id"), entry.get("state")
        if not isinstance(entity_id, str) or state is None:
            continue
        if _entity_domain(entity_id) not in KEEP_EVENT_DOMAINS:
            continue
        timestamp = entry.get("when")
        if timestamp is None:
            timestamp = entry.get("time")
        try:
            local_time = _local_time(timestamp, zone)
        except (OverflowError, OSError, TypeError, ValueError):
            continue
        events.append({"t": local_time, "entity_id": entity_id, "state": str(state),
                       "source": classify_source(entry)})
    events.sort(key=lambda item: item["t"])
    return events


def filter_events(events, days):
    counts = {}
    for event in events:
        counts[event["entity_id"]] = counts.get(event["entity_id"], 0) + 1
    events = [event for event in events if counts[event["entity_id"]] <= NOISY_EVENTS_PER_DAY * days]
    return events[-8000:], len(events) > 8000


def build_events(entries, zone, days):
    return filter_events(normalize_events(entries, zone), days)


def _is_human_action(entity_id, source):
    return _entity_domain(entity_id) in ACTIONABLE_DOMAINS and source not in ("automation", "script", "restore")


def _hhmm(value):
    total = int(value + 0.5) % 1440
    return f"{total // 60:02d}:{total % 60:02d}"


def _median_hhmm(values):
    return _hhmm(statistics.median(values)) if values else None


def _minutes(moment):
    return moment.hour * 60 + moment.minute


def _is_on(domain, state):
    if domain == "media_player":
        return state in ("on", "playing")
    if domain in ("climate", "water_heater"):
        return state not in ("off", "unavailable", "unknown")
    return state == "on"


def _overlap(intervals, periods):
    i = j = 0
    total = 0.0
    hit = set()
    while i < len(intervals) and j < len(periods):
        low = max(intervals[i][0], periods[j][0])
        high = min(intervals[i][1], periods[j][1])
        if high > low:
            total += high - low
            hit.add(j)
        if intervals[i][1] < periods[j][1]:
            i += 1
        else:
            j += 1
    return total, len(hit)


def _presence(rows, person_states):
    # Returns (timeline with synthetic everyone-away/home events, away periods >= 30 min, people count).
    first = {}
    for row in rows:
        if _entity_domain(row[2]) == "person" and row[2] not in first:
            first[row[2]] = row[3]
    away = {person: state == "home" for person, state in first.items()}
    for person, state in (person_states or {}).items():
        if _entity_domain(person) == "person" and person not in away:
            away[person] = state != "home"
    total = len(away)
    away_count = sum(away.values())
    everyone = total > 0 and away_count == total
    timeline, periods, started = [], [], None
    for row in rows:
        timeline.append(row)
        if row[2] not in away:
            continue
        now_away = row[3] != "home"
        if now_away == away[row[2]]:
            continue
        away[row[2]] = now_away
        away_count += 1 if now_away else -1
        now_everyone = away_count == total
        if now_everyone == everyone:
            continue
        everyone = now_everyone
        timeline.append((row[0], row[1], "everyone", "away" if everyone else "home", "presence"))
        if everyone:
            started = row
        elif started is not None:
            periods.append((started, row))
            started = None
    if started is not None:
        periods.append((started, rows[-1]))
    counted = [(a[0], b[0], a[1], b[1]) for a, b in periods if b[0] - a[0] >= MIN_AWAY_S]
    return timeline, counted, total


def _sequences(timeline, busy):
    if not timeline or timeline[-1][0] <= timeline[0][0]:
        return []
    first_ts, last_ts = timeline[0][0], timeline[-1][0]
    action_total, trigger_total, pairs = {}, {}, {}
    covered, covered_to = {}, {}
    latest, window = {}, deque()
    for seq, row in enumerate(timeline):
        ts, moment, entity, state, source = row
        key = (entity, state)
        trigger_total[key] = trigger_total.get(key, 0) + 1
        reach = min(ts + (PRESENCE_WINDOW_S if entity == "everyone" else SEQUENCE_WINDOW_S), last_ts)
        low = max(ts, covered_to.get(key, ts))
        if reach > low:
            covered[key] = covered.get(key, 0) + reach - low
            covered_to[key] = reach
        while window and ts - window[0][0] > PRESENCE_WINDOW_S:
            _old_ts, old_seq, old_key = window.popleft()
            if latest[old_key][1] == old_seq:
                del latest[old_key]
        if _is_human_action(entity, source) and entity not in busy:
            action_total[key] = action_total.get(key, 0) + 1
            for (after, after_state), (after_ts, _seq) in latest.items():
                if after == entity:
                    continue
                delay = ts - after_ts
                if delay > (PRESENCE_WINDOW_S if after == "everyone" else SEQUENCE_WINDOW_S):
                    continue
                slot = pairs.setdefault((after, after_state, entity, state), ([], set()))
                slot[0].append(delay)
                slot[1].add(moment.date())
        latest[key] = (ts, seq)
        window.append((ts, seq, key))
    found = []
    for (after, after_state, entity, state), (delays, dates) in pairs.items():
        if len(dates) < MIN_PATTERN_DAYS:
            continue
        count = len(delays)
        delay = int(round(statistics.median(delays)))
        if delay < MIN_SEQUENCE_DELAY_S:
            continue
        coverage = covered.get((after, after_state), 0) / (last_ts - first_ts)
        if not coverage:
            continue
        lift = count / action_total[(entity, state)] / coverage
        if lift < MIN_LIFT:
            continue
        found.append({"after": after, "after_state": after_state, "entity_id": entity, "state": state,
                      "count": count, "days": len(dates), "median_delay_s": delay,
                      "share_of_action": round(count / action_total[(entity, state)], 2),
                      "share_of_trigger": round(count / trigger_total[(after, after_state)], 2),
                      "lift": round(lift, 1)})
    found.sort(key=lambda item: (-item["days"], -item["lift"], -item["count"], item["after"],
                                 item["after_state"], item["entity_id"], item["state"]))
    return found[:MAX_PATTERNS]


def _routines(rows, busy):
    occurrences = {}
    for _ts, moment, entity, state, source in rows:
        if _is_human_action(entity, source) and entity not in busy:
            occurrences.setdefault((entity, state), []).append((_minutes(moment), moment.date()))
    found = []
    for (entity, state), items in occurrences.items():
        items.sort()
        counts, end, best, best_start = {}, 0, 0, None
        # ponytail: no midnight wrap; routines around 00:00 split in two
        for index, (start, _date) in enumerate(items):
            if index:
                gone = items[index - 1][1]
                counts[gone] -= 1
                if not counts[gone]:
                    del counts[gone]
            while end < len(items) and items[end][0] <= start + ROUTINE_WINDOW_MIN:
                counts[items[end][1]] = counts.get(items[end][1], 0) + 1
                end += 1
            if index and start == items[index - 1][0]:
                continue
            if len(counts) > best:
                best, best_start = len(counts), start
        if best < MIN_PATTERN_DAYS:
            continue
        first = {}
        for minute, date in items:
            if best_start <= minute <= best_start + ROUTINE_WINDOW_MIN:
                first.setdefault(date, minute)
        minutes = list(first.values())
        found.append({"entity_id": entity, "state": state, "days": len(first),
                      "median_time": _median_hhmm(minutes), "window_start": _hhmm(min(minutes)),
                      "window_end": _hhmm(max(minutes)),
                      "weekdays": sum(date.weekday() < 5 for date in first),
                      "weekend_days": sum(date.weekday() >= 5 for date in first)})
    found.sort(key=lambda item: (-item["days"], item["entity_id"], item["state"]))
    return found[:MAX_PATTERNS]


def _on_while_away(rows, periods, active):
    if not periods:
        return []
    spans = [(start, end) for start, end, _a, _b in periods]
    end_ts = rows[-1][0]
    intervals, since = {}, {}
    for ts, _moment, entity, state, _source in rows:
        domain = _entity_domain(entity)
        if domain not in ON_WHILE_AWAY_DOMAINS or entity not in active:
            continue
        if entity in since:
            intervals.setdefault(entity, []).append((since.pop(entity), ts))
        if _is_on(domain, state):
            since[entity] = ts
    for entity, ts in since.items():
        intervals.setdefault(entity, []).append((ts, end_ts))
    found = []
    for entity, items in intervals.items():
        total, hit = _overlap(items, spans)
        if hit >= 2:
            found.append({"entity_id": entity, "periods": hit, "hours": round(total / 3600, 1)})
    found.sort(key=lambda item: (-item["hours"], item["entity_id"]))
    return found[:20]


def ignored_entities(registry):
    return {item["entity_id"] for item in registry
            if isinstance(item, dict) and isinstance(item.get("entity_id"), str)
            and (item.get("disabled_by") is not None or item.get("hidden_by") is not None
                 or item.get("entity_category") is not None)}


def build_patterns(events, days, person_states, ignored):
    rows = []
    for event in events:
        try:
            moment = datetime.fromisoformat(event["t"])
        except (KeyError, TypeError, ValueError):
            continue
        if event["entity_id"] in ignored and _entity_domain(event["entity_id"]) != "person":
            continue
        rows.append((moment.timestamp(), moment, event["entity_id"], event["state"], event["source"]))
    rows.sort(key=lambda row: row[0])
    # A change into or out of unavailable/unknown is a reconnect or restart, never a person.
    previous = {}
    for index, row in enumerate(rows):
        before, previous[row[2]] = previous.get(row[2]), row[3]
        if before in UNSETTLED_STATES or row[3] in UNSETTLED_STATES:
            rows[index] = row[:4] + ("restore",)
    rate = {}
    for row in rows:
        if _is_human_action(row[2], row[4]):
            rate[row[2]] = rate.get(row[2], 0) + 1
    busy = {entity for entity, count in rate.items() if count > MAX_ACTIONS_PER_DAY * days}
    timeline, periods, people = _presence(rows, person_states)
    presence = None
    if people:
        spans = {True: ([], []), False: ([], [])}
        for _s, _e, start, end in periods:
            leave, back = spans[start.weekday() < 5]
            leave.append(_minutes(start))
            back.append(_minutes(end))
        presence = {"people": people, "away_periods": len(periods),
                    "away_days": len({start.date() for _s, _e, start, _end in periods}),
                    "weekday_leave_median": _median_hhmm(spans[True][0]),
                    "weekday_return_median": _median_hhmm(spans[True][1]),
                    "weekend_leave_median": _median_hhmm(spans[False][0]),
                    "weekend_return_median": _median_hhmm(spans[False][1])}
    return {"events_analysed": len(rows), "sequences": _sequences(timeline, busy), "routines": _routines(rows, busy),
            "presence": presence, "on_while_away": _on_while_away(rows, periods, {entity for entity, count in rate.items() if count >= 2} - busy) if people else []}


def build_automations(states, configs):
    automations = []
    for state in states:
        if not isinstance(state, dict):
            continue
        entity_id = state.get("entity_id")
        attrs = state.get("attributes") if isinstance(state.get("attributes"), dict) else {}
        ident = attrs.get("id")
        if not isinstance(entity_id, str) or not entity_id.startswith("automation.") or ident is None:
            continue
        automations.append({"entity_id": entity_id, "id": ident,
                            "alias": attrs.get("friendly_name"), "config": configs.get(entity_id)})
    automations.sort(key=lambda item: item["entity_id"])
    return automations[:200], len(automations) > 200


def days_covered(events):
    return len({item["t"][:10] for item in events})


def _context_params(params):
    from .commands import CommandError

    if (not isinstance(params, dict) or set(params) != {"days"}
            or type(params["days"]) is not int or not 1 <= params["days"] <= 30):
        raise CommandError("invalid_params")
    return params


def _automation_id(value):
    return isinstance(value, str) and AUTOMATION_CONFIG_ID_RE.fullmatch(value)


def _automation_config_shape(config):
    from .commands import CommandError

    if (not isinstance(config, dict) or set(config) - AUTOMATION_CONFIG_KEYS
            or not isinstance(config.get("alias"), str) or not config["alias"].strip()
            or not isinstance(config.get("triggers"), list) or not config["triggers"]
            or not isinstance(config.get("actions"), list) or not config["actions"]
            or "description" in config and not isinstance(config["description"], str)):
        raise CommandError("invalid_params")
    try:
        if len(json.dumps(config, allow_nan=False).encode()) > MAX_AUTOMATION_CONFIG_BYTES:
            raise CommandError("invalid_params")
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise CommandError("invalid_params") from None


def _automation_upsert_params(params):
    from .commands import CommandError

    if not isinstance(params, dict) or set(params) != {"id", "config"} or not _automation_id(params["id"]):
        raise CommandError("invalid_params")
    _automation_config_shape(params["config"])
    return params


def _automation_validate_params(params):
    from .commands import CommandError

    if not isinstance(params, dict) or set(params) != {"config"}:
        raise CommandError("invalid_params")
    _automation_config_shape(params["config"])
    return params


def _automation_delete_params(params):
    from .commands import CommandError

    if not isinstance(params, dict) or set(params) != {"id"} or not _automation_id(params["id"]):
        raise CommandError("invalid_params")
    return params


def check_automation(config, services, states, validation):
    """Return human-readable reasons this automation config would not work on the home's real Home Assistant."""
    problems = []
    services = services if isinstance(services, dict) else {}
    known = {item.get("entity_id") for item in states if isinstance(item, dict)} if isinstance(states, list) else set()

    def add(text):
        text = str(text)[:200]
        if text not in problems:
            problems.append(text)

    def templated(value):
        return "{{" in value or "{%" in value

    def check_step(step, name):
        if templated(name):
            add("templated actions are not allowed")
            return
        domain, _, service = name.partition(".")
        domain_services = services.get(domain)
        description = domain_services.get(service) if isinstance(domain_services, dict) else None
        if not isinstance(description, dict):
            if domain == "notify" and name in known:
                add(f"{name} is a notification entity, not an action; "
                    f"use action notify.send_message with target entity_id {name}")
            else:
                add(f"unknown action {name}")
            return
        if step.get("target") and "target" not in description:
            add(f"{name} does not accept a target")
        data, fields = step.get("data"), description.get("fields")
        if not isinstance(fields, dict) or not fields or isinstance(data, str):
            return
        data = data if isinstance(data, dict) else {}
        allowed = {"entity_id", "device_id", "area_id", "floor_id", "label_id"}
        required = []
        for key, field in fields.items():
            allowed.add(key)
            if not isinstance(field, dict):
                continue
            if isinstance(field.get("fields"), dict):
                # A section: its fields may sit flat in data or nested under the section name.
                section = data.get(key) if isinstance(data.get(key), dict) else {}
                for sub_key, sub_field in field["fields"].items():
                    allowed.add(sub_key)
                    if isinstance(sub_field, dict) and sub_field.get("required") is True \
                            and sub_key not in data and sub_key not in section:
                        required.append(sub_key)
            elif field.get("required") is True and key not in data:
                required.append(key)
        for key in data:
            if key not in allowed:
                add(f"{name} has no field {key}")
        for key in required:
            add(f"{name} needs field {key}")

    def walk(steps, depth=0):
        if isinstance(steps, dict):
            steps = [steps]
        if not isinstance(steps, list) or depth > 20:
            return
        for step in steps:
            if not isinstance(step, dict):
                continue
            name = step.get("action") if isinstance(step.get("action"), str) else step.get("service")
            if isinstance(name, str):
                check_step(step, name)
            for key in ("sequence", "default", "then", "else", "parallel"):
                walk(step.get(key), depth + 1)
            options = step.get("choose")
            for option in [options] if isinstance(options, dict) else options if isinstance(options, list) else []:
                if isinstance(option, dict):
                    walk(option.get("sequence"), depth + 1)
            if isinstance(step.get("repeat"), dict):
                walk(step["repeat"].get("sequence"), depth + 1)

    def references(node, depth=0):
        if depth > 30:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if key != "entity_id":
                    references(value, depth + 1)
                    continue
                for item in value.split(",") if isinstance(value, str) else value if isinstance(value, list) else []:
                    item = item.strip() if isinstance(item, str) else ""
                    if item and not templated(item) and item not in ("all", "none") and item not in known:
                        add(f"unknown entity {item}")
        elif isinstance(node, list):
            for item in node:
                references(item, depth + 1)

    if isinstance(validation, dict):
        for section in ("triggers", "conditions", "actions"):
            result = validation.get(section)
            if isinstance(result, dict) and result.get("valid") is False:
                add(f"{section}: {result.get('error')}")
    if isinstance(config, dict):
        walk(config.get("actions"))
        references(config)
    return problems[:10]


async def _automation_problems(client, config):
    from .supervisor import UpstreamError

    results = await client.admin_ws([
        {"type": "get_services"}, {"type": "get_states"},
        {"type": "validate_config", "triggers": config["triggers"],
         "conditions": config.get("conditions", []), "actions": config["actions"]},
    ])
    if (not isinstance(results, list) or len(results) != 3 or not isinstance(results[0], dict)
            or not isinstance(results[1], list) or not isinstance(results[2], dict)):
        raise UpstreamError()
    return check_automation(config, *results)


async def automation_validate(client, params):
    return {"problems": await _automation_problems(client, params["config"])}


async def automation_context(client, params):
    from .supervisor import UpstreamError

    results = await client.admin_ws([
        {"type": "get_config"}, {"type": "config/entity_registry/list"},
        {"type": "config/device_registry/list"}, {"type": "config/area_registry/list"},
        {"type": "get_states"},
    ])
    if not isinstance(results, list) or len(results) != 5:
        raise UpstreamError()
    config, registry, devices, areas, states = results
    if not isinstance(config, dict) or not isinstance(config.get("time_zone"), str):
        raise UpstreamError()
    zone = ZoneInfo(config["time_zone"])
    generated = datetime.now(timezone.utc).replace(microsecond=0)
    start = generated - timedelta(days=params["days"])
    state_list = states if isinstance(states, list) else []
    entity_ids = sorted({item["entity_id"] for item in state_list if isinstance(item, dict)
                         and isinstance(item.get("entity_id"), str)
                         and _entity_domain(item["entity_id"]) in KEEP_EVENT_DOMAINS})
    all_events = []
    if entity_ids:
        for day in range(params["days"]):
            day_start = start + timedelta(days=day)
            windows = [{"type": "logbook/get_events", "start_time": window_start.isoformat(),
                        "end_time": min(window_start + timedelta(hours=2), generated).isoformat(),
                        "entity_ids": entity_ids}
                       for window_start in (day_start + timedelta(hours=2 * index) for index in range(12))]
            # ponytail: 2-hour logbook windows; shrink them if one window overflows the 2 MiB message cap
            results = await client.admin_ws(windows)
            if isinstance(results, list):
                for result in results:
                    if isinstance(result, list):
                        all_events.extend(result)
    automation_states = [item for item in state_list if isinstance(item, dict)
                         and isinstance(item.get("entity_id"), str) and item["entity_id"].startswith("automation.")
                         and isinstance(item.get("attributes"), dict) and item["attributes"].get("id") is not None]
    configs = {}
    for offset in range(0, len(automation_states), 50):
        batch = automation_states[offset:offset + 50]
        values = await client.admin_ws([{"type": "automation/config", "entity_id": item["entity_id"]}
                                        for item in batch])
        if not isinstance(values, list) or len(values) != len(batch):
            raise UpstreamError()
        configs.update((item["entity_id"], value) for item, value in zip(batch, values))
    entities, entities_truncated = build_entities(registry if isinstance(registry, list) else [],
        devices if isinstance(devices, list) else [], areas if isinstance(areas, list) else [], state_list)
    normalized = normalize_events(all_events, zone)
    events, events_truncated = filter_events(normalized, params["days"])
    person_states = {item["entity_id"]: item["state"] for item in state_list if isinstance(item, dict)
                     and isinstance(item.get("entity_id"), str) and _entity_domain(item["entity_id"]) == "person"
                     and isinstance(item.get("state"), str)}
    patterns = build_patterns(normalized, params["days"], person_states,
                              ignored_entities(registry if isinstance(registry, list) else []))
    automations, automations_truncated = build_automations(state_list, configs)
    result = {"version": 1, "generated_at": generated.isoformat(), "time_zone": config["time_zone"],
              "days_requested": params["days"], "days_covered": days_covered(events),
              "truncated": entities_truncated or events_truncated or automations_truncated,
              "entities": entities, "events": events, "automations": automations,
              "patterns": patterns}
    encoded = json.dumps(result, separators=(",", ":"))
    while len(encoded.encode()) >= 1_900_000:
        if result["events"]:
            result["events"].pop(0)
            result["days_covered"] = days_covered(result["events"])
        elif result["automations"]:
            result["automations"].pop()
        elif result["entities"]:
            result["entities"].pop()
        else:
            raise UpstreamError()
        result["truncated"] = True
        encoded = json.dumps(result, separators=(",", ":"))
    return result


async def automation_upsert(client, params):
    from .commands import CommandError
    from .supervisor import UpstreamError

    problems = await _automation_problems(client, params["config"])
    if problems:
        raise CommandError("ha_error", "Automation would not work: " + "; ".join(problems))
    access, refresh = await _login(client)
    path = "/api/config/automation/config/" + params["id"]
    try:
        status, raw = await client.ha_http("POST", path, json=params["config"], token=access)
        payload = _response_json(raw)
        if status == 400:
            message = payload.get("message", "") if isinstance(payload, dict) else ""
            raise CommandError("ha_error", message if isinstance(message, str) else "")
        if status != 200:
            raise UpstreamError()
        status, raw = await client.ha_http("GET", path, token=access)
        verified = _response_json(raw)
        if status != 200 or not isinstance(verified, dict) or verified.get("alias") != params["config"]["alias"]:
            raise UpstreamError()
        deadline = time.monotonic() + 15
        while True:
            states_result = await client.admin_ws([{"type": "get_states"}])
            states = states_result[0] if isinstance(states_result, list) and len(states_result) == 1 else None
            if isinstance(states, list) and any(isinstance(item, dict)
                    and isinstance(item.get("entity_id"), str) and item["entity_id"].startswith("automation.")
                    and isinstance(item.get("attributes"), dict) and item["attributes"].get("id") == params["id"]
                    for item in states):
                return {"id": params["id"]}
            if time.monotonic() >= deadline:
                raise CommandError("conflict")
            await asyncio.sleep(0.5)
    finally:
        try:
            await client.ha_http("POST", "/auth/revoke", form={"token": refresh})
        except Exception:
            pass


def _response_json(raw):
    from .supervisor import UpstreamError

    if not isinstance(raw, bytes) or len(raw) > 2 * 1024 * 1024:
        raise UpstreamError()
    try:
        return json.loads(raw) if raw else None
    except (ValueError, UnicodeError, RecursionError):
        raise UpstreamError() from None


async def automation_delete(client, params):
    from .supervisor import UpstreamError

    access, refresh = await _login(client)
    try:
        status, raw = await client.ha_http("DELETE", "/api/config/automation/config/" + params["id"], token=access)
        payload = _response_json(raw)
        if status == 200:
            return {"removed": True}
        message = payload.get("message") if isinstance(payload, dict) else None
        if status in (400, 404) and isinstance(message, str) and "not found" in message.lower():
            return {"removed": False}
        raise UpstreamError()
    finally:
        try:
            await client.ha_http("POST", "/auth/revoke", form={"token": refresh})
        except Exception:
            pass
