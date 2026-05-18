# Rubric — Platform Mechanics

Rules in this file are derived from official Salesforce documentation and are
objective — either the config is valid or it is not.

Each rule cites its source. If Salesforce changes the platform, update the rule
and the citation together.

---

## PM-01 — isConfirmationRequired must be false for autonomous actions

**Applies to:** All `localActions` in `GenAiPlannerBundle` and `GenAiPlugin`

**Rule:** Any action intended to execute without user confirmation must have
`isConfirmationRequired` set to `false`. The platform default is `true`.

**When violated:** The agent pauses execution and asks the user "Would you like
me to proceed?" before calling the action. This causes `output_validation` failure
in automated test runs because the response is a question, not an action confirmation.

**Config location:** `GenAiPlannerBundle > localActions > isConfirmationRequired`

**Source:**
- Salesforce Help: "Configure Agent Actions" — isConfirmationRequired field description
- https://help.salesforce.com/s/articleView?id=sf.copilot_actions_configure.htm

---

## PM-02 — invocationTarget must be a registered org action (detectable from known bad patterns)

**Applies to:** `localActions.invocationTarget` and `plannerActions.invocationTarget`

**Rule:** The value of `invocationTarget` is validated against the org's registered
action registry at deploy time. Only actions from installed managed packages or
explicitly registered flows are accepted. Custom flow API names are rejected.

**When violated:** Deploy fails with "bad value for restricted picklist field."
The agent cannot be activated or tested.

**Config location:** `GenAiPlannerBundle > localActions > invocationTarget`

**Source:**
- Salesforce Help: "Agentforce Actions" — invocation target constraints
- https://help.salesforce.com/s/articleView?id=sf.copilot_actions.htm

---

## PM-03 — Session-injected actions return empty in headless test runs

**Applies to:** Actions that require `verifiedCustomerID` or other session-context
parameters (e.g. `SvcCopilotTmpl__CreateCaseEnhancedData`)

**Rule:** Actions that depend on session-injected parameters silently return an
empty response in headless test runs (`sf agent test run`). No error is raised.
The agent must have a fallback instruction for the empty-response case.

**When violated:** Agent receives empty action response, has no instruction for
that case, and either says "system error" or gives no confirmation — both cause
`output_validation` failure.

**Config location:** Topic `genAiPluginInstructions` — must include a
"if no record returned" fallback step

**Source:**
- Salesforce Help: "Test Agentforce Agents" — headless test limitations
- https://help.salesforce.com/s/articleView?id=sf.einstein_agent_testing.htm
- Salesforce Help: "Create Case with Enhanced Data action" — verifiedCustomerID requirement
- https://help.salesforce.com/s/articleView?id=sf.copilot_service_actions.htm

---

## PM-04 — Package-dependent actions require installed managed package

**Applies to:** Actions from managed packages (e.g. `createIncidentForRequestor`
requires Field Service / Employee Service package)

**Rule:** Actions sourced from managed packages return empty `{}` if the package
is not installed in the org. No error is raised at deploy time or test time.

**When violated:** Agent calls the action, receives `{}`, and proceeds without
any meaningful data — typically producing a generic or incorrect response.

**Config location:** `GenAiPlannerBundle > localActions > source` (managed package namespace prefix)

**Source:**
- Salesforce Help: "Agentforce for Employee Service" — package dependency requirements
- https://help.salesforce.com/s/articleView?id=sf.copilot_employee_service.htm

---

## PM-05 — Flow must have environments: Default to appear in Agent Builder

**Applies to:** Custom flows used as agent actions

**Rule:** A flow is only visible in the Agent Builder action picker if it has
`environments: Default` set in its metadata. Without this, the flow deploys
successfully but cannot be connected to a topic.

**Config location:** `Flow-meta.xml > environments`

**Source:**
- Salesforce Help: "Create a Flow for an Agentforce Action"
- https://help.salesforce.com/s/articleView?id=sf.flow_build_autolaunched_flow.htm

---

