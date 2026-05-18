# AgentEval — Agent Evaluator for Agentforce

Static config analysis for Agentforce agents. Catches defects before you waste test runs.

## Example Output

See what a report looks like before you run it: [Example Report](outputs/EXAMPLE_REPORT.md)

## What It Does

Reads your deployed agent configuration from your Salesforce org and produces a health
report — ranked findings with exact fix instructions. No agent is invoked. No data is
written to your org.

## What It Covers

- Chat / messaging agents (CustomerWebClient, Messaging surfaces)
- Voice agents (Einstein Conversation Intelligence, Voice surface)
- Escalation and routing configuration (human handoff, transfer rules)

## Requirements

- Salesforce CLI (`sf`) installed and authenticated to your org
- Python 3.9+
- Salesforce user must have **Modify All Data** or **Modify Metadata Through Metadata API Interactions** permission

If you see `INSUFFICIENT_ACCESS`: Setup → Users → your user → Edit → grant one of the above → Save. Then retry.

## Authenticate Once

```bash
sf org login web --alias myOrg --instance-url https://mycompany.my.salesforce.com
```

## Run

```bash
python tools/healthcheck.py --org myOrg --agent YourAgentApiName
```

If you do not know the agent API name:

```bash
python tools/healthcheck.py --org myOrg --list-agents
```

## Output

A markdown report in `outputs/` — one file per run. Share it as-is with the team.

## Phases

| Phase | What it evaluates | Status |
|---|---|---|
| 1 — Config Health | Static agent configuration | Available |
| 2 — Behavioral Evaluation | Test case scoring + failure taxonomy | Roadmap |
| 3 — Live Trace Evaluation | Production conversation analysis | Roadmap |

## No Feedback Required

AgentEval rules are validated by the maintainers against real agents before each release.
You do not need to report findings back. Upgrade to a new version to get improved rules.
