from __future__ import annotations


def _fill_template(src: str, mapping: dict[str, str]) -> str:
    """Replace common token styles in templates.

    Supports both '{{KEY}}' and '<KEY>' placeholders. Also replaces
    special placeholders commonly used in current templates such as
    '<YYYY-MM-DDTHH:MM:SSZ>' and '<Codex|Claude|Team>'.
    """
    out = src
    for k, v in mapping.items():
        out = out.replace(f"{{{{{k}}}}}", v)
        out = out.replace(f"<{k}>", v)
    # Special-case common placeholders
    if "UTC" in mapping:
        out = out.replace("<YYYY-MM-DDTHH:MM:SSZ>", mapping["UTC"])
    if "AGENT" in mapping:
        out = out.replace("<Codex|Claude|Team>", mapping["AGENT"])  # entry template
    if "BALL" in mapping:
        # header template
        out = out.replace("Ball: <Codex|Claude|Team>", f"Ball: {mapping['BALL']}")
    if "TOPIC" in mapping:
        out = out.replace("Topic: <Short title>", f"Topic: {mapping['TOPIC']}")
        out = out.replace("<topic>", mapping["TOPIC"])  # title line convenience
    return out