## PM-06 — GenAiPlannerBundle cannot be deployed while agent is Active

**Applies to:** `GenAiPlannerBundle` deploys via Salesforce CLI

**Rule:** Deploying a `GenAiPlannerBundle` while the agent is in Active state
fails with "Cannot update record as Agent is Active." The agent must be
deactivated before deploy and reactivated after.

**Config location:** Agent Builder → agent status toggle

**Source:**
- Salesforce Help: "Deploy Agentforce Agents" — deployment prerequisites
- https://help.salesforce.com/s/articleView?id=sf.einstein_agent_deploy.htm

---

## PM-07 — Voice agents require Omni-Channel routing configuration

**Applies to:** Agents deployed on Voice surface (Einstein Conversation Intelligence)

**Rule:** Voice agents must have a corresponding Omni-Channel flow or queue
configured to handle escalations. Without it, `escalateToAgent` actions will
fail silently — the call is not transferred and no error is returned to the agent.

**Config location:**
- `GenAiPlannerBundle > plannerSurfaces > surfaceType: Voice`
- Omni-Channel: Setup → Routing Configurations → Voice queue assignment

**Source:**
- Salesforce Help: "Set Up Voice for Agentforce"
- https://help.salesforce.com/s/articleView?id=sf.voice_setup_agentforce.htm
- Salesforce Help: "Omni-Channel Routing for Voice"
- https://help.salesforce.com/s/articleView?id=sf.omnichannel_routing_voice.htm

---

## PM-08 — Escalate to Human action requires canEscalate: true on topic

**Applies to:** Any topic that contains an escalation instruction or `EscalateToAgent` action

**Rule:** A topic can only escalate to a human agent if `canEscalate` is set to
`true` in the topic's `GenAiPlugin` definition. If `canEscalate` is `false`,
the escalation instruction in the topic is silently ignored — the agent does not
transfer, produces no error, and continues handling the conversation.

**Config location:** `GenAiPlugin > canEscalate`

**Source:**
- Salesforce Help: "Configure Topic Escalation"
- https://help.salesforce.com/s/articleView?id=sf.copilot_topics_escalate.htm

---

## PM-09 — Voice escalation requires transfer number or queue, not just canEscalate

**Applies to:** Voice agents with human escalation paths

**Rule:** For voice agents, `canEscalate: true` alone is insufficient. A transfer
destination — either a direct dial number or an Omni-Channel queue — must be
configured in the Voice setup. Without a destination, the escalation action is
accepted by the platform but the call is dropped rather than transferred.

**Config location:**
- Agent Builder → Topic → Escalation settings → Transfer destination
- Setup → Voice → Call Center configuration → Transfer targets

**Source:**
- Salesforce Help: "Configure Human Escalation for Voice Agents"
- https://help.salesforce.com/s/articleView?id=sf.voice_agentforce_escalation.htm

---

## PM-10 — Omni-Channel presence rules must include agent bot routing capacity

**Applies to:** Chat and messaging agents with human fallback routing

**Rule:** When an AI agent is configured to hand off to a human agent via
Omni-Channel, the routing configuration must assign capacity to both the bot
channel and the human queue. If bot routing capacity is not defined, Omni-Channel
routes all conversations directly to human agents, bypassing the AI agent entirely.

**Config location:** Setup → Omni-Channel → Routing Configurations → Capacity model

**Source:**
- Salesforce Help: "Omni-Channel Capacity Model"
- https://help.salesforce.com/s/articleView?id=sf.omnichannel_capacity_model.htm
- Salesforce Well-Architected: "Agent Routing Design"
- https://architect.salesforce.com/well-architected/agentforce/routing

---

## PM-11 — SEL rule must not block unauthenticated inbound voice calls

**Applies to:** Voice agents (`plannerType: Atlas__VoiceAgent`) with `ruleExpressions`

