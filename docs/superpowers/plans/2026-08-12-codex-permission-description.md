# Codex Permission Description Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Gerald handle the real Codex Bash `PermissionRequest` shape containing `command` plus string `description`, while preserving the existing fail-closed physical approval boundary.

**Architecture:** Keep the existing `approval_hint()` boundary and extend only its accepted key-set validation. `description` is compatibility metadata only: it is validated as a string when present, then ignored; only the existing normalized complete command reaches Gerald.

**Tech Stack:** Python 3, pytest, existing Codex hook adapter.

## Global Constraints

- Productive scope is only `bridge/codex/gerald.py` and `bridge/tests/test_codex_adapter.py`.
- Accept only exact key sets `{command}` or `{command, description}`.
- `description` must be a string when present and must never be transmitted to Gerald.
- Unknown additional metadata must remain fail-closed.
- Existing command normalization, safety checks, identity binding, timeout fallback, and allow/deny semantics remain unchanged.
- No BLE, UI, protocol, re-advertising, or unrelated refactor.

---

### Task 1: Reproduce and fix the PermissionRequest compatibility boundary

**Files:**
- Modify: `bridge/tests/test_codex_adapter.py`
- Modify: `bridge/codex/gerald.py`

**Interfaces:**
- Consumes: `approval_hint(event: dict) -> str | None`, `handle_event(event: object, client: BridgeClient) -> dict | None`.
- Produces: compatibility with Bash `tool_input={"command": <str>, "description": <str>}` while preserving all existing rejection paths.

- [ ] **Step 1: Write the failing positive regression test**

Add a test using a `PermissionRequest` with `tool_name="Bash"` and:

```python
tool_input={
    "command": "touch /etc/gerald-codex-return-probe",
    "description": "May I run the exact requested command with permission to write under /etc?",
}
```

Use `FakeClient("deny")`. Assert Gerald is contacted exactly once with tool `Bash` and hint `touch /etc/gerald-codex-return-probe`, and assert the returned hook decision is `{"behavior": "deny", "message": "Denied once from Gerald"}`.

- [ ] **Step 2: Add the fail-closed negative regression test**

Add a second test with the same `command` and `description` plus `"unexpected": "metadata"`. Assert `handle_event(...) is None` and `client.approvals == []`.

- [ ] **Step 3: Run the positive test before production modification**

Run:

```bash
python -m pytest -q bridge/tests/test_codex_adapter.py -k 'description_metadata'
```

Expected before the production fix: the positive description-metadata case fails because Gerald is not contacted; the unexpected-extra-field case passes.

- [ ] **Step 4: Implement the minimal key-set change**

In `approval_hint()`, replace the exact `set(tool_input) != {"command"}` rejection with logic equivalent to:

```python
keys = set(tool_input)
if keys == {"command"}:
    pass
elif keys == {"command", "description"} and isinstance(tool_input.get("description"), str):
    pass
else:
    return None
```

Leave `_normalize_approval_command(tool_input.get("command"))` and every other function unchanged.

- [ ] **Step 5: Run the focused adapter suite**

Run:

```bash
python -m pytest -q bridge/tests/test_codex_adapter.py
```

Expected: all tests pass.

- [ ] **Step 6: Run syntax validation and scope check**

Run:

```bash
python -m py_compile bridge/codex/gerald.py bridge/tests/test_codex_adapter.py
git diff --check
git status --short
```

Expected: only the two productive/test files differ from the documentation baseline; no whitespace errors.

- [ ] **Step 7: Commit the bounded implementation**

```bash
git add bridge/codex/gerald.py bridge/tests/test_codex_adapter.py
git commit -m "fix: accept Codex permission descriptions"
```

- [ ] **Step 8: Physical acceptance**

With the verified commit used by the active Codex hook, set Codex permissions to `Ask for approval` and request exactly:

```text
touch /etc/gerald-codex-return-probe
```

Gerald must display the command. Physically deny it on Gerald. Codex must receive the denial and must not execute the command. Only after that live result may `GERALD_TO_CODEX_APPROVAL_PATH`, `PHYSICAL_NUS_TX_DATA`, and `PHYSICAL_NUS_BIDIRECTIONAL` be marked PASS.
