# Codex Permission Description Compatibility Design

## Goal

Allow Gerald's physical approval hook to accept the real Codex `PermissionRequest` payload shape observed in production when `tool_input` contains both `command` and `description`, without weakening the existing fail-closed approval boundary.

## Scope

Only `bridge/codex/gerald.py` and `bridge/tests/test_codex_adapter.py` are in productive scope. No BLE peripheral, bridge transport, re-advertising, UI, protocol, or unrelated refactoring is allowed.

## Accepted input shapes

`approval_hint()` may contact Gerald only for `tool_name == "Bash"` and exactly one of these `tool_input` key sets:

- `{command}`
- `{command, description}` where `description` is a string

Any other key set, missing command, non-string description, malformed command, unsafe command, or command exceeding the existing physical display limit must continue to fall through to native Codex approval handling.

## Data handling

Only the normalized complete `command` is sent to Gerald as the approval hint. `description` is never displayed, transmitted over BLE, or used to authorize the request. Existing session/turn request identity binding remains unchanged.

## Security invariants

- Bash-only physical approval remains unchanged.
- Existing ASCII, control-character, quoting, backslash, bracket, Unicode, and length protections remain unchanged.
- Unknown additional `tool_input` metadata remains fail-closed.
- Gerald decisions remain one-shot `allow` or `deny`; timeout/unavailable bridge still falls through to native Codex handling.

## TDD verification

Add a regression test reproducing the real payload shape `{command, description}` and require that Gerald receives the exact normalized command. Add a negative regression test proving an additional unexpected key is still rejected. Run the new regression RED before modifying production code, then run the focused Codex adapter suite GREEN after the minimal implementation.

## Physical acceptance

After local tests pass, repeat the real Codex `touch /etc/gerald-codex-return-probe` approval request with Codex set to `Ask for approval`. Gerald must display the command. A physical Deny on Gerald must propagate back to Codex and prevent execution. That live result is the acceptance gate for `GERALD_TO_CODEX_APPROVAL_PATH`, `PHYSICAL_NUS_TX_DATA`, and `PHYSICAL_NUS_BIDIRECTIONAL`.