**Rule:** A Session Eligibility Logic (SEL) rule that gates on `isVerified=true`
will block every unauthenticated inbound PSTN call. `isVerified` is `null` or
`false` at the moment a raw PSTN call arrives — it is only set to `true` if the
inbound Omni-Channel flow explicitly performs a caller authentication step before
handing off to the agent. Without that authentication step in the flow, the SEL
rule causes every call to be rejected silently — the agent never starts, the
caller gets no response.

**When violated:** PSTN calls do not reach the agent. No error is raised.
The call is dropped or falls through to a default IVR.

**Config location:** `GenAiPlannerBundle > ruleExpressions > conditions > leftOperand: isVerified`

**Fix options:**
1. Remove the `ruleExpressions` block from the bundle entirely if all callers
   should reach the agent without prior verification.
2. If verification is required: ensure the inbound Omni-Channel flow sets
   `isVerified=true` via a caller authentication step before routing to the agent.

**Note:** This field is not exposed in the Agent Builder UI — fix requires
metadata retrieve, XML edit, and redeploy.

**Source:**
- Salesforce Help: "Agentforce Voice Session Eligibility"
- https://help.salesforce.com/s/articleView?id=sf.voice_agentforce_eligibility.htm
- Empirical: confirmed in Agentforce_Voice_Service_Agent — PSTN calls could not
  reach agent, root cause was isVerified SEL rule with no authentication flow

---

## PM-12 — Voice surface must have outboundRouteConfigs defined

**Applies to:** Agents with `surfaceType: Telephony` in `plannerSurfaces`

**Rule:** A Telephony surface must have at least one `outboundRouteConfigs` block
defining where to route escalated calls. Without it, the voice surface is
registered but has no escalation path — the agent can answer calls but cannot
transfer them to a human agent under any circumstances.

**Config location:** `GenAiPlannerBundle > plannerSurfaces > surfaceType: Telephony > outboundRouteConfigs`

**Source:**
- Salesforce Help: "Configure Voice Escalation Routes"
- https://help.salesforce.com/s/articleView?id=sf.voice_agentforce_routes.htm

---

## PM-13 — Voice escalation must include an escalation message for the caller

**Applies to:** `outboundRouteConfigs` blocks in Telephony surface

**Rule:** Every `outboundRouteConfigs` block must include an `escalationMessage`.
This is the message spoken to the caller while the transfer is in progress
(e.g. "Connecting call to human agent. Please wait."). Without it, the caller
hears silence during the transfer — typically lasting 5-15 seconds — which
causes callers to hang up assuming the call dropped.

**Config location:** `GenAiPlannerBundle > plannerSurfaces > Telephony > outboundRouteConfigs > escalationMessage`

**Source:**
- Salesforce Help: "Voice Transfer Experience"
- https://help.salesforce.com/s/articleView?id=sf.voice_transfer_message.htm

---

## PM-14 — Omni-Channel flow referenced in outboundRouteConfigs must exist in org

**Applies to:** `outboundRouteConfigs > outboundRouteName` where `outboundRouteType: OmniChannelFlow`

**Rule:** The flow name in `outboundRouteName` must exist as an active
Omni-Channel flow in the org. If the referenced flow was deleted, renamed, or
never deployed, the voice transfer silently fails at runtime — the agent attempts
the transfer, gets no response, and the call may be dropped.

**Config location:** `GenAiPlannerBundle > plannerSurfaces > Telephony > outboundRouteConfigs > outboundRouteName`

**How to verify:** Check Setup → Process Automation → Flows → filter by
Omni-Channel Flow type — the named flow must appear and be Active.

**Source:**
- Salesforce Help: "Omni-Channel Flows for Voice"
- https://help.salesforce.com/s/articleView?id=sf.omnichannel_flow_voice.htm

---

## PM-15 — A Contact Center must exist in the org for voice agents

**Applies to:** All agents with `plannerType: Atlas__VoiceAgent`

**Rule:** A voice agent requires a Contact Center record in the org to receive
inbound PSTN calls. The Contact Center maps the PSTN number to the Salesforce
Voice infrastructure. Without a Contact Center, inbound calls have no entry
point into Salesforce regardless of how the agent is configured.

