# AgentEval Health Report — Acme Customer Service Agent *(Example)*
Org: acme-prod  |  Run: 2026-05-18 09:00

> **How to read this report**
>
> AgentEval checked this agent's configuration before any tests were run.
> It found issues that would cause failures or poor customer experiences at runtime —
> things that are invisible in normal testing until they show up as unexplained errors.
>
> Issues are ranked by severity. Fix Critical issues first — they will break the agent.
> High issues will degrade behavior noticeably. Medium and Low are worth fixing before go-live.
>
> Every issue includes: what was found, why it matters, and exactly where and how to fix it.

---

## Summary

**3 critical**  |  4 high  |  3 medium  |  2 low  |  48 passed

| Severity | Count | Meaning |
|---|---|---|
| 🔴 Critical | 3 | Will cause test failure or runtime error. Fix before testing. |
| 🟠 High | 4 | Will degrade agent behavior. Likely to cause wrong or empty responses. |
| 🟡 Medium | 3 | Reduces quality or increases risk. Fix before go-live. |
| 🔵 Low | 2 | Best practice gap. Low immediate impact but worth addressing. |
| ✅ Passed | 48 | These checks are clean. |

---

## Issues

---

### 🔴 CRITICAL — Create Case topic: agent will pause and ask permission before every action

**Rule:** PM-01  
**Location:** `GenAiPlannerBundle > Create Case > localActions > CreateCase > isConfirmationRequired`  
**Found:** `true`  
**Expected:** `false`

**What this means in plain English:**

When `isConfirmationRequired` is `true`, the agent stops before executing an action and says something like:
*"I'm about to create a case for you. Would you like me to proceed?"*

This sounds polite, but it breaks automated test runs — the test sends a request, expects a case to be created, and instead receives a question. The test fails because there is no action confirmation in the response.

It also adds an unnecessary extra step for every customer interaction. Most agents should just do the action, not ask permission first.

**Impact:** Every automated test for this topic will fail. In production, every customer interaction requires an extra confirmation click before anything happens.

**How to fix:**

> Setup → Agents → Acme Customer Service Agent → Open in Agent Builder
> → Topics tab → click **Create Case**
> → Actions section → click **Create Case** (the action)
> → uncheck **Require Confirmation**
> → Save

Repeat this for every action in every topic that should execute automatically.

**Salesforce docs:** https://help.salesforce.com/s/articleView?id=sf.copilot_actions_configure.htm

---

### 🔴 CRITICAL — Password Reset topic: escalation instruction exists but canEscalate is switched off

**Rule:** PM-08  
**Location:** `GenAiPlannerBundle > Password Reset > canEscalate`  
**Found:** `false`  
**Expected:** `true`

**What this means in plain English:**

The Password Reset topic's instructions say something like *"if you cannot reset the password, escalate to a human agent."* But the `canEscalate` switch — a hidden setting in the agent configuration — is turned off.

When `canEscalate` is false, the agent **ignores all escalation instructions**. It reads the instruction, processes it, and then does nothing — no transfer, no handoff. The customer stays in the bot loop even when a human should take over.

This setting is not visible in the Agent Builder UI. It only exists in the XML configuration file.

**Impact:** Customers who need human help are never transferred. The agent keeps trying to resolve the issue itself even after it cannot.

**How to fix:**

This requires a direct edit to the configuration file — it cannot be done through the Agent Builder screen.

> Step 1: Run in your terminal:
> `sf project retrieve start --metadata 'GenAiPlannerBundle:AcmeCustomerServiceAgent' --target-org acme-prod`
>
> Step 2: Open the downloaded `.genAiPlannerBundle` file in a text editor.
> Find the `Password Reset` topic block. Change:
> `<canEscalate>false</canEscalate>` → `<canEscalate>true</canEscalate>`
>
> Step 3: Setup → Agents → Acme Customer Service Agent → **Deactivate**
>
> Step 4: Run in your terminal:
> `sf project deploy start --metadata 'GenAiPlannerBundle:AcmeCustomerServiceAgent' --target-org acme-prod`
>
> Step 5: Setup → Agents → Acme Customer Service Agent → **Activate**

Also verify that a transfer destination is configured:
> Agent Builder → Topics tab → Password Reset → Escalation section → confirm a queue or flow is set

**Salesforce docs:** https://help.salesforce.com/s/articleView?id=sf.copilot_topics_escalate.htm

---

### 🔴 CRITICAL — Voice surface: SEL eligibility rule blocks all inbound phone calls

