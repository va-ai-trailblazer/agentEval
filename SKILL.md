# AgentEval Skill Definition

## Name
AgentEval — Agent Evaluator for Agentforce

## Version
0.3.1 — Phase 1: Static Config Analysis + Well-Architected Pattern Catalogue (17-agent validated, precise fix guidance, citable patterns)

## Purpose

AgentEval evaluates Agentforce agent configurations against a rubric derived from
Salesforce platform documentation, Salesforce Well-Architected principles, and
confirmed empirical observations across real agent deployments.

It produces a ranked health report — before any tests are run, before any agent
is invoked.

## Capabilities (Phase 1)

- config-analysis: enabled
- behavioral-evaluation: disabled (Phase 2)
- live-trace-evaluation: disabled (Phase 3)

## Agent Types Supported

Validated across 17 agents in 3 orgs:

- Chat / messaging agents (CustomerWebClient, Messaging surfaces)
- Voice agents (Einstein Conversation Intelligence, Omni-Channel Voice, PSTN)
- Escalation-heavy agents (human handoff, transfer routing, Omni-Channel queues)
- Analytics and data agents (multi-topic, data action binding)
- Sales agents (high action count, Slack integration, CRM operations)
- Sales coaching agents
- Employee service agents
- Partner operations agents
- Automation / flow agents (single-topic, action-only)
- Lead generation agents (multi-step, calendar integration)

## Input Contract

| Input | How provided | Required |
|---|---|---|
| Org alias | `--org` flag | yes |
| Agent API name | `--agent` flag | yes (or use --list-agents) |

No XML files. No credentials in chat. No manual metadata retrieval.

## What It Retrieves

- `GenAiPlannerBundle` — planner instructions, topic links, global actions
- `GenAiPlugin` (per topic) — topic scope, action bindings, step instructions
- Omni-Channel routing configuration (voice and chat surface)
- Einstein Bot / Voice configuration (if present)

All retrieval via `sf project retrieve`. Read-only. No org writes.

## Output Contract

A markdown report with two sections:

1. Traffic light summary — one row per check dimension, scannable in 30 seconds
2. Ranked findings — ordered by severity (Critical → High → Medium → Low)

Each finding includes:
- Exact config location (file + field path)
- Observed value
- Expected value
- Impact on runtime behavior
- Precise fix instruction with exact Salesforce UI navigation path OR CLI commands
- Direct link to relevant Salesforce documentation page

Fix instructions distinguish:
- UI-fixable: exact Setup → Agents → Agent Builder → Tab → Field navigation
- Metadata-only: step-by-step CLI retrieve → edit → deploy sequence
- Hybrid: UI verification steps + CLI fix where UI is insufficient

## Severity Levels

| Level | Meaning |
|---|---|
| Critical | Will cause test failure or runtime error. Fix before testing. |
| High | Will degrade agent behavior. Likely output_validation failure. |
| Medium | Reduces quality or increases risk. Worth fixing before go-live. |
| Low | Best practice gap. Low immediate impact. |

## Rubric Authority

Rules are defined in `knowledge/`. Three files:

- `rubric_platform_mechanics.md` — Salesforce-sourced rules, citable to docs
- `rubric_design_quality.md` — empirical rules, confirmed across 3+ real agents
- `agentforce_well_architected.md` — pattern catalogue (added v0.3.0). 26+ named, stable-ID patterns across 9 categories: Authorization, Grounding, Trust/Privacy, Routing, Action Design, Instructions, Surfaces, Escalation, Lifecycle. Every finding produced by AgentEval should cite a pattern ID (e.g. `AGENTFORCE-WELLARCH-AUTH-1`, `AGENTFORCE-WELLARCH-GROUND-2`).

Rules are updated by maintainers, not generated at runtime. The rubric does not
change between runs against the same agent version.

## Pattern Catalogue (v0.3.0)

Every finding now carries a stable pattern ID from `agentforce_well_architected.md`. This makes findings:

- **Portable** — the same `AGENTFORCE-WELLARCH-TRUST-3` reasoning applies to every agent, not just the one being scored.
- **Citable** — developers look up the pattern once, learn the fix shape, and apply it across agents.
- **Versionable** — patterns have a lifecycle: DRAFT → PROVISIONAL → STABLE → AUTHORITATIVE (cross-referenced to documented Salesforce guidance).

Each pattern documents: what good looks like, anti-pattern indicators (concrete XML elements / instruction phrasings to grep for), the smallest correct fix with snippets, and a verification step.

## Feedback Loop

The catalogue is alive. Every evaluation grows it.

- Encounter a finding with no matching pattern? Propose a new one in `agentforce_well_architected.md` with the next sequential ID in the right section, marked **DRAFT**.
- A pattern referenced in 2+ separate evaluations earns **PROVISIONAL → STABLE** promotion.
- A pattern cross-referenced to documented Salesforce guidance earns **STABLE → AUTHORITATIVE**.
- Patterns can be deprecated; keep them in place with `**Deprecated:**` markers so institutional memory survives.

## What This Skill Does Not Do

- Execute test cases (Phase 2)
- Evaluate agent responses (Phase 2)
- Analyze live production conversations (Phase 3)
- Write to the org
- Store or transmit org data
