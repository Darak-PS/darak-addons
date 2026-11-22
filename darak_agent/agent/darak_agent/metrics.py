"""Prometheus exposition of the last successful health command, without I/O."""

from . import __version__


def _label(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_metrics(health=None):
    lines = []

    def gauge(name, help_text, value, labels=""):
        if value is not None:
            if isinstance(value, bool):
                value = int(value)
            lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} gauge",
                          f"{name}{labels} {value}"))

    gauge("darak_agent_up", "Agent HTTP service is available.", 1)
    gauge("darak_agent_info", "Darak agent version.", 1,
          f'{{version="{_label(__version__)}"}}')
    if health is None:
        return "\n".join(lines) + "\n"
    if health["core"]["version"] is not None:
        gauge("darak_ha_core_info", "Home Assistant Core version.", 1,
              f'{{version="{_label(health["core"]["version"])}"}}')
    for component in ("core", "supervisor", "os"):
        gauge(f"darak_ha_{component}_update_available", f"Update available for {component}.",
              health[component]["update_available"])
    for key in ("used", "total"):
        gb = health["host"][f"disk_{key}_gb"]
        gauge(f"darak_host_disk_{key}_bytes", f"Host disk {key} bytes.",
              None if gb is None else round(gb * 1024 ** 3))
    gauge("darak_host_cpu_temperature_celsius", "Host CPU temperature in Celsius.",
          health["host"]["cpu_temperature_celsius"])
    for key in ("cpu", "memory"):
        gauge(f"darak_ha_core_{key}_percent", f"Core {key} utilization percent.",
              health["core_stats"][f"{key}_percent"])
    for key in ("unhealthy", "unsupported"):
        gauge(f"darak_ha_resolution_{key}", f"Number of {key} resolution reasons.",
              health["resolution"][f"{key}_count"])
    gauge("darak_ha_addon_update_available", "Number of add-ons with an update available.",
          sum(item["update_available"] is True for item in health["addons"]["items"]))
    return "\n".join(lines) + "\n"