**Rule:** PM-11  
**Location:** `GenAiPlannerBundle > ruleExpressions > conditions > isVerified = true`  
**Found:** `isVerified = true` gate present on voice surface  
**Expected:** Either no gate, or an inbound flow that sets `isVerified = true` before routing to agent

**What this means in plain English:**

This agent has a Session Eligibility rule (a filter that decides who is allowed to reach the agent) that requires callers to be verified (`isVerified = true`). But inbound phone (PSTN) calls arrive with `isVerified = null` — they are never pre-verified just by calling in.

The result: **every single inbound phone call is silently rejected** before the agent even answers. Callers hear nothing, or fall through to a generic error message. This is not visible anywhere in the Agent Builder UI — only in the raw XML configuration.

**Impact:** The voice agent is completely unreachable via phone. Zero calls get through.

**How to fix:**

*Option A — Remove the gate (allow all callers):*

> Step 1: `sf project retrieve start --metadata 'GenAiPlannerBundle:AcmeCustomerServiceAgent' --target-org acme-prod`
>
> Step 2: Open the `.genAiPlannerBundle` file. Find and delete the entire `<ruleExpressions>` block.
>
> Step 3: Deactivate → deploy → activate (same steps as above).
>
> Step 4: Test an inbound call to confirm it reaches the agent.

*Option B — Keep verification — fix the inbound flow:*

> Setup → Process Automation → Flows → open your inbound Omni-Channel routing flow
> → add an Assignment step before the **Route Work** step
> → set `{!isVerified}` = `true` after the caller authenticates
> → Save and activate the flow

**Salesforce docs:** https://help.salesforce.com/s/articleView?id=sf.voice_agentforce_eligibility.htm

---

### 🟠 HIGH — Create Case topic: no fallback instruction if the action returns nothing

**Rule:** DQ-02  
**Location:** `GenAiPlannerBundle > Create Case > genAiPluginInstructions`  
**Found:** No "if no record returned" or equivalent fallback instruction  
**Expected:** An explicit instruction for what to say when the action returns empty

**What this means in plain English:**

The Create Case topic calls an action to create a case record. But there is no instruction telling the agent what to do if that action fails or returns nothing — which can happen due to missing session data, a package dependency error, or a temporary system issue.

Without this instruction, the agent has no guidance. In practice, it defaults to one of these poor outcomes:
- Says *"I'm sorry, there was a system error"*
- Says nothing and ends the conversation
- Gives a generic response that fails automated output validation

This gap exists in 15 out of 17 agents we have tested — Salesforce's default templates do not include fallback instructions.

**Impact:** Any action failure produces a confusing or empty response. Automated test evaluations that check the response content will fail.

**How to fix:**

> Setup → Agents → Acme Customer Service Agent → Open in Agent Builder
> → Topics tab → click **Create Case**
> → Instructions section → click **+ Add Instruction**
> → Add as the **last step**:
>
> *"If the action returned no case record or an empty response, still confirm to the customer that their request has been received and the team will follow up. Do not say 'system error'."*
>
> → Save

No redeploy needed — this is an instruction text change only.

**Salesforce docs:** https://help.salesforce.com/s/articleView?id=sf.copilot_topic_instructions.htm

---

### 🟠 HIGH — Sales Research topic: 3 instructions exceed the 4,000 character limit and will be cut off silently

**Rule:** PM-16  
**Location:** `GenAiPlannerBundle > Sales Research > genAiPluginInstructions`  
**Found:** Instructions `meeting_prep_lead` (5,792 chars), `account_summary` (6,761 chars), `strategy_questions` (4,175 chars)  
**Expected:** Every instruction under 4,000 characters

**What this means in plain English:**

Salesforce has a hard limit of 4,000 characters per instruction step. If an instruction is longer, Salesforce **silently truncates it at deployment** — no warning, no error. The agent receives an instruction that ends mid-sentence.

In this agent, three instructions in the Sales Research topic are over the limit. The agent is operating with incomplete instructions right now, and no one would know unless they ran this check.

**Impact:** Unpredictable behavior in Sales Research. Steps that come after the cutoff point are never executed. The agent appears to work but skips important parts of its instructions.

**How to fix:**

For each oversized instruction:

> Setup → Agents → Acme Customer Service Agent → Open in Agent Builder
> → Topics tab → click **Sales Research**
> → Instructions section → click the instruction name (e.g. `meeting_prep_lead`)
> → edit the text to under 4,000 characters
> → if the content doesn't fit, click **+ Add Instruction** to split it into two sequential steps (e.g. "Step 2a" and "Step 2b")
> → Save