**How to verify:** Setup → Feature Settings → Service → Contact Center →
Contact Centers — at least one record must exist and have a phone number assigned.

---

## PM-16 — Topic instruction text must not exceed platform character limit

**Applies to:** All `genAiPluginInstructions > description` fields

**Rule:** Salesforce silently truncates individual instruction text that exceeds
4,000 characters. The deploy succeeds with no warning. The truncated instruction
is incomplete at runtime — the agent receives a partial instruction with no
indication that content is missing.

**When violated:** Agent behavior appears inconsistent — some instruction steps
execute correctly, others are partially followed or ignored entirely with no
error raised.

**Config location:** `GenAiPlannerBundle > localTopics > genAiPluginInstructions > description`

**Source:**
- Salesforce Help: "Agent Topic Instruction Limits"
- https://help.salesforce.com/s/articleView?id=sf.copilot_topic_limits.htm
- Empirical: confirmed in Sales_Agent — 6 instructions truncated, some at 8000+ chars.
  Agent behavior was inconsistent across instructions in the same topic.

---

## PM-17 — Duplicate localDeveloperName across topics causes unpredictable routing

**Applies to:** `localTopics > localDeveloperName` across all topics in a bundle

**Rule:** Each topic must have a unique `localDeveloperName`. Duplicate names
cause the planner to route to the first matching topic regardless of trigger
word matching — the second topic with the same name is effectively unreachable.

**When violated:** One topic is silently shadowed. All messages that should route
to the shadowed topic go to the first topic with the same developer name.
No error is raised at deploy or runtime.

**Config location:** `GenAiPlannerBundle > localTopics > localDeveloperName`

**Source:**
- Salesforce Help: "Agentforce Topic Configuration Reference"
- https://help.salesforce.com/s/articleView?id=sf.copilot_topics_config.htm

---

## PM-18 — Action localDeveloperName must be unique within a topic

**Applies to:** `localActions > localDeveloperName` within each topic

**Rule:** Each action within a topic must have a unique `localDeveloperName`.
Duplicate action names within the same topic cause the planner to call the
first matching action and ignore subsequent ones — silent data loss.

**Config location:** `GenAiPlannerBundle > localTopics > localActions > localDeveloperName`

**Source:**
- Salesforce Help: "Agentforce Action Configuration Reference"
- https://help.salesforce.com/s/articleView?id=sf.copilot_actions_config.htm

---

## PM-19 — progressIndicatorMessage required for actions over 2 seconds on voice

**Applies to:** `localActions` in voice agents (`plannerType: Atlas__VoiceAgent`)

