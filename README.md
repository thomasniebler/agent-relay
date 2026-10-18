# Agent Relay

A local HTTP endpoint for two coding sessions to track work and exchange
individual messages. It uses Agent Mail for messages and Beads for tasks. Both
sessions use the same API; each gets a separate bearer credential.

The first implementation is intentionally a local single-writer service. It
does not wake an idle Claude or Codex session. It also does not claim a lease
expiry means the previous process stopped.

## What works

- Task create/list/show, exclusive claim, heartbeat, notes, status, blocker,
  handoff, independent review, and close after acceptance of the exact SHA.
- Optimistic version checks: every task update includes the last `version`; a
  stale version returns HTTP 409.
- Addressed messages, recipient-specific inbox cursors, message bodies, separate
  mark-read and acknowledge actions, and native idempotency keys for sends.
- The API maps bearer credentials to fixed agent identities. Message bodies are
  returned only when the message appears in that recipient's inbox event feed.
- Health endpoint, structured errors, 256 KiB request limit, loopback-only bind,
  private config files, and logs with request bodies and tokens omitted.

## Start it

Use Python 3 and the pinned upstream binaries. Download and verify the signed
release archives according to the upstream instructions before extracting them.
This repository's `init` command checks the binary versions, copies them under
`.local/`, and records their hashes. It does not download software or change your
shell, MCP, or system-service configuration.

```sh
python3 manage.py init \
  --br-bin /path/to/br \
  --mail-bin /path/to/mcp-agent-mail
python3 manage.py serve
```

Default endpoints are `127.0.0.1:18765` (this API) and `127.0.0.1:18766` (Agent
Mail MCP). A second terminal can use `client.py`:

```sh
python3 client.py --agent codex GET /tasks
printf '%s' '{"title":"Review dashboard","acceptance_criteria":"Verify exact PR head"}' \
  | python3 client.py --agent codex POST /tasks --body -
python3 client.py --agent claude GET /inbox
python3 client.py --agent claude GET '/inbox?pending_ack=true'
```

Client credentials are in `.local/clients/{codex,claude}.json` with mode 0600.
Keep `.local/` private. Do not paste these files into messages, shell history,
GitHub, or a prompt. The client saves the returned inbox cursor beside each
credential after printing the result; it does not mark messages read or
acknowledge them. With a raw HTTP caller, persist `next_cursor` per recipient and
pass it as `after` on the next request. Readiness is `GET /health`; it
intentionally reveals no configuration.

## HTTP API

Send `Authorization: Bearer <agent-token>` and JSON request bodies. The token is
read from the private client file by `client.py`, not passed on a command line.

| Method and path | Purpose |
| --- | --- |
| `GET /tasks` | List tasks in `tasks`; optional exact `?state=open` filter or active-work summary `?state=active`; pages use `?offset=0&limit=100` (`limit` 1–1000) |
| `POST /tasks` | Create JSON `{title, description?, acceptance_criteria?}` |
| `GET /tasks/{id}` | Read task and relay metadata |
| `POST /tasks/{id}/claim` | Exclusive claim with `version`, `session_id`, optional `lease_seconds` |
| `POST /tasks/{id}/heartbeat` | Renew the same session lease |
| `PATCH /tasks/{id}` | Set `status`, `notes`, or `evidence` with `version` |
| `POST /tasks/{id}/blockers` | Record a permission blocker; an open blocker prevents handoff, claim, and close |
| `POST /tasks/{id}/handoff` | Send task for review with recipient, full commit SHA, and evidence |
| `POST /tasks/{id}/review` | JSON `{version, sha, verdict, evidence}`; named recipient reviews the exact SHA; `verdict` is `accept` or `request_changes` |
| `GET /inbox?after={cursor}` | Read recipient events; each event includes `acknowledged` and `acknowledged_at` |
| `GET /inbox?pending_ack=true` | Full snapshot of unacknowledged, acknowledgment-required events |
| `GET /messages/{id}` | Retrieve the body only for a message in this agent's inbox |
| `POST /messages` | JSON `{to, subject, body, thread_id?, ack_required?, idempotency_key?}`; `to` uses configured agent IDs |
| `POST /messages/{id}/read` | Mark read without acknowledging |
| `POST /messages/{id}/ack` | Acknowledge a message |

Task status transitions use Beads statuses (`review` is a workflow status). For a message request, `to` is a nonempty list such as `["claude"]`, and `body` is Markdown text. The sender identity is derived from the bearer token and cannot be supplied in the request. The pending-ack view rescans the retained event feed on each call, so it continues to show unacknowledged messages; omit `after` for this snapshot. It is separate from the incremental inbox cursor.

Every task mutation requires the current version. A heartbeat updates that version, so clients must reread before their next task change. `GET /tasks?state=...` is an exact single-state filter; `state=active` combines `open`, `in_progress`, `blocked`, and `review` for an overview.

To resume a nonterminal `blocked` or `review` task with no open permission blocker, patch its status to `in_progress` with the latest version. That clears any previous handoff, review, and evidence. Closed tasks are terminal through this API.

Task status transitions use Beads statuses (`review` is a workflow status). To close, first record an
accepted independent review on the current handoff SHA, then PATCH status to
`closed`. If code changes, create a new handoff; it clears the previous review.
An expired lease remains assigned to its existing owner until a deliberate
operator recovery. It cannot be taken over through this API.

## Boundaries and recovery

The HTTP gateway serializes writes in one process and uses Beads' native
`updated_at` conditional update. Keep exactly one gateway and do not change tasks
through the Beads CLI while the gateway is running. Native task creation cannot
be made exactly-once if the caller loses the response. Mail send idempotency is
provided by Agent Mail. Task close depends on the single-writer process lock
because Beads does not expose a conditional close operation.

Permission blockers preserve the reported action, target, reason, reporter and
time. This slice intentionally has no agent-facing unblock endpoint. Do not use
another agent to bypass an external tool denial. This slice also has no separate
human-approval record or approval endpoint. The human operator must resolve the
permission issue and recover the task explicitly offline.

Stop the supervisor with Ctrl-C. Back up the entire `.local/` directory while
the processes are stopped; it contains the Beads task store, Agent Mail SQLite
database, config and credentials. `br list` output alone is not a message
backup. Restore the directory with private permissions before starting again.

## Existing options

[Agent Mail](https://github.com/Dicklesworthstone/mcp_agent_mail_rust) already
provides HTTP MCP, durable inbox events and acknowledgments. [Beads
Rust](https://github.com/Dicklesworthstone/beads_rust) provides tasks, claims and
version-conditional updates. Agent Relay connects those components behind one
small HTTP contract. GitHub Issues and Projects remain useful for a code
repository's backlog and PR links, but this private local inbox avoids routine
external writes. [Linear's Agent API](https://linear.app/developers/agent-interaction)
provides session activities and webhooks, but needs a running receiver to
deliver prompts; it cannot wake these existing chat sessions by itself.

## Project decisions

See [SPEC.md](SPEC.md) for the rationale, acceptance workflow, failure
boundaries, alternatives and the explicit features deferred from this first
slice.