Current sizes and how much to remove:
- `meeting_prep_lead`: 5,792 chars — remove ~2,000 characters or split into 2 steps
- `account_summary`: 6,761 chars — remove ~2,800 characters or split into 2 steps
- `strategy_questions`: 4,175 chars — remove ~400 characters

**Salesforce docs:** https://help.salesforce.com/s/articleView?id=sf.copilot_topic_limits.htm

---

### 🟠 HIGH — Planner instructions do not tell the agent to choose a topic before taking action

**Rule:** DQ-05  
**Location:** `GenAiPlannerBundle > description (top-level agent instructions)`  
**Found:** No instruction requiring the agent to select a topic before calling any action  
**Expected:** An explicit instruction like: *"Your first responsibility on every turn is to select a topic before calling any action"*

**What this means in plain English:**

The top-level agent instructions tell the agent how to behave overall. Without a rule that says *"pick a topic first,"* the agent may call a global action (like searching the knowledge base) before it has decided which topic to use.

This matters because each topic has its own guardrails — things it's allowed and not allowed to do. If the agent calls an action before committing to a topic, it bypasses all of those rules.

This gap exists in 12 out of 17 agents we have tested — Salesforce's default agent templates never include this instruction.

**Impact:** The agent may search the knowledge base or perform global actions before routing correctly, bypassing topic-level restrictions and producing off-topic responses.

**How to fix:**

> Setup → Agents → Acme Customer Service Agent → Open in Agent Builder
> → **Agent Instructions** field (top-level, above the Topics tab — not inside any topic)
> → prepend the following text at the very beginning:
>
> *"Your first responsibility on every turn is to select a topic before calling any action. Do not call AnswerQuestionsWithKnowledge or any other action before a topic has been selected."*
>
> → Save

**Salesforce docs:** https://help.salesforce.com/s/articleView?id=sf.copilot_planner_instructions.htm

---

### 🟠 HIGH — Billing Inquiry topic: scope says no knowledge base, but instructions call it anyway

**Rule:** DQ-14  
**Location:** `GenAiPlannerBundle > Billing Inquiry > scope vs genAiPluginInstructions`  
**Found:** Scope says *"do not search the knowledge base"*, but a step instruction references the AnswerQuestionsWithKnowledge action  
**Expected:** Instructions must not reference actions that the scope prohibits

**What this means in plain English:**

The Billing Inquiry topic's scope statement says it should not use the knowledge base — which is correct for billing, where answers come from account data, not articles.

But somewhere in the step instructions, there is a reference to the knowledge base action. **The instruction takes precedence over the scope at runtime.** So the agent will call the knowledge base despite the scope saying it should not.

This creates a false sense of security — the configuration looks correct in the scope field, but the agent does not behave as described.

**Impact:** The knowledge base is called on billing questions. This increases latency and may return irrelevant article content in billing responses.

**How to fix:**

> Setup → Agents → Acme Customer Service Agent → Open in Agent Builder
> → Topics tab → click **Billing Inquiry**
> → Instructions section → review each instruction step
> → find the step that references "knowledge," "AnswerQuestionsWithKnowledge," or "KB"
> → remove or replace that reference with the correct billing action
> → Save

**Salesforce docs:** https://help.salesforce.com/s/articleView?id=sf.copilot_topic_instructions.htm

---

### 🟡 MEDIUM — Routing conflict: 'Account Management' and 'Technical Support' share trigger words

**Rule:** DQ-01  
**Location:** `GenAiPlannerBundle > Account Management > description AND Technical Support > description`  
**Found:** Both topics use these words in their descriptions: `login`, `access`, `error`  
**Expected:** Each topic owns exclusive trigger vocabulary — no overlap on discriminating words

**What this means in plain English:**

When a customer sends a message, the agent reads the topic descriptions to decide which topic best matches. If two topics both mention the same words (like "login" or "access"), the agent cannot clearly distinguish between them.

For example, a message like *"I can't log in and I'm getting an error"* matches both Account Management (login) and Technical Support (error). The agent may route this to the wrong topic.

**Impact:** Ambiguous messages are routed unpredictably. Test cases that check topic selection will fail on edge cases involving these overlapping keywords.

**How to fix:**

> Step 1 — Make "login" and "access" exclusive to Account Management:
> Agent Builder → Topics tab → click **Technical Support** → Description field
> → remove the words `login` and `access` from the description → Save
>
> Step 2 — Add a tiebreaker to the agent instructions:
> Agent Builder → Agent Instructions field (top-level)
> → append: *"If a message mentions both login issues and technical errors, prefer Technical Support over Account Management only if the error is API-related or system-wide."*
> → Save

**Salesforce docs:** https://help.salesforce.com/s/articleView?id=sf.copilot_topics_config.htm

