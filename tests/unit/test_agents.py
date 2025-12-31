from __future__ import annotations

from watercooler.agents import (
    _canonical_agent,
    _counterpart_of,
    _split_agent_and_tag,
    _default_agent_and_role,
)


def test_split_and_canonical():
    # New format: "Agent (tag)" not "agent#tag"
    reg = {"canonical": {"gpt": "GPT", "codex": "Codex"}}
    a, tag = _split_agent_and_tag("GPT (dev)")
    assert a == "GPT" and tag == "dev"
    # Canonical agent with auto user tagging
    result = _canonical_agent("gpt", reg)
    assert result.startswith("GPT (")  # Auto-tagged with OS username


def test_counterpart_and_default():
    # New registry structure with canonical/counterpart keys
    reg = {
        "canonical": {"codex": "Codex", "claude": "Claude"},
        "counterpart": {"Codex": "Claude", "Claude": "Codex"},
        "default_ball": "Team"
    }
    # Counterpart now returns tagged format
    result = _counterpart_of("Codex", reg)
    assert result.startswith("Claude (")  # Tagged with OS username
    # Default agent is now "Team" by default
    agent, role = _default_agent_and_role(reg)
    assert agent == "Team" and isinstance(role, str) and role

