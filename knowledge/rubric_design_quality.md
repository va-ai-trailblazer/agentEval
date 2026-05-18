# Rubric — Design Quality

Rules in this file are grounded in Salesforce Well-Architected principles,
Agentforce design guidance (Trailhead), and empirical observations confirmed
across real agent deployments.

Each rule cites its source. Rules marked [CANDIDATE] have been observed but not
yet confirmed across 3+ independent agents — treat them as advisory, not definitive.

---

## DQ-01 — Topic trigger words must not overlap across topics

**Applies to:** `GenAiPlugin > description` — the routing signal words for each topic

**Rule:** Trigger words in topic descriptions must be unambiguous. If the same
keyword appears as a signal for two different topics, the planner faces a routing
conflict and may select the wrong topic. Each topic should own a distinct vocabulary.

**Bad example:**
- Technical Support triggers on: "login, error, API, broken"
- Account Management triggers on: "login, access, password"
- "login" appears in both → routing ambiguity on login-related messages

**Fix:** Assign "login" exclusively to Account Management. Add a tiebreaker rule
to the planner instructions: "if user reports errors OR API failures, prefer
Technical Support over Account Management even if login is mentioned."

**Source:**
- Salesforce Trailhead: "Design Agentforce Topics" — topic disambiguation guidance
- https://trailhead.salesforce.com/content/learn/modules/agentforce-topics
- Salesforce Well-Architected: "Agentforce Routing and Topic Design"
- https://architect.salesforce.com/well-architected/agentforce/topics
- Empirical: confirmed across 5+ agents in 3 orgs — routing overlaps on login/error/access
  signals cause measurable topic_assertion failures on edge cases

---

## DQ-02 — Every action path must have a fallback instruction for empty response

**Applies to:** All `genAiPluginInstructions` steps that call an action

**Rule:** Every topic instruction sequence that calls an action must include an
explicit instruction for the case where the action returns no result. Without it,
the agent has no guidance and will either say "system error," produce a generic
response, or ask the user to contact support — all of which fail output_validation.

**Required pattern:**
```
STEP 2 — If the action returned a record, confirm [X].
         If no record was returned, still confirm [Y] and do not say system error.
```

**Source:**
- Salesforce Help: "Write Topic Instructions" — fallback response guidance
- https://help.salesforce.com/s/articleView?id=sf.copilot_topic_instructions.htm
- Empirical: confirmed across 15/17 agents in 3 orgs — Salesforce templates do not
  include fallback instructions by default. Universal gap.

---

## DQ-03 — Knowledge base action must not be reachable from action-only topics

**Applies to:** Topics whose scope explicitly says "do not search the knowledge base"

**Rule:** If a topic's scope instructs the agent not to search the knowledge base,
the `AnswerQuestionsWithKnowledge` action (or equivalent KB action) must not appear
in that topic's `genAiFunctions` block. Even if the planner bundle restricts KB
access at the planner level, a topic-level `genAiFunctions` reference overrides
bundle-level instructions and re-exposes the KB action.

**When violated:** Agent calls KB 4-6 times before attempting the intended action.
`actions_assertion` fails. Latency increases significantly.

**Fix:** Remove the `genAiFunctions` block from topic plugin XML entirely if
the topic should not have access to KB.

**Source:**
- Salesforce Help: "GenAiPlugin genAiFunctions field" — override behavior
- https://help.salesforce.com/s/articleView?id=sf.copilot_plugin_functions.htm
- Empirical: confirmed in AgentEval_Case_Triage_Demo Run 2 (TC2/TC4/TC5, 6x KB loop)

---

## DQ-04 — Sensitive operations must be gated behind human escalation

**Applies to:** Actions that modify credentials, permissions, financial records,
or personal data (password reset, refund, role change, data deletion)

**Rule:** Any action that performs an irreversible or high-risk operation must
be escalated to a human agent rather than executed directly by the AI agent.
The topic must have `canEscalate: true` and the instructions must explicitly
prohibit direct execution.