---

### 🟡 MEDIUM — Escalation topic has no plan if the escalation itself fails

**Rule:** DQ-11  
**Location:** `GenAiPlannerBundle > Escalation > genAiPluginInstructions`  
**Found:** `canEscalate=true` with escalation instructions, but no instruction for what to do if the transfer fails  
**Expected:** A fallback instruction like: *"If escalation fails, log a case or provide a contact number"*

**What this means in plain English:**

The Escalation topic correctly tries to transfer the customer to a human agent. But there is no instruction for what happens if that transfer fails — for example, if the queue is full, it is after business hours, or there is a routing error.

Without a fallback, the agent simply has no path forward. The customer waits, receives an error, or is disconnected with no resolution.

**Impact:** Any escalation failure leaves the customer stranded with no alternative offered.

**How to fix:**

> Setup → Agents → Acme Customer Service Agent → Open in Agent Builder
> → Topics tab → click **Escalation**
> → Instructions section → click **+ Add Instruction**
> → add as the final step:
>
> *"If escalation to a live agent is unavailable or fails, offer to log a support case for follow-up or provide a direct contact number. Do not attempt escalation again."*
>
> → Save

**Salesforce docs:** https://help.salesforce.com/s/articleView?id=sf.copilot_topic_instructions.htm

---

### 🟡 MEDIUM — General Inquiry topic: asks multiple questions at once instead of one

**Rule:** DQ-06  
**Location:** `GenAiPlannerBundle > General Inquiry > genAiPluginInstructions`  
**Found:** Instructions say "ask the customer questions to clarify their request" (plural, no single-question constraint)  
**Expected:** "Ask exactly one clarifying question per turn"

**What this means in plain English:**

When a message is unclear, the General Inquiry topic is supposed to ask a clarifying question. But its instructions say "ask questions" (plural) without limiting it to one at a time.

In practice, this means the agent may respond with two or three questions in a single message, like:
*"Could you tell me your account number? Also, what is the error message you're seeing? And when did this start?"*

Customers find this overwhelming and often answer only the first question, leading to a longer, slower conversation.

**Impact:** Customer satisfaction drops when multiple questions are asked at once. Longer conversations, lower resolution rates.

**How to fix:**

> Setup → Agents → Acme Customer Service Agent → Open in Agent Builder
> → Topics tab → click **General Inquiry**
> → Instructions section → find the step that says "ask questions"
> → change it to: *"Ask exactly one targeted clarifying question per turn. Wait for the customer to answer before asking the next question."*
> → Save

**Salesforce docs:** https://help.salesforce.com/s/articleView?id=sf.copilot_topic_instructions.htm

---

### 🔵 LOW — Technical Support topic: scope does not say what it will NOT handle

**Rule:** DQ-10  
**Location:** `GenAiPlannerBundle > Technical Support > scope`  
**Found:** Scope describes what Technical Support handles, but does not exclude adjacent topics  
**Expected:** Scope includes: *"You are not responsible for [billing / account changes / password resets]"*

**What this means in plain English:**

Each topic's scope tells the agent what that topic is responsible for. But if the scope only describes what the topic *does* handle — without saying what it *does not* handle — the agent has no way to reject an ambiguous message.

For example, if a customer says *"I can't log in and I got a bill I don't recognize,"* Technical Support's scope doesn't say it should reject billing questions. So it may accept the whole message and try to handle the billing part too.

**Impact:** Ambiguous messages that mention both technical and non-technical topics may be mis-handled. Low immediate risk, but increases routing failure rate over time.

**How to fix:**

> Setup → Agents → Acme Customer Service Agent → Open in Agent Builder
> → Topics tab → click **Technical Support**
> → Scope field → append to the end:
>
> *"You are not responsible for billing disputes, account changes, or password resets. Direct those requests to the appropriate topic."*
>
> → Save

**Salesforce docs:** https://help.salesforce.com/s/articleView?id=sf.copilot_topic_scope.htm

---

### 🔵 LOW — Voice surface: call recording policy not explicitly set

**Rule:** PM-21  
**Location:** `GenAiPlannerBundle > plannerSurfaces > Telephony > callRecordingAllowed`  
**Found:** `callRecordingAllowed` field is absent — platform default applies  
**Expected:** An explicit `true` or `false` based on the org's compliance requirements

**What this means in plain English:**

This voice agent has a phone (Telephony) surface but has not explicitly stated whether call recording is allowed or not. The platform will apply its own default, which may not match the org's legal or compliance requirements.

