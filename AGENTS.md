# Working on Agent Relay

Specify behavior and failure boundaries before implementation. Keep coordination
data, tokens, binaries and logs under ignored `.local/`, never in GitHub issues.

Use one task authority (Beads) and one message authority (Agent Mail). Do not add
a second copy of task status. The HTTP gateway is the sole task writer while it
runs; direct CLI writes bypass its coordination predicates.

Use synthetic fixtures. Test competing claims, stale versions, independent
review, blockers, recipient isolation and restart persistence. Never describe
passing adapter tests as proof that an external upstream release is flawless.

Each change has an owner and independent reviewer. A handoff names the exact
commit, evidence, untested behavior and next action. Tool permission rejections
are durable blockers; another agent is not an approval workaround.

No global MCP, shell, service or billing configuration changes as a side effect.
HTTP polling does not wake an inactive agent. Report implemented, verified,
running and adopted separately.
