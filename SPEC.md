# Agent Relay: first usable slice

## Problem

Two coding sessions missed messages in an append-only Markdown file because
each searched after its own last post. Task ownership, exact reviewed commits,
permissions and test evidence also required repeatedly reading prose history.

## Decision

Reuse `mcp_agent_mail_rust` 0.3.39 for durable addressed messages and
`beads_rust` 0.7.6 for tasks. Add a small local HTTP adapter, rather than rebuild
either store. Python's standard library keeps the adapter easy to run.

Beads is the only task authority. Its `updated_at` is the API's version token;
its `agent_context.relay` holds workflow metadata. Native conditional updates
and exclusive claims are used where available. A process lock serializes writes
inside the single gateway. Direct Beads writes, multiple gateways and distributed
writers are unsupported. Native close lacks conditional update; its predicate
check and close rely on that sole-writer contract. No exactly-once claim is made
for task creation after a lost HTTP response.

Mail is the only message authority. Gateway credentials map to provisioned Mail
identities; callers cannot choose another sender. Native message idempotency keys
are forwarded. Inbox events use recipient-specific monotonic cursors, not the
time of the caller's last sent message. Reading and acknowledgment are separate;
upstream `fetch_inbox` implicitly marks read, so the gateway uses the event feed.

## First-slice behavior

- Authenticated task create/list/show, atomic claim, owner updates and heartbeat.
- Session and lease recorded on a task. Expiry is visible, advisory and never
  proof that the old process stopped. No automatic takeover.
- Version required for state changes. Stale writes fail rather than overwrite.
- Handoff records a named review recipient; that recipient accepts or requests
  changes in a separate review operation. The author remains task owner.
- Review binds an independent reviewer and evidence to an exact commit SHA.
  Changing the head invalidates the old acceptance for completion.
- An open permission blocker prevents completion/ownership transfer. Recording
  one does not grant authority to override a tool denial. No agent-facing unblock
  endpoint in this slice; operator recovery is an explicit offline procedure.
- Addressed message send, incremental inbox and explicit acknowledgment.
- Loopback binding and distinct agent bearer tokens stored in private files.

## Deployment and recovery

No global installation or MCP registration. `manage.py` provisions only `.local/`
and supervises the two local processes. A failed child causes its peer to stop.
Stop the supervisor before copying `.local/` for a consistent cold backup; restore
the whole directory with private permissions. A task export is not a full Mail
backup. A live copy of SQLite files without their WAL is not a backup strategy.

## Acceptance

Real pinned binaries must demonstrate two authenticated clients exchanging and
acknowledging messages, only one winning a concurrent claim, stale versions
rejected, restart continuity, recipient isolation, review SHA invalidation and
blocked completion. Then Claude independently repeats the HTTP workflow before
we migrate active Smart Emmerich tasks. Old history stays archived in Markdown;
the file becomes a wayfinder after both clients confirm the switch.

## Deferred, explicit limitations

No agent wakeup/supervisor for coding sessions; our process supervisor only keeps
the HTTP service and Mail together. No multi-host deployment, distributed locks,
automatic approval parsing, global port reservations, dependency scheduling,
dashboard, or automatic GitHub status mirroring. Native upstream MCP remains
available as a component, but configuring either chat client is a separate step.

## Alternatives considered

- Agent Mail + Beads without adapter: least code, but tasks need CLI access and
  identity/review rules are convention rather than a common HTTP contract.
- GitHub Issues/Projects: excellent development backlog and PR links; unsuitable
  as our high-frequency local inbox because it creates external writes and lacks
  native task leases and per-session inbox cursors.
- Linear Agent API: agent sessions and webhook activities exist; requires a
  hosted account and an independently running receiver/agent loop.
- Own SQLite task/message server: fewer upstream processes but reinvents mature
  inbox, acknowledgment, persistence and task capabilities. Revisit only if the
  adopted components prove harder to maintain than this adapter saves.

Sources: https://github.com/Dicklesworthstone/mcp_agent_mail_rust,
https://github.com/Dicklesworthstone/beads_rust,
https://docs.github.com/en/rest/issues/comments,
https://linear.app/developers/agent-interaction.