If the org is in a region that requires call recording consent disclosure, and recordings are happening without the required disclosure, this is a compliance risk.

**Impact:** Potential compliance risk if call recording defaults do not match org policy. Low immediate impact but should be explicitly set before go-live.

**How to fix:**

> Step 1 — Check with your compliance or legal team whether call recording is permitted and consented in your deployment region.
>
> Step 2 — Set the value explicitly:
> `sf project retrieve start --metadata 'GenAiPlannerBundle:AcmeCustomerServiceAgent' --target-org acme-prod`
> → open `.genAiPlannerBundle`
> → find `<plannerSurfaces>` with `<surfaceType>Telephony</surfaceType>`
> → add `<callRecordingAllowed>true</callRecordingAllowed>` or `<callRecordingAllowed>false</callRecordingAllowed>`
> → deactivate → deploy → reactivate

**Salesforce docs:** https://help.salesforce.com/s/articleView?id=sf.voice_call_recording.htm

---

## What Passed

These checks ran and found no issues.

✅ **DQ-03** (4 checks) — No knowledge base action exposed in topics that exclude KB  
✅ **DQ-04** (5 checks) — All sensitive operations (password reset, refund, permissions) are gated behind escalation  
✅ **DQ-12** (1 check) — Agent topics cover all major intent categories mentioned in agent description  
✅ **DQ-13** (5 checks) — All topics with no actions have information-only scope (no misleading action verbs)  
✅ **PM-07** (1 check) — Voice surface detected and Omni-Channel routing configuration is present  
✅ **PM-08** (3 checks) — Topics with escalation instructions have canEscalate set correctly  
✅ **PM-12** (1 check) — Voice surface has an outbound escalation route configured  
✅ **PM-13** (1 check) — Voice escalation route has a hold message so callers don't hear silence  
✅ **PM-14** (1 check) — Omni-Channel flow referenced in the escalation route exists and is active  
✅ **PM-15** (1 check) — Contact Center exists in org with a PSTN number assigned  
✅ **PM-17** (5 checks) — No duplicate topic developer names  
✅ **PM-18** (8 checks) — No duplicate action developer names within any topic  
✅ **PM-22** (1 check) — Agent has at least one topic linked  
✅ **PM-23** (5 checks) — All topic links point to defined topics (no dangling references)  
✅ **PM-24** (8 checks) — All action links point to defined actions (no dangling references)  
✅ **PM-25** (1 check) — plannerType matches the deployed surface (voice agent has Telephony surface)  
✅ **PM-01** (8 checks) — These actions correctly have isConfirmationRequired=false  
✅ **PM-16** (12 checks) — These instructions are under 4,000 characters  

---

## Priority fix order

If you want to work through these systematically, do them in this order:

1. **PM-11 (Critical)** — Fix the SEL rule blocking phone calls. Nothing works until this is done for voice.
2. **PM-08 (Critical)** — Fix `canEscalate` on Password Reset. Metadata-only fix, 10 minutes with the CLI.
3. **PM-01 (Critical)** — Uncheck Require Confirmation on the Create Case action. 2 minutes in Agent Builder.
4. **DQ-05 (High)** — Add the topic-first instruction to agent instructions. 2 minutes in Agent Builder.
5. **PM-16 (High)** — Split the 3 oversized Sales Research instructions. Takes ~30 minutes of editing.
6. **DQ-02 (High)** — Add fallback instructions to Create Case. 2 minutes per topic in Agent Builder.
7. **DQ-14 (High)** — Remove KB reference from Billing Inquiry instructions.
8. **DQ-01, DQ-11, DQ-06 (Medium)** — Routing overlap, escalation fallback, single question rule.
9. **DQ-10, PM-21 (Low)** — Scope exclusions and call recording policy.

---

## About this report

**What AgentEval checks:** Static agent configuration — the instructions, actions, topics, and surface settings stored in your Salesforce org. No agent is invoked. No test conversations are run. No data is written to your org.

**What it does not check:** Agent response quality, live conversation behavior, or integration correctness. Those are evaluated in Phase 2 (behavioral evaluation, coming soon).

**Rule sources:**
- Platform Mechanics rules (PM-xx) are sourced from Salesforce platform documentation and are objective pass/fail checks.
- Design Quality rules (DQ-xx) are sourced from Salesforce Well-Architected principles and empirical observations confirmed across 17 agents in 3 real Salesforce orgs.

---

*Generated by AgentEval v0.2.0 — Phase 1: Static Config Analysis*  
*Rules sourced from Salesforce platform docs, Well-Architected principles, and confirmed empirical observations.*  
*This file is an illustrative example. Agent and org names are fictional.*