**Required pattern in topic scope:**
```
For any [sensitive operation], you must escalate to a human agent.
Never perform [sensitive operation] directly.
```

**Source:**
- Salesforce Well-Architected: "Agentforce Security and Trust"
- https://architect.salesforce.com/well-architected/agentforce/security
- Salesforce Trailhead: "Agentforce Guardrails and Safety"
- https://trailhead.salesforce.com/content/learn/modules/agentforce-safety
- Empirical: confirmed in AgentEval_Case_Triage_Demo Account Management topic (TC3)

---

## DQ-05 — Planner instructions must commit to topic before calling any action

**Applies to:** `GenAiPlannerBundle > description` — top-level planner instructions

**Rule:** The planner must commit to a topic selection before invoking any action.
If the planner instructions do not explicitly sequence "select topic first, then
call action," the planner may call a global action (e.g. AnswerQuestionsWithKnowledge)
before topic routing — bypassing all topic-level guardrails.

**Required pattern in planner instructions:**
```
Your first responsibility on every turn is to commit to a topic before calling
any action. Do not call any function before a topic is selected.
```

**Source:**
- Salesforce Help: "Agentforce Planner Instructions"
- https://help.salesforce.com/s/articleView?id=sf.copilot_planner_instructions.htm
- Salesforce Well-Architected: "Agentforce Orchestration Patterns"
- https://architect.salesforce.com/well-architected/agentforce/orchestration
- Empirical: confirmed across 12/17 agents in 3 orgs — Salesforce default planner
  description never includes topic-first sequencing. Universal gap in all template agents.

---

## DQ-10 — Topic scope must explicitly exclude adjacent topic responsibilities

**Applies to:** All topic `GenAiPlugin > scope` fields

**Rule:** Each topic's scope must state not just what it handles, but what it
does not handle. Without explicit exclusions, the planner may route ambiguous
requests to the wrong topic because no topic has rejected it.

**Required pattern:**
```
You handle [X] only. You are not responsible for [adjacent topic A] or [adjacent topic B].
```

**Bad example:** Technical Support scope says "you handle technical errors" but
does not say "you are not responsible for password resets or billing issues."

**Fix:** Add explicit exclusions to every topic scope statement.

**Source:**
- Salesforce Help: "Write Topic Scope Instructions"
- https://help.salesforce.com/s/articleView?id=sf.copilot_topic_scope.htm
- Empirical: confirmed in AgentEval_Case_Triage_Demo — routing ambiguity between
  Technical Support and Account Management on login + error combined messages (TC5)

---

## DQ-06 — Fallback topic must ask exactly one clarifying question per turn

**Applies to:** Topics used as catch-all / General Inquiry / fallback

**Rule:** A fallback topic must ask exactly one targeted clarifying question.
Topics that ask multiple questions in one response reduce resolution rate and
create poor conversation experience. Detectable when instructions say
"ask questions" (plural) without a "one" or "single" qualifier.

**Source:**
- Salesforce Trailhead: "Agentforce Conversation Design"
- https://trailhead.salesforce.com/content/learn/modules/agentforce-conversation-design
- Salesforce Well-Architected: "Agentforce User Experience Principles"
- https://architect.salesforce.com/well-architected/agentforce/ux

---

## DQ-07 — Voice agents must handle no-input timeout explicitly [CANDIDATE]

**Applies to:** Voice agents — topics or planner instructions

**Rule:** Voice agents must define behavior for caller silence (no-input timeout).
Without an explicit instruction, the platform default repeats the last prompt
up to 3 times then disconnects — no case created, no escalation triggered.

**Recommended pattern in planner or topic instructions:**
"If the caller does not respond, ask once more then offer to connect to a live agent."

**Source:**
- Salesforce Help: "Voice Agent Timeout Configuration"
- https://help.salesforce.com/s/articleView?id=sf.voice_agent_timeout.htm
- [CANDIDATE — observed in 1 voice agent deployment]

---

## DQ-08 — Voice agents must confirm transfer before executing it [CANDIDATE]

**Applies to:** Escalation topics in voice agents

