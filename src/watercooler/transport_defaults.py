# SPDX-License-Identifier: Apache-2.0
"""Shipped transport defaults — single source of truth, dependency-free.

These are the values ``McpConfig`` bakes into its field defaults (hosted-first
onboarding, PR #1128): a machine with credentials but NO config file is an
authenticated proxy. They live in this tiny module — not inline in the
pydantic schema — so dependency-light readers that must not import the config
stack (the Stop hook's per-repo effective-transport gate,
``watercooler.stop_hook._local_findings_apply``) resolve the SAME defaults the
schema does instead of drifting on independent fallback values (review #1135
P1, round 4: the hook defaulted to ``stdio``/no-URL and kept polling local
findings while the server imported thin).
"""

DEFAULT_TRANSPORT = "proxy"
DEFAULT_HOSTED_MCP_URL = "https://watercooler-cloud-production.up.railway.app/mcp/"