**Rule:** On a voice channel, if an action takes more than ~2 seconds the caller
hears silence. Every action in a voice agent must have `isIncludeInProgressIndicator`
set to `true` and a meaningful `progressIndicatorMessage` (e.g. "One moment while
I look that up."). Without it the caller experiences dead air and typically
hangs up assuming the call dropped.

**Config location:** `GenAiPlannerBundle > localTopics > localActions > progressIndicatorMessage`

**Source:**
- Salesforce Help: "Voice Agent Action Progress Indicators"
- https://help.salesforce.com/s/articleView?id=sf.voice_agentforce_progress.htm

---

## PM-20 — adaptiveResponseAllowed must be true for voice surface

**Applies to:** `plannerSurfaces` blocks where `surfaceType: Telephony`

**Rule:** The Telephony surface must have `adaptiveResponseAllowed` set to `true`.
When false, the agent produces a fixed-format response unsuitable for text-to-speech
conversion — the caller hears a raw structured response including field labels,
brackets, and formatting characters.

**Config location:** `GenAiPlannerBundle > plannerSurfaces > surfaceType: Telephony > adaptiveResponseAllowed`

**Source:**
- Salesforce Help: "Configure Voice Agent Surfaces"
- https://help.salesforce.com/s/articleView?id=sf.voice_agentforce_surfaces.htm

---

## PM-21 — callRecordingAllowed must be explicitly set on voice surface

**Applies to:** `plannerSurfaces > surfaceType: Telephony`

**Rule:** `callRecordingAllowed` must be explicitly set to match your org's
compliance requirements. The platform default varies by org type. If your org
requires call recording consent disclosures and this is false, recordings may
be made without the required disclosure. If your compliance policy prohibits
recording and this is true, you may violate data retention policies.

**Config location:** `GenAiPlannerBundle > plannerSurfaces > surfaceType: Telephony > callRecordingAllowed`

**Source:**
- Salesforce Help: "Voice Call Recording Compliance"
- https://help.salesforce.com/s/articleView?id=sf.voice_recording_compliance.htm

---

## PM-22 — Agent must have at least one topic linked via localTopicLinks

**Applies to:** All agents

**Rule:** A planner bundle with no `localTopicLinks` entries has no topics to
route to. The agent will accept conversations but route every message to the
fallback global actions only (typically AnswerQuestionsWithKnowledge). All
topic-specific guardrails, action bindings, and scopes are bypassed entirely.

**Config location:** `GenAiPlannerBundle > localTopicLinks`

**Source:**
- Salesforce Help: "Add Topics to an Agentforce Agent"
- https://help.salesforce.com/s/articleView?id=sf.copilot_topics_add.htm

---

## PM-23 — Topic linked in localTopicLinks must have a matching localTopics entry

**Applies to:** `localTopicLinks > genAiPluginName` cross-referenced against `localTopics > fullName`

**Rule:** Every plugin name in `localTopicLinks` must have a corresponding
`localTopics` block in the same bundle. A dangling reference causes the planner
to attempt routing to a topic that has no instructions — resulting in a silent
no-op or runtime error.

**Config location:** `GenAiPlannerBundle > localTopicLinks > genAiPluginName`

**Source:**
- Salesforce Help: "GenAiPlannerBundle Metadata Reference"
- https://help.salesforce.com/s/articleView?id=sf.meta_types_genaiplanners.htm

---

## PM-24 — Action linked in localActionLinks must have a matching localActions entry

**Applies to:** `localActionLinks > functionName` cross-referenced against `localActions > fullName`

**Rule:** Every function name in `localActionLinks` must have a corresponding
`localActions` block. A dangling action link causes the planner to attempt to
call an action with no invocation configuration — silent failure at runtime.

**Config location:** `GenAiPlannerBundle > localTopics > localActionLinks > functionName`

**Source:**
- Salesforce Help: "GenAiPlannerBundle Metadata Reference"
- https://help.salesforce.com/s/articleView?id=sf.meta_types_genaiplanners.htm

---

## PM-25 — plannerType must match the deployed surface

**Applies to:** `GenAiPlannerBundle > plannerType`

**Rule:** The `plannerType` must be consistent with the surfaces the agent is
deployed on. `Atlas__VoiceAgent` agents must only be deployed on Telephony
surfaces. `AiCopilot__ReAct` agents must only be on CustomerWebClient or
Messaging surfaces. A mismatch between plannerType and surfaceType causes
the agent to behave incorrectly — either failing to process voice input or
producing responses incompatible with the channel format.

**Config location:** `GenAiPlannerBundle > plannerType` vs `plannerSurfaces > surfaceType`

**Source:**
- Salesforce Help: "Agentforce Agent Types and Surfaces"
- https://help.salesforce.com/s/articleView?id=sf.copilot_agent_types.htm

**Note:** The existence of a Contact Center is detectable via metadata query.
Whether the correct PSTN number is assigned requires a manual UI check.

**Source:**
- Salesforce Help: "Set Up a Contact Center"
- https://help.salesforce.com/s/articleView?id=sf.voice_contact_center_setup.htm
- Salesforce Help: "Assign Phone Numbers to a Contact Center"
- https://help.salesforce.com/s/articleView?id=sf.voice_phone_numbers.htm