**Rule:** Before executing a voice transfer, the agent must verbally confirm
the action to the caller. Silent transfers cause caller confusion and abandoned
calls as callers interpret dead air as a dropped call.

**Required pattern in escalation instructions:**
"Before calling the transfer action, tell the caller you are connecting them
to a live agent and ask them to hold."

**Source:**
- Salesforce Trailhead: "Voice Agent UX Best Practices"
- https://trailhead.salesforce.com/content/learn/modules/voice-agent-design
- [CANDIDATE — observed in 1 voice agent deployment]

---

## DQ-09 — Omni-Channel routing must define a fallback queue for AI agent failures

**Applies to:** Chat and messaging agents with Omni-Channel routing

**Rule:** The Omni-Channel routing configuration must include a fallback queue
for when the AI agent errors or times out. Without it, failed conversations
are dropped with no human follow-up and no case record created.

**Config location:** Omni-Channel → Routing Configuration → Overflow actions

**Source:**
- Salesforce Help: "Omni-Channel Overflow and Fallback Routing"
- https://help.salesforce.com/s/articleView?id=sf.omnichannel_overflow.htm
- Salesforce Well-Architected: "Resilience in Agentforce Deployments"
- https://architect.salesforce.com/well-architected/agentforce/resilience

---

## DQ-11 — Escalation topic must not be able to re-escalate itself

**Applies to:** Topics with `canEscalate: true` that also contain escalation instructions

**Rule:** An escalation topic that itself has `canEscalate: true` and contains
instructions to escalate creates a potential loop — if the escalation fails,
the topic may attempt to escalate the escalation. The escalation failure path
must terminate with a fallback action (log a case, give a number) not another
escalation attempt.

**Source:**
- Salesforce Well-Architected: "Agentforce Escalation Design"
- https://architect.salesforce.com/well-architected/agentforce/escalation

---

## DQ-12 — Agent must have a topic for every expected intent category

**Applies to:** All agents — topic coverage completeness

**Rule:** The set of topics must cover all primary customer intent categories
for the agent's stated purpose. An agent with only one topic routes all
unrecognized intents to that topic regardless of relevance. A triage agent
without a billing topic will silently route billing questions to technical support.

**How to assess:** Compare the agent's `description` (stated purpose) against
the set of `localTopics > masterLabel` values — every major intent class
mentioned in the description should have a corresponding topic.

**Source:**
- Salesforce Trailhead: "Design Agentforce Topics"
- https://trailhead.salesforce.com/content/learn/modules/agentforce-topics
- Salesforce Well-Architected: "Agentforce Topic Coverage"
- https://architect.salesforce.com/well-architected/agentforce/topics

---

## DQ-13 — Topic with no actions must have a clear response-only scope

**Applies to:** Topics with no `localActionLinks` or `localActions`

**Rule:** A topic with no actions must have an explicit scope that states it
provides information only and does not execute operations. Without this,
the planner may route action-requiring requests to the topic — the topic
accepts the routing but can produce no outcome, leaving the customer with
an empty or confused response.

**Source:**
- Salesforce Help: "Agentforce Topic Scope Design"
- https://help.salesforce.com/s/articleView?id=sf.copilot_topic_scope.htm
- Empirical: confirmed in Sales_Agent — User Request Clarification topic had no
  actions but instructions contained "execute" and "update" verbs. Agent routed
  action-requiring requests to the topic and produced empty responses.

---

## DQ-14 — Topic instructions must not contradict topic scope

**Applies to:** `genAiPluginInstructions` vs `scope` within the same topic

**Rule:** If a topic scope explicitly prohibits an action (e.g. "do not search
the knowledge base") but a step instruction calls that action, the instruction
takes precedence at runtime and the scope statement is ignored. This creates
a false sense of security — the config looks compliant but the agent does not
behave as the scope describes.

**Source:**
- Salesforce Help: "Agentforce Topic Instructions vs Scope"
- https://help.salesforce.com/s/articleView?id=sf.copilot_topic_instructions.htm
