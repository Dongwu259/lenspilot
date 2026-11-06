"""PhotoS — JSON output contract versioning (for AI-agent consumers).

Every JSON payload emitted by the CLI (``--json``), the REST server
(``serve``) and the MCP tools carries an additive top-level
``schema_version`` marker. This is NOT an envelope — the existing top-level
keys are preserved unchanged, so old consumers that read ``d["summary"]``
keep working; the marker lets agents detect and adapt to contract changes.

Contract rules (documented in docs/AGENT_API.md):
- ``schema_version`` is additive: consumers must IGNORE unknown keys.
- PhotoS only increments ``SCHEMA_VERSION`` on a breaking shape change
  (renamed/removed/retagged keys). Adding a key is NOT a bump.
- It is a single global integer, not tied to the PhotoS release version.

Module has zero project imports (pure stdlib) so cli/server/mcp/plugincmd
can all reuse it without import cycles.
"""

SCHEMA_VERSION = 1


def versioned(payload: dict) -> dict:
    """Return ``payload`` with the ``schema_version`` marker prepended.

    Additive by construction: every existing key is preserved; the marker
    is simply the first key (nice for humans reading the JSON).
    """
    return {"schema_version": SCHEMA_VERSION, **payload}
