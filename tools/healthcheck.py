#!/usr/bin/env python3
"""
AgentEval Phase 1 — Static Config Health Check

Retrieves an Agentforce agent configuration from a Salesforce org and
evaluates it against rubric rules derived from Salesforce platform docs
and confirmed empirical observations.

Usage:
    python tools/healthcheck.py --org myOrg --agent MyAgentApiName
    python tools/healthcheck.py --org myOrg --list-agents
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# Rule catalogue — maps rule prefix to human-readable category, and each rule
# ID to a one-line description. Used in report rendering so viewers see
# "Platform Mechanics 22 — Agent must have at least one topic linked"
# instead of the bare "PM-22".
RULE_CATEGORIES = {
    "PM": "Platform Mechanics",
    "DQ": "Design Quality",
}

RULE_DESCRIPTIONS = {
    "PM-01": "Read actions should not require confirmation (writes correctly require it)",
    "PM-07": "Voice surface requires Omni-Channel routing configuration",
    "PM-08": "Topics with escalation instructions must have canEscalate: true",
    "PM-11": "SEL rule must not block unauthenticated inbound voice calls",
    "PM-12": "Voice surface must have outboundRouteConfigs defined",
    "PM-13": "Voice escalation must include an escalation message for the caller",
    "PM-14": "Omni-Channel flow referenced in outboundRouteConfigs must exist in org",
    "PM-15": "A Contact Center must exist in the org for voice agents",
    "PM-16": "Topic instruction text must not exceed 4000 character platform limit",
    "PM-17": "Duplicate localDeveloperName across topics causes unpredictable routing",
    "PM-18": "Action localDeveloperName must be unique within a topic",
    "PM-19": "Voice agent actions must have progressIndicatorMessage set",
    "PM-20": "adaptiveResponseAllowed must be true for voice/telephony surface",
    "PM-21": "callRecordingAllowed must be explicitly set on voice surface",
    "PM-22": "Agent must have at least one topic linked via localTopicLinks",
    "PM-23": "Topic linked in localTopicLinks must have a matching localTopics entry",
    "PM-24": "Action in localActionLinks must have a matching localActions entry",
    "PM-25": "plannerType must match the deployed surface",
    "DQ-01": "Topic trigger words must not overlap across topics",
    "DQ-02": "Every action path must have a fallback for empty response",
    "DQ-03": "KB action must not be in genAiFunctions of action-only topics",
    "DQ-04": "Sensitive operations must be gated behind human escalation",
    "DQ-05": "Planner must commit to topic before calling any action",
    "DQ-06": "Fallback topic must ask exactly one clarifying question",
    "DQ-10": "Topic scope must explicitly exclude adjacent topic responsibilities",
    "DQ-11": "Escalation topic must not create an escalation loop",
    "DQ-12": "Agent must have topics covering all intent categories in its description",
    "DQ-13": "Topic with no actions must have a response-only scope",
    "DQ-14": "Topic instructions must not contradict topic scope",
}


# Build sequential numbering within each category so viewers see
# "Platform Mechanics 1, 2, 3..." instead of the gappy original IDs
# (PM-01, PM-07, PM-08...). The mapping is stable: a given rule ID
# always maps to the same sequential number across all reports.
def _build_sequential_index() -> dict[str, int]:
    by_category: dict[str, list[str]] = {}
    for rid in RULE_DESCRIPTIONS.keys():
        prefix = rid.split("-", 1)[0]
        by_category.setdefault(prefix, []).append(rid)
    index: dict[str, int] = {}
    for prefix, rids in by_category.items():
        for i, rid in enumerate(sorted(rids), start=1):
            index[rid] = i
    return index


RULE_SEQUENTIAL_INDEX = _build_sequential_index()


def format_rule_label(rule_id: str) -> str:
    """Render a rule ID as 'Platform Mechanics 3' or 'Design Quality 7'.

    Uses sequential numbering within each category so reports never
    show gappy original IDs (e.g. PM-22 -> Platform Mechanics 15, not 22).
    """
    base = rule_id.split(":")[0]                     # strip any ':topic_name' suffix
    if "-" in base:
        prefix = base.split("-", 1)[0]
        category = RULE_CATEGORIES.get(prefix, prefix)
        seq = RULE_SEQUENTIAL_INDEX.get(base)
        if seq is not None:
            return f"{category} {seq}"
        return category
    return base


def format_rule_with_desc(rule_id: str) -> str:
    """'Platform Mechanics 3 — Agent must have at least one topic linked'."""
    base = rule_id.split(":")[0]
    label = format_rule_label(rule_id)
    desc = RULE_DESCRIPTIONS.get(base)
    return f"{label} — {desc}" if desc else label

@dataclass
class Finding:
    rule_id: str
    severity: str          # critical | high | medium | low
    title: str
    location: str          # config file + field path
    observed: str          # what we found
    expected: str          # what it should be
    impact: str            # runtime behavior when violated
    fix: str               # specific actionable fix
    candidate: bool = False  # True = not yet confirmed across 3+ agents

@dataclass
class CheckResult:
    rule_id: str
    passed: bool
    finding: Optional[Finding] = None


@dataclass
class HealthReport:
    agent_name: str
    org_alias: str
    run_timestamp: str
    findings: list[Finding] = field(default_factory=list)
    passed_checks: list[str] = field(default_factory=list)
    skipped_checks: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Org interaction
# ---------------------------------------------------------------------------

NS = "http://soap.sforce.com/2006/04/metadata"

def run_sf(args: list[str], cwd: str = ".") -> tuple[int, str, str]:
    result = subprocess.run(
        ["sf"] + args,
        capture_output=True, text=True, cwd=cwd
    )
    return result.returncode, result.stdout, result.stderr


def check_org_access(org: str) -> None:
    """Pre-flight: verify org is authenticated and user has Metadata API access."""
    # Step 1 — check org is authenticated
    code, out, err = run_sf(["org", "display", "--target-org", org, "--json"])
    if code != 0:
        combined = (out + err).lower()
        if "not found" in combined or "no authorization" in combined or "expired" in combined:
            print(f"\nError: org '{org}' is not authenticated or session has expired.")
            print(f"Fix: sf org login web --alias {org} --instance-url https://login.salesforce.com")
        else:
            print(f"\nError: could not connect to org '{org}'.\n{err.strip()}")
        sys.exit(1)

    # Step 2 — check Metadata API access by attempting a lightweight metadata list
    code, out, err = run_sf([
        "org", "list", "metadata",
        "--metadata-type", "CustomObject",
        "--target-org", org,
        "--json"
    ])
    if code != 0:
        combined = (out + err)
        if "INSUFFICIENT_ACCESS" in combined or "ModifyAllData" in combined or "ModifyMetadata" in combined:
            try:
                data = json.loads(out)
                username = data.get("result", {}).get("username", "this user")
            except Exception:
                username = "this user"
            print(f"\nError: '{username}' does not have Metadata API access in org '{org}'.")
            print("AgentEval requires one of these permissions:")
            print("  - Modify All Data")
            print("  - Modify Metadata Through Metadata API Interactions")
            print(f"\nFix: Setup → Users → {username} → Edit → grant one of the above permissions → Save")
            print("Then retry: python3 tools/healthcheck.py --org {org} --agent <AgentName>")
        else:
            print(f"\nError: Metadata API check failed for org '{org}'.\n{combined.strip()}")
        sys.exit(1)


def list_agents(org: str) -> list[str]:
    code, out, err = run_sf([
        "org", "list", "metadata",
        "--metadata-type", "GenAiPlannerBundle",
        "--target-org", org,
        "--json"
    ])
    if code != 0:
        print(f"Error listing agents: {err}", file=sys.stderr)
        sys.exit(1)
    data = json.loads(out)
    records = data.get("result", [])
    return sorted(r.get("fullName", "") for r in records if r.get("fullName"))


def retrieve_agent_config(org: str, agent_name: str, work_dir: str) -> bool:
    # sf project retrieve requires a valid SFDX project — create a minimal one in work_dir
    sfdx_project = {
        "packageDirectories": [{"path": "force-app", "default": True}],
        "sourceApiVersion": "66.0"
    }
    project_json = os.path.join(work_dir, "sfdx-project.json")
    with open(project_json, "w") as f:
        json.dump(sfdx_project, f)
    os.makedirs(os.path.join(work_dir, "force-app"), exist_ok=True)

    code, out, err = run_sf([
        "project", "retrieve", "start",
        "--metadata", f"GenAiPlannerBundle:{agent_name}",
        "--target-org", org,
    ], cwd=work_dir)
    if code != 0:
        print(f"Retrieve failed:\n{err}", file=sys.stderr)
        return False
    return True


def find_bundle_file(work_dir: str, agent_name: str) -> Optional[Path]:
    pattern = "**/*.genAiPlannerBundle"
    matches = list(Path(work_dir).glob(pattern))
    if not matches:
        return None
    # prefer exact name match
    for m in matches:
        if agent_name in m.name:
            return m
    return matches[0]


def find_plugin_files(work_dir: str) -> list[Path]:
    return list(Path(work_dir).glob("**/*.genAiPlugin-meta.xml"))


# ---------------------------------------------------------------------------
# XML parsing helpers
# ---------------------------------------------------------------------------

def tag(name: str) -> str:
    return f"{{{NS}}}{name}"


def parse_bundle(path: Path) -> ET.Element:
    tree = ET.parse(path)
    return tree.getroot()


def get_local_topics(root: ET.Element) -> list[ET.Element]:
    return root.findall(tag("localTopics"))


def get_planner_actions(root: ET.Element) -> list[ET.Element]:
    return root.findall(tag("plannerActions"))


def get_topic_actions(topic: ET.Element) -> list[ET.Element]:
    return topic.findall(tag("localActions"))


def get_text(el: ET.Element, child_tag: str, default: str = "") -> str:
    child = el.find(tag(child_tag))
    if child is not None and child.text:
        return child.text.strip()
    return default


def get_planner_surfaces(root: ET.Element) -> list[ET.Element]:
    return root.findall(tag("plannerSurfaces"))


# ---------------------------------------------------------------------------
# Rules — Platform Mechanics
# ---------------------------------------------------------------------------

def _is_write_action(action_name: str, action_label: str) -> bool:
    """Heuristic: does this action mutate data?

    Write actions: Create*, Update*, Delete*, Add*, Submit*, Send*, Insert*,
    Patch*, Put*, Post*, Modify*, Remove*. For these, isConfirmationRequired=true
    is the CORRECT design (matches catalogue pattern AGENTFORCE-WELLARCH-TRUST-2).
    Read actions: Get*, List*, Search*, Find*, Lookup*, Fetch*, Retrieve*,
    Identify*. For these, confirmation adds friction without protecting anything.
    """
    write_prefixes = (
        "create", "update", "delete", "add", "submit", "send", "insert",
        "patch", "put", "post", "modify", "remove", "register", "enroll",
        "approve", "reject", "cancel", "schedule", "book",
    )
    name_lower = (action_name or "").lower()
    label_lower = (action_label or "").lower()
    return name_lower.startswith(write_prefixes) or label_lower.startswith(write_prefixes)


def check_pm01_confirmation_required(root: ET.Element) -> list[CheckResult]:
    """PM-01: confirmation required only when it adds friction without value.

    Per catalogue pattern AGENTFORCE-WELLARCH-TRUST-2, write actions SHOULD
    have isConfirmationRequired=true. PM-01 only flags read-style actions
    where confirmation adds friction without preventing harm.
    """
    results = []
    for topic in get_local_topics(root):
        topic_label = get_text(topic, "masterLabel", "unknown topic")
        for action in get_topic_actions(topic):
            action_label = get_text(action, "masterLabel", "unknown action")
            action_name = get_text(action, "localDeveloperName", "unknown")
            confirm = get_text(action, "isConfirmationRequired", "true")
            rule_id = f"PM-01:{topic_label}:{action_name}"
            is_write = _is_write_action(action_name, action_label)

            # Write action with confirmation: correct design — pass.
            # Write action without confirmation: catalogue WA-TRUST-2 violation
            # (let WA-TRUST-2 surface that; PM-01 stays silent).
            if is_write:
                results.append(CheckResult(rule_id=rule_id, passed=True))
                continue

            # Read action with confirmation: friction without value — flag.
            if confirm.lower() == "true":
                results.append(CheckResult(
                    rule_id=rule_id,
                    passed=False,
                    finding=Finding(
                        rule_id="PM-01",
                        severity="medium",
                        title=f"{topic_label}: read action requires confirmation ({action_label})",
                        location=f"GenAiPlannerBundle > {topic_label} > localActions > {action_name} > isConfirmationRequired",
                        observed="true",
                        expected="false (for read-style actions)",
                        impact=(
                            "This action looks like a read (Get/List/Search/Lookup), but it's "
                            "configured to ask the user 'Would you like me to proceed?' before running. "
                            "Confirmation on reads adds turn-by-turn friction without preventing any "
                            "destructive action. Also breaks automated test runs that expect a direct "
                            "answer. For WRITE actions (Create/Update/Delete), confirmation IS correct — "
                            "see catalogue pattern AGENTFORCE-WELLARCH-TRUST-2."
                        ),
                        fix=(
                            f"UI path: Setup → Agents → [your agent] → Open in Agent Builder → "
                            f"Topics tab → click '{topic_label}' → Actions section → click '{action_label}' → "
                            f"uncheck 'Require Confirmation' → Save. "
                            f"CLI path: retrieve bundle XML, set <isConfirmationRequired>false</isConfirmationRequired> "
                            f"under the '{action_name}' localActions block, deactivate agent, deploy, reactivate. "
                            f"Salesforce docs: https://help.salesforce.com/s/articleView?id=sf.copilot_actions_configure.htm"
                        ),
                    )
                ))
            else:
                results.append(CheckResult(rule_id=rule_id, passed=True))
    return results


def check_pm08_can_escalate(root: ET.Element) -> list[CheckResult]:
    """PM-08: Topics with escalation instructions must have canEscalate: true."""
    results = []
    escalation_signals = ["escalate", "transfer", "human agent", "human review", "handoff", "hand off"]
    for topic in get_local_topics(root):
        topic_label = get_text(topic, "masterLabel", "unknown topic")
        can_escalate = get_text(topic, "canEscalate", "false")
        scope = get_text(topic, "scope", "").lower()
        instructions_text = " ".join(
            get_text(instr, "description", "").lower()
            for instr in topic.findall(tag("genAiPluginInstructions"))
        )
        combined_text = scope + " " + instructions_text
        mentions_escalation = any(s in combined_text for s in escalation_signals)
        rule_id = f"PM-08:{topic_label}"
        if mentions_escalation and can_escalate.lower() != "true":
            results.append(CheckResult(
                rule_id=rule_id,
                passed=False,
                finding=Finding(
                    rule_id="PM-08",
                    severity="critical",
                    title=f"{topic_label}: escalation instruction exists but canEscalate is false",
                    location=f"GenAiPlannerBundle > {topic_label} > canEscalate",
                    observed="false",
                    expected="true",
                    impact="Escalation instruction in topic scope or step instructions is silently ignored. "
                           "Agent does not transfer to human — continues handling a conversation it should have escalated.",
                    fix=(
                        f"Note: canEscalate is NOT exposed in the Agent Builder UI — it requires a metadata fix. "
                        f"Step 1: sf project retrieve start --metadata 'GenAiPlannerBundle:[AgentName]' --target-org [alias]. "
                        f"Step 2: Open the .genAiPlannerBundle file, find topic '{topic_label}', "
                        f"change <canEscalate>false</canEscalate> to <canEscalate>true</canEscalate>. "
                        f"Step 3: Setup → Agents → [your agent] → Deactivate. "
                        f"Step 4: sf project deploy start --metadata 'GenAiPlannerBundle:[AgentName]' --target-org [alias]. "
                        f"Step 5: Setup → Agents → [your agent] → Activate. "
                        f"Also verify: Setup → Agents → [your agent] → Open in Agent Builder → "
                        f"Topics tab → '{topic_label}' → Escalation section has a transfer destination configured. "
                        f"Salesforce docs: https://help.salesforce.com/s/articleView?id=sf.copilot_topics_escalate.htm"
                    ),
                )
            ))
        else:
            results.append(CheckResult(rule_id=rule_id, passed=True))
    return results


def check_pm07_voice_omnichannel(root: ET.Element) -> list[CheckResult]:
    """PM-07: Voice surface requires Omni-Channel routing configuration."""
    surfaces = get_planner_surfaces(root)
    surface_types = [get_text(s, "surfaceType", "") for s in surfaces]
    rule_id = "PM-07:voice_surface"
    if "Voice" in surface_types:
        # We can only detect Voice surface presence — actual Omni-Channel config
        # lives outside the bundle. Flag as medium advisory.
        return [CheckResult(
            rule_id=rule_id,
            passed=False,
            finding=Finding(
                rule_id="PM-07",
                severity="medium",
                title="Voice surface detected — verify Omni-Channel routing is configured",
                location="GenAiPlannerBundle > plannerSurfaces > surfaceType: Voice",
                observed="Voice surface present in bundle",
                expected="Omni-Channel Voice queue and routing configuration confirmed in org setup",
                impact="If Omni-Channel Voice queue is not configured, escalateToAgent actions fail silently — "
                       "call is not transferred and no record is created.",
                fix=(
                    "Step 1 — Verify routing config: Setup → Omni-Channel → Routing Configurations → "
                    "confirm a routing config exists with Voice channel enabled and capacity allocated. "
                    "Step 2 — Verify queue: Setup → Omni-Channel → Queues → confirm a Voice queue exists "
                    "and is assigned to the routing config. "
                    "Step 3 — Verify agent surface: Setup → Agents → [your agent] → Open in Agent Builder → "
                    "Surfaces tab → Telephony → confirm outbound route is set. "
                    "Salesforce docs: https://help.salesforce.com/s/articleView?id=sf.omnichannel_routing_voice.htm"
                ),
            )
        )]
    return [CheckResult(rule_id=rule_id, passed=True)]


# ---------------------------------------------------------------------------
# Rules — Design Quality
# ---------------------------------------------------------------------------

def check_dq02_fallback_instructions(root: ET.Element) -> list[CheckResult]:
    """DQ-02: Every action path must have a fallback for empty response."""
    results = []
    fallback_signals = ["no record", "if no", "not returned", "still confirm", "regardless",
                        "even if", "if nothing", "if empty", "if the action"]
    for topic in get_local_topics(root):
        topic_label = get_text(topic, "masterLabel", "unknown topic")
        actions = get_topic_actions(topic)
        if not actions:
            continue
        instructions = topic.findall(tag("genAiPluginInstructions"))
        instructions_text = " ".join(
            get_text(i, "description", "").lower() for i in instructions
        )
        has_fallback = any(s in instructions_text for s in fallback_signals)
        rule_id = f"DQ-02:{topic_label}"
        if not has_fallback:
            results.append(CheckResult(
                rule_id=rule_id,
                passed=False,
                finding=Finding(
                    rule_id="DQ-02",
                    severity="high",
                    title=f"{topic_label}: no fallback instruction if action returns empty",
                    location=f"GenAiPlannerBundle > {topic_label} > genAiPluginInstructions",
                    observed="No 'if no record returned' or equivalent fallback instruction found",
                    expected="Explicit instruction for empty action response (e.g. 'If no caseRecord was returned, still confirm...')",
                    impact="When the action returns empty (session-injected params missing, package dependency unavailable, "
                           "or transient failure), the agent has no fallback instruction. The user receives a confusing "
                           "'system error' message — or no confirmation at all — instead of a graceful response. "
                           "Increases abandonment risk and erodes trust in the agent.",
                    fix=(
                        f"UI path: Setup → Agents → [your agent] → Open in Agent Builder → "
                        f"Topics tab → click '{topic_label}' → Instructions section → "
                        f"click '+ Add Instruction' → add as the last step: "
                        f"'If the action returned no record or an empty response, still confirm to the customer "
                        f"that their request has been received and the team will follow up. Do not say system error.' "
                        f"→ Save. "
                        f"Salesforce docs: https://help.salesforce.com/s/articleView?id=sf.copilot_topic_instructions.htm"
                    ),
                )
            ))
        else:
            results.append(CheckResult(rule_id=rule_id, passed=True))
    return results


def check_dq03_kb_in_action_topics(root: ET.Element, plugin_files: list[Path]) -> list[CheckResult]:
    """DQ-03: KB action must not be in genAiFunctions of action-only topics."""
    results = []
    kb_signals = ["answerquestions", "knowledge", "knowledgesearch", "kb"]
    no_kb_scope_signals = ["do not search the knowledge base", "do not use kb",
                           "do not call knowledge", "no knowledge base"]
    for topic in get_local_topics(root):
        topic_label = get_text(topic, "masterLabel", "unknown topic")
        scope = get_text(topic, "scope", "").lower()
        scope_excludes_kb = any(s in scope for s in no_kb_scope_signals)
        if not scope_excludes_kb:
            continue
        # Check for matching plugin file
        topic_dev_name = get_text(topic, "developerName", "")
        matching_plugins = [p for p in plugin_files if topic_dev_name in p.name or topic_label.replace(" ", "_") in p.name]
        for plugin_path in matching_plugins:
            try:
                plugin_tree = ET.parse(plugin_path)
                plugin_root = plugin_tree.getroot()
                functions = plugin_root.findall(f".//{tag('genAiFunctions')}")
                for func in functions:
                    func_name = get_text(func, "functionName", "").lower()
                    if any(s in func_name for s in kb_signals):
                        rule_id = f"DQ-03:{topic_label}"
                        results.append(CheckResult(
                            rule_id=rule_id,
                            passed=False,
                            finding=Finding(
                                rule_id="DQ-03",
                                severity="critical",
                                title=f"{topic_label}: knowledge base exposed via genAiFunctions despite scope exclusion",
                                location=f"{plugin_path.name} > genAiFunctions > functionName",
                                observed=f"genAiFunctions references KB action: {func_name}",
                                expected="genAiFunctions block removed or KB action not listed",
                                impact="genAiFunctions in plugin XML overrides bundle-level instructions. "
                                       "Agent calls knowledge base 4-6x before attempting the intended action. "
                                       "actions_assertion fails. Significant latency increase.",
                                fix=(
                                    f"This requires a metadata fix — not fixable in Agent Builder UI. "
                                    f"Step 1: sf project retrieve start --metadata 'GenAiPlugin:[plugin name]' --target-org [alias]. "
                                    f"Step 2: Open '{plugin_path.name}', find and delete the entire "
                                    f"<genAiFunctions>...</genAiFunctions> block. "
                                    f"Step 3: Setup → Agents → [your agent] → Deactivate. "
                                    f"Step 4: sf project deploy start --source-dir [path to plugin file] --target-org [alias]. "
                                    f"Step 5: Reactivate agent and rerun tests to confirm KB loop is gone. "
                                    f"Salesforce docs: https://help.salesforce.com/s/articleView?id=sf.copilot_plugin_functions.htm"
                                ),
                            )
                        ))
                        break
            except ET.ParseError:
                continue
    # If no issues found for any topic with no-KB scope
    if not any(not r.passed for r in results):
        results.append(CheckResult(rule_id="DQ-03:global", passed=True))
    return results


def check_dq01_topic_overlap(root: ET.Element) -> list[CheckResult]:
    """DQ-01: Topic trigger words must not overlap across topics."""
    topics = get_local_topics(root)
    topic_signals: dict[str, set[str]] = {}
    for topic in topics:
        label = get_text(topic, "masterLabel", "unknown")
        desc = get_text(topic, "description", "").lower()
        words = set(re.findall(r'\b\w+\b', desc))
        # Only keep domain-specific routing signal words — filter generic business
        # vocabulary that appears in every topic by design (verbs, conjunctions,
        # generic nouns). Overlap on these is expected, not a routing ambiguity.
        stopwords = {
            # conjunctions / prepositions
            "the", "a", "an", "and", "or", "to", "in", "for", "of", "is", "are",
            "this", "that", "with", "from", "at", "on", "as", "by", "if", "it",
            "be", "have", "has", "can", "will", "should", "must", "any", "all",
            "these", "those", "over", "even", "not", "do",
            # generic action verbs — appear in every topic
            "use", "select", "handle", "request", "requests", "manage", "create",
            "update", "delete", "send", "get", "set", "view", "search", "find",
            "help", "assist", "provide", "allow", "include", "make", "take",
            "belong", "belongs", "work", "works", "need", "needs", "want",
            # generic business nouns — expected cross-topic vocabulary
            "user", "users", "customer", "customers", "agent", "agents", "topic",
            "report", "reports", "record", "records", "data", "field", "fields",
            "information", "details", "issue", "issues", "question", "questions",
            "item", "items", "type", "types", "related", "relevant", "specific",
            "such", "example", "based", "using", "without", "about", "within",
        }
        topic_signals[label] = words - stopwords

    # Only flag overlaps on known high-signal routing keywords — words that are
    # genuinely topic-discriminating. Generic overlap across a large topic set is
    # expected and not actionable.
    routing_signals = {
        "billing", "invoice", "payment", "refund", "charge", "subscription",
        "password", "login", "credential", "permission", "reset", "access",
        "error", "broken", "outage", "incident", "api", "dashboard",
        "escalate", "transfer", "handoff", "urgent", "emergency",
        "order", "shipment", "delivery", "tracking", "return", "refund",
        "appointment", "schedule", "booking", "reservation",
        "voice", "call", "pstn", "telephone",
    }

    results = []
    labels = list(topic_signals.keys())
    overlaps_found = []
    for i in range(len(labels)):
        for j in range(i + 1, len(labels)):
            a, b = labels[i], labels[j]
            overlap = topic_signals[a] & topic_signals[b] & routing_signals
            if overlap:
                overlaps_found.append((a, b, overlap))

    if overlaps_found:
        for a, b, words in overlaps_found:
            results.append(CheckResult(
                rule_id=f"DQ-01:{a}:{b}",
                passed=False,
                finding=Finding(
                    rule_id="DQ-01",
                    severity="medium",
                    title=f"Routing overlap: '{a}' and '{b}' share discriminating trigger words",
                    location=f"GenAiPlannerBundle > {a} > description AND {b} > description",
                    observed=f"Shared routing signal words: {', '.join(sorted(words))}",
                    expected="Each topic owns exclusive rights to its discriminating trigger vocabulary",
                    impact="User messages containing these words are ambiguous between the two topics. "
                           "topic_assertion failures on edge cases.",
                    fix=(
                        f"Step 1 — Remove shared words from one topic description: "
                        f"Setup → Agents → [your agent] → Open in Agent Builder → Topics tab → "
                        f"click '{a}' → Description field → remove '{', '.join(sorted(words))}' → Save. "
                        f"Step 2 — Add tiebreaker to planner instructions: "
                        f"Agent Builder → Agent Instructions field (top-level) → append: "
                        f"'If a message contains both {list(words)[0]} signals and other signals, "
                        f"prefer [winning topic] over [losing topic].' → Save. "
                        f"Salesforce docs: https://help.salesforce.com/s/articleView?id=sf.copilot_topics_config.htm"
                    ),
                )
            ))
    else:
        results.append(CheckResult(rule_id="DQ-01:global", passed=True))
    return results


def check_dq04_sensitive_operations(root: ET.Element) -> list[CheckResult]:
    """DQ-04: Sensitive operations must be gated behind human escalation."""
    sensitive_signals = ["password", "reset", "refund", "permission", "role change",
                         "delete", "credential", "credit card", "payment method"]
    gate_signals = ["escalate", "human agent", "cannot", "must not", "do not", "never"]
    results = []
    for topic in get_local_topics(root):
        topic_label = get_text(topic, "masterLabel", "unknown topic")
        scope = get_text(topic, "scope", "").lower()
        instructions_text = " ".join(
            get_text(i, "description", "").lower()
            for i in topic.findall(tag("genAiPluginInstructions"))
        )
        combined = scope + " " + instructions_text
        mentions_sensitive = any(s in combined for s in sensitive_signals)
        has_gate = any(s in combined for s in gate_signals)
        rule_id = f"DQ-04:{topic_label}"
        if mentions_sensitive and not has_gate:
            results.append(CheckResult(
                rule_id=rule_id,
                passed=False,
                finding=Finding(
                    rule_id="DQ-04",
                    severity="high",
                    title=f"{topic_label}: sensitive operation mentioned without escalation gate",
                    location=f"GenAiPlannerBundle > {topic_label} > scope / genAiPluginInstructions",
                    observed="Sensitive operation keyword found, no escalation or prohibition instruction",
                    expected="Explicit 'do not perform directly — escalate to human agent' instruction",
                    impact="Agent may attempt to perform a sensitive operation directly rather than escalating. "
                           "Security and compliance risk.",
                    fix=(
                        f"Step 1 — Add escalation gate to scope: "
                        f"Setup → Agents → [your agent] → Open in Agent Builder → "
                        f"Topics tab → click '{topic_label}' → Scope field → "
                        f"append: 'For any [sensitive operation], you must escalate to a human agent. "
                        f"Never perform [sensitive operation] directly.' → Save. "
                        f"Step 2 — Ensure canEscalate is enabled (requires metadata if not in UI): "
                        f"check that <canEscalate>true</canEscalate> is set on this topic in the bundle XML. "
                        f"See PM-08 fix instructions for the canEscalate metadata procedure. "
                        f"Salesforce docs: https://help.salesforce.com/s/articleView?id=sf.copilot_topics_escalate.htm"
                    ),
                )
            ))
        else:
            results.append(CheckResult(rule_id=rule_id, passed=True))
    return results


def check_dq05_topic_before_action(root: ET.Element) -> list[CheckResult]:
    """DQ-05: Planner must commit to topic before calling any action."""
    planner_desc = get_text(root, "description", "").lower()
    commit_signals = ["commit to", "topic is selected", "before calling", "select.*topic.*before",
                      "topic.*first", "first.*topic"]
    has_commit = any(
        re.search(s, planner_desc) for s in commit_signals
    )
    rule_id = "DQ-05:planner"
    if not has_commit:
        return [CheckResult(
            rule_id=rule_id,
            passed=False,
            finding=Finding(
                rule_id="DQ-05",
                severity="high",
                title="Planner instructions do not explicitly sequence topic selection before actions",
                location="GenAiPlannerBundle > description (planner instructions)",
                observed="No instruction requiring topic commitment before action calls",
                expected="Explicit instruction: 'Commit to a topic before calling any action'",
                impact="Planner may call a global action (e.g. AnswerQuestionsWithKnowledge) before topic routing, "
                       "bypassing all topic-level guardrails and scope restrictions.",
                fix=(
                    "UI path: Setup → Agents → [your agent] → Open in Agent Builder → "
                    "Agent Instructions field (top-level, above the Topics tab) → "
                    "prepend the following to the existing instructions: "
                    "'Your first responsibility on every turn is to select a topic before calling any action. "
                    "Do not call AnswerQuestionsWithKnowledge or any other action before a topic has been selected.' "
                    "→ Save. "
                    "Salesforce docs: https://help.salesforce.com/s/articleView?id=sf.copilot_planner_instructions.htm"
                ),
            )
        )]
    return [CheckResult(rule_id=rule_id, passed=True)]


def check_dq10_scope_exclusions(root: ET.Element) -> list[CheckResult]:
    """DQ-10: Topic scope must explicitly exclude adjacent topic responsibilities."""
    topics = get_local_topics(root)
    if len(topics) < 2:
        return [CheckResult(rule_id="DQ-10:global", passed=True)]
    exclusion_signals = ["not responsible", "do not handle", "you are not", "belongs to",
                         "refer to", "outside your scope", "not your responsibility"]
    results = []
    for topic in topics:
        topic_label = get_text(topic, "masterLabel", "unknown topic")
        scope = get_text(topic, "scope", "").lower()
        has_exclusion = any(s in scope for s in exclusion_signals)
        rule_id = f"DQ-10:{topic_label}"
        if not has_exclusion:
            results.append(CheckResult(
                rule_id=rule_id,
                passed=False,
                finding=Finding(
                    rule_id="DQ-10",
                    severity="low",
                    title=f"{topic_label}: scope does not exclude adjacent topic responsibilities",
                    location=f"GenAiPlannerBundle > {topic_label} > scope",
                    observed="No explicit exclusion of adjacent topic areas",
                    expected="'You are not responsible for [X] or [Y]' in scope statement",
                    impact="Ambiguous messages may be accepted by the wrong topic because no topic has rejected them. "
                           "Increases routing failure rate on edge cases.",
                    fix=(
                        f"UI path: Setup → Agents → [your agent] → Open in Agent Builder → "
                        f"Topics tab → click '{topic_label}' → Scope field → "
                        f"append to end of existing scope text: "
                        f"'You are not responsible for [list the other topics in this agent, e.g. billing disputes, "
                        f"password resets, technical errors]. Direct those requests to the appropriate topic.' "
                        f"→ Save. "
                        f"Salesforce docs: https://help.salesforce.com/s/articleView?id=sf.copilot_topic_scope.htm"
                    ),
                    candidate=False,
                )
            ))
        else:
            results.append(CheckResult(rule_id=rule_id, passed=True))
    return results


# ---------------------------------------------------------------------------
# Rules — Voice upstream/downstream (PM-11 through PM-15)
# ---------------------------------------------------------------------------

def is_voice_agent(root: ET.Element) -> bool:
    planner_type = get_text(root, "plannerType", "")
    surface_types = [get_text(s, "surfaceType", "") for s in get_planner_surfaces(root)]
    return "Atlas__VoiceAgent" in planner_type or "Telephony" in surface_types


def check_pm11_sel_rule_blocks_pstn(root: ET.Element) -> list[CheckResult]:
    """PM-11: SEL rule must not block unauthenticated inbound voice calls."""
    if not is_voice_agent(root):
        return [CheckResult(rule_id="PM-11:not_voice", passed=True)]

    rule_exprs = root.findall(tag("ruleExpressions"))
    for expr in rule_exprs:
        expr_type = get_text(expr, "expressionType", "")
        conditions = expr.findall(tag("conditions"))
        for cond in conditions:
            operand = get_text(cond, "leftOperand", "").lower()
            value = get_text(cond, "rightOperandValue", "").lower()
            if expr_type == "sel" and operand == "isverified" and value == "true":
                expr_name = get_text(expr, "expressionLabel", "Verified_User")
                return [CheckResult(
                    rule_id="PM-11:sel_verified",
                    passed=False,
                    finding=Finding(
                        rule_id="PM-11",
                        severity="critical",
                        title=f"Voice agent: SEL rule '{expr_name}' blocks all unauthenticated PSTN calls",
                        location="GenAiPlannerBundle > ruleExpressions > conditions > isVerified = true",
                        observed="isVerified = true gate present — inbound PSTN calls arrive with isVerified=null/false",
                        expected="Either no SEL rule, or an inbound Omni-Channel flow that sets isVerified=true before routing to agent",
                        impact="Every inbound PSTN call is silently rejected before the agent starts. "
                               "Callers hear nothing or fall through to a generic IVR. "
                               "This is not visible in Agent Builder — only in bundle XML.",
                        fix=(
                            "This field is NOT visible in Agent Builder UI — requires metadata fix. "
                            "Option 1 (remove gate — open to all callers): "
                            "Step 1: sf project retrieve start --metadata 'GenAiPlannerBundle:[AgentName]' --target-org [alias]. "
                            "Step 2: Open .genAiPlannerBundle, delete the entire <ruleExpressions>...</ruleExpressions> block. "
                            "Step 3: Setup → Agents → [your agent] → Deactivate. "
                            "Step 4: sf project deploy start --metadata 'GenAiPlannerBundle:[AgentName]' --target-org [alias]. "
                            "Step 5: Setup → Agents → [your agent] → Activate. Test PSTN call. "
                            "Option 2 (keep verification — fix the flow): "
                            "Setup → Process Automation → Flows → open your inbound Omni-Channel flow → "
                            "add an Assignment element before the Route Work step that sets {!isVerified} = true "
                            "after successful caller authentication. "
                            "Salesforce docs: https://help.salesforce.com/s/articleView?id=sf.voice_agentforce_eligibility.htm"
                        ),
                    )
                )]
    return [CheckResult(rule_id="PM-11:sel_verified", passed=True)]


def check_pm12_voice_outbound_route(root: ET.Element) -> list[CheckResult]:
    """PM-12: Voice surface must have outboundRouteConfigs defined."""
    if not is_voice_agent(root):
        return [CheckResult(rule_id="PM-12:not_voice", passed=True)]

    for surface in get_planner_surfaces(root):
        if get_text(surface, "surfaceType", "") == "Telephony":
            routes = surface.findall(tag("outboundRouteConfigs"))
            if not routes:
                return [CheckResult(
                    rule_id="PM-12:no_outbound_route",
                    passed=False,
                    finding=Finding(
                        rule_id="PM-12",
                        severity="critical",
                        title="Voice surface has no outbound route configured — escalation will fail",
                        location="GenAiPlannerBundle > plannerSurfaces > surfaceType: Telephony > outboundRouteConfigs",
                        observed="No outboundRouteConfigs block found under Telephony surface",
                        expected="At least one outboundRouteConfigs with outboundRouteName and outboundRouteType",
                        impact="Agent can answer inbound calls but cannot transfer to a human agent. "
                               "All escalation attempts fail silently — caller is not transferred.",
                        fix=(
                            "Step 1 — Add escalation route in Agent Builder: "
                            "Setup → Agents → [your agent] → Open in Agent Builder → "
                            "Surfaces tab → Telephony → Escalation Routes section → '+ Add Route' → "
                            "set Route Name, Route Type (OmniChannelFlow or Queue), and target → Save. "
                            "Step 2 — If the route is not visible in UI, add via metadata: "
                            "retrieve bundle XML → add <outboundRouteConfigs> block with "
                            "<outboundRouteName>, <outboundRouteType>, and optionally <escalationMessage> → "
                            "deactivate agent → deploy → reactivate. "
                            "Salesforce docs: https://help.salesforce.com/s/articleView?id=sf.voice_agentforce_escalation.htm"
                        ),
                    )
                )]
    return [CheckResult(rule_id="PM-12:voice_route", passed=True)]


def check_pm13_escalation_message(root: ET.Element) -> list[CheckResult]:
    """PM-13: Voice escalation must include an escalation message for the caller."""
    if not is_voice_agent(root):
        return [CheckResult(rule_id="PM-13:not_voice", passed=True)]

    results = []
    for surface in get_planner_surfaces(root):
        if get_text(surface, "surfaceType", "") != "Telephony":
            continue
        for route in surface.findall(tag("outboundRouteConfigs")):
            route_name = get_text(route, "outboundRouteName", "unnamed route")
            message = get_text(route, "escalationMessage", "")
            rule_id = f"PM-13:{route_name}"
            if not message:
                results.append(CheckResult(
                    rule_id=rule_id,
                    passed=False,
                    finding=Finding(
                        rule_id="PM-13",
                        severity="medium",
                        title=f"Voice route '{route_name}': no escalation message — caller hears silence during transfer",
                        location=f"GenAiPlannerBundle > plannerSurfaces > Telephony > outboundRouteConfigs > {route_name} > escalationMessage",
                        observed="escalationMessage is empty or missing",
                        expected="A short hold message e.g. 'Connecting you to a live agent, please hold.'",
                        impact="Caller hears silence for 5-15 seconds during transfer. "
                               "Most callers assume the call dropped and hang up.",
                        fix=(
                            f"UI path: Setup → Agents → [your agent] → Open in Agent Builder → "
                            f"Surfaces tab → Telephony → Escalation Routes → click '{route_name}' → "
                            f"Escalation Message field → enter: 'Connecting you to a live agent. Please hold.' "
                            f"→ Save. "
                            f"CLI path: retrieve bundle XML → find <outboundRouteConfigs> for '{route_name}' → "
                            f"add <escalationMessage>Connecting you to a live agent. Please hold.</escalationMessage> → "
                            f"deactivate → deploy → reactivate. "
                            f"Salesforce docs: https://help.salesforce.com/s/articleView?id=sf.voice_agentforce_escalation.htm"
                        ),
                    )
                ))
            else:
                results.append(CheckResult(rule_id=rule_id, passed=True))
    if not results:
        results.append(CheckResult(rule_id="PM-13:no_routes", passed=True))
    return results


def check_pm14_omnichannel_flow_exists(root: ET.Element, org: str) -> list[CheckResult]:
    """PM-14: Omni-Channel flow referenced in outboundRouteConfigs must exist in org."""
    if not is_voice_agent(root):
        return [CheckResult(rule_id="PM-14:not_voice", passed=True)]

    route_names = []
    for surface in get_planner_surfaces(root):
        if get_text(surface, "surfaceType", "") != "Telephony":
            continue
        for route in surface.findall(tag("outboundRouteConfigs")):
            rtype = get_text(route, "outboundRouteType", "")
            rname = get_text(route, "outboundRouteName", "")
            if rtype == "OmniChannelFlow" and rname:
                route_names.append(rname)

    if not route_names:
        return [CheckResult(rule_id="PM-14:no_omni_routes", passed=True)]

    # Query org for existing Omni-Channel flows
    code, out, err = run_sf([
        "org", "list", "metadata",
        "--metadata-type", "Flow",
        "--target-org", org,
        "--json"
    ])
    existing_flows: set[str] = set()
    if code == 0:
        try:
            data = json.loads(out)
            for rec in data.get("result", []):
                existing_flows.add(rec.get("fullName", ""))
        except (json.JSONDecodeError, KeyError):
            pass

    results = []
    for rname in route_names:
        rule_id = f"PM-14:{rname}"
        if existing_flows and rname not in existing_flows:
            results.append(CheckResult(
                rule_id=rule_id,
                passed=False,
                finding=Finding(
                    rule_id="PM-14",
                    severity="critical",
                    title=f"Voice escalation route '{rname}' not found as an active flow in org",
                    location=f"GenAiPlannerBundle > plannerSurfaces > Telephony > outboundRouteConfigs > outboundRouteName",
                    observed=f"outboundRouteName = '{rname}'",
                    expected=f"An active Omni-Channel Flow named '{rname}' in Setup → Flows",
                    impact="Voice transfer fails at runtime — agent attempts to route the call, "
                           "flow does not exist, call is dropped with no human handoff.",
                    fix=f"Verify the flow '{rname}' exists in Setup → Process Automation → Flows "
                        f"and is Active and of type Omni-Channel Flow. "
                        f"If it was renamed or deleted, update outboundRouteName in the bundle XML to match.",
                )
            ))
        else:
            results.append(CheckResult(rule_id=rule_id, passed=True))
    return results


def check_pm15_contact_center_exists(root: ET.Element, org: str) -> list[CheckResult]:
    """PM-15: A Contact Center must exist in the org for voice agents."""
    if not is_voice_agent(root):
        return [CheckResult(rule_id="PM-15:not_voice", passed=True)]

    code, out, err = run_sf([
        "org", "list", "metadata",
        "--metadata-type", "ContactCenter",
        "--target-org", org,
        "--json"
    ])
    rule_id = "PM-15:contact_center"
    if code != 0:
        # Can't verify — flag as advisory
        return [CheckResult(
            rule_id=rule_id,
            passed=False,
            finding=Finding(
                rule_id="PM-15",
                severity="medium",
                title="Voice agent: could not verify Contact Center exists in org",
                location="Setup → Feature Settings → Service → Contact Center → Contact Centers",
                observed="Metadata query for ContactCenter failed or returned no results",
                expected="At least one Contact Center with a PSTN number assigned",
                impact="Without a Contact Center, inbound PSTN calls have no entry point into Salesforce. "
                       "The agent will never receive a call regardless of other configuration.",
                fix=(
                    "Verify: Setup → Feature Settings → Service → Contact Center → Contact Centers → "
                    "confirm at least one Contact Center record exists. "
                    "Click the Contact Center → Phone Numbers tab → verify your PSTN number is listed and active. "
                    "Also confirm: Setup → Agents → [your agent] → Open in Agent Builder → "
                    "Surfaces tab → Telephony → Contact Center field is set to this Contact Center. "
                    "If the number is missing: Setup → Contact Centers → [your center] → Phone Numbers → Add Phone Number. "
                    "Salesforce docs: https://help.salesforce.com/s/articleView?id=sf.voice_contact_center_setup.htm"
                ),
                candidate=False,
            )
        )]

    try:
        data = json.loads(out)
        records = data.get("result", [])
    except (json.JSONDecodeError, KeyError):
        records = []

    if not records:
        return [CheckResult(
            rule_id=rule_id,
            passed=False,
            finding=Finding(
                rule_id="PM-15",
                severity="critical",
                title="Voice agent: no Contact Center found in org — PSTN calls cannot be received",
                location="Setup → Feature Settings → Service → Contact Center → Contact Centers",
                observed="No ContactCenter metadata records found in org",
                expected="At least one Contact Center with PSTN number assigned",
                impact="Inbound PSTN calls have no entry point into Salesforce. "
                       "The agent will never receive a call.",
                fix=(
                    "Step 1 — Create a Contact Center: Setup → Feature Settings → Service → Contact Center → "
                    "Contact Centers → New → fill in the required fields → Save. "
                    "Step 2 — Assign PSTN number: click the new Contact Center → Phone Numbers tab → "
                    "Add Phone Number → select or provision your PSTN number. "
                    "Step 3 — Link to this agent: Setup → Agents → [your agent] → Open in Agent Builder → "
                    "Surfaces tab → Telephony → Contact Center field → select your Contact Center → Save. "
                    "Salesforce docs: https://help.salesforce.com/s/articleView?id=sf.voice_contact_center_setup.htm"
                ),
            )
        )]
    return [CheckResult(rule_id=rule_id, passed=True)]


# ---------------------------------------------------------------------------
# Rules — Platform Mechanics (PM-16 through PM-25)
# ---------------------------------------------------------------------------

def check_pm16_instruction_length(root: ET.Element) -> list[CheckResult]:
    """PM-16: Topic instruction text must not exceed 4000 character platform limit."""
    LIMIT = 4000
    results = []
    for topic in get_local_topics(root):
        topic_label = get_text(topic, "masterLabel", "unknown topic")
        for instr in topic.findall(tag("genAiPluginInstructions")):
            dev_name = get_text(instr, "developerName", "unknown")
            text = get_text(instr, "description", "")
            rule_id = f"PM-16:{topic_label}:{dev_name}"
            if len(text) > LIMIT:
                results.append(CheckResult(
                    rule_id=rule_id,
                    passed=False,
                    finding=Finding(
                        rule_id="PM-16",
                        severity="high",
                        title=f"{topic_label}: instruction '{dev_name}' exceeds 4000 char limit ({len(text)} chars) — will be silently truncated",
                        location=f"GenAiPlannerBundle > {topic_label} > genAiPluginInstructions > {dev_name} > description",
                        observed=f"{len(text)} characters",
                        expected="Under 4000 characters",
                        impact="Salesforce silently truncates the instruction at deploy time. "
                               "The agent receives an incomplete instruction with no error raised. "
                               "Behavior becomes inconsistent — some steps execute, others are cut off.",
                        fix=(
                            f"UI path: Setup → Agents → [your agent] → Open in Agent Builder → "
                            f"Topics tab → '{topic_label}' → Instructions section → "
                            f"click the instruction labeled '{dev_name}' → edit the text to under 4000 characters. "
                            f"If the content is too long to fit in one instruction, click '+ Add Instruction' "
                            f"to split it into multiple sequential steps (e.g. Step 2a, Step 2b). "
                            f"Current length: {len(text)} chars — remove approximately {len(text) - 3800} characters. "
                            f"Salesforce docs: https://help.salesforce.com/s/articleView?id=sf.copilot_topic_limits.htm"
                        ),
                    )
                ))
            else:
                results.append(CheckResult(rule_id=rule_id, passed=True))
    if not results:
        results.append(CheckResult(rule_id="PM-16:no_instructions", passed=True))
    return results


def check_pm17_duplicate_topic_names(root: ET.Element) -> list[CheckResult]:
    """PM-17: Duplicate localDeveloperName across topics causes unpredictable routing."""
    topics = get_local_topics(root)
    seen: dict[str, str] = {}
    results = []
    for topic in topics:
        label = get_text(topic, "masterLabel", "unknown")
        dev_name = get_text(topic, "localDeveloperName", "").lower()
        if not dev_name:
            continue
        rule_id = f"PM-17:{dev_name}"
        if dev_name in seen:
            results.append(CheckResult(
                rule_id=rule_id,
                passed=False,
                finding=Finding(
                    rule_id="PM-17",
                    severity="critical",
                    title=f"Duplicate topic developer name '{dev_name}' — topic '{label}' shadows '{seen[dev_name]}'",
                    location=f"GenAiPlannerBundle > localTopics > localDeveloperName",
                    observed=f"'{dev_name}' used by both '{seen[dev_name]}' and '{label}'",
                    expected="Each topic has a unique localDeveloperName",
                    impact="The second topic with this name is unreachable. All routing to it silently "
                           "goes to the first topic with the same name.",
                    fix=(
                        f"This requires a metadata fix — developer names are not editable in Agent Builder UI. "
                        f"Step 1: sf project retrieve start --metadata 'GenAiPlannerBundle:[AgentName]' --target-org [alias]. "
                        f"Step 2: Open the .genAiPlannerBundle file → find the second <localTopics> block "
                        f"with <localDeveloperName>{dev_name}</localDeveloperName> → change it to a unique value "
                        f"(e.g. append '_v2' or use a more descriptive name). "
                        f"Step 3: Setup → Agents → [your agent] → Deactivate. "
                        f"Step 4: sf project deploy start --metadata 'GenAiPlannerBundle:[AgentName]' --target-org [alias]. "
                        f"Step 5: Reactivate agent. "
                        f"Salesforce docs: https://help.salesforce.com/s/articleView?id=sf.copilot_topics_add.htm"
                    ),
                )
            ))
        else:
            seen[dev_name] = label
            results.append(CheckResult(rule_id=rule_id, passed=True))
    return results or [CheckResult(rule_id="PM-17:global", passed=True)]


def check_pm18_duplicate_action_names(root: ET.Element) -> list[CheckResult]:
    """PM-18: Action localDeveloperName must be unique within a topic."""
    results = []
    for topic in get_local_topics(root):
        topic_label = get_text(topic, "masterLabel", "unknown topic")
        seen: dict[str, str] = {}
        for action in get_topic_actions(topic):
            dev_name = get_text(action, "localDeveloperName", "").lower()
            label = get_text(action, "masterLabel", dev_name)
            if not dev_name:
                continue
            rule_id = f"PM-18:{topic_label}:{dev_name}"
            if dev_name in seen:
                results.append(CheckResult(
                    rule_id=rule_id,
                    passed=False,
                    finding=Finding(
                        rule_id="PM-18",
                        severity="high",
                        title=f"{topic_label}: duplicate action name '{dev_name}' — second action is unreachable",
                        location=f"GenAiPlannerBundle > {topic_label} > localActions > localDeveloperName",
                        observed=f"'{dev_name}' defined twice in topic '{topic_label}'",
                        expected="Each action has a unique localDeveloperName within its topic",
                        impact="The second action with the duplicate name is silently ignored. "
                               "Planner always calls the first one.",
                        fix=(
                            f"This requires a metadata fix — action developer names are not editable in Agent Builder UI. "
                            f"Step 1: sf project retrieve start --metadata 'GenAiPlannerBundle:[AgentName]' --target-org [alias]. "
                            f"Step 2: Open the .genAiPlannerBundle file → find topic '{topic_label}' → "
                            f"locate the second <localActions> block with <localDeveloperName>{dev_name}</localDeveloperName> → "
                            f"change it to a unique value (e.g. append '_v2' or use a more descriptive name). "
                            f"Step 3: Setup → Agents → [your agent] → Deactivate. "
                            f"Step 4: sf project deploy start --metadata 'GenAiPlannerBundle:[AgentName]' --target-org [alias]. "
                            f"Step 5: Reactivate agent. "
                            f"Salesforce docs: https://help.salesforce.com/s/articleView?id=sf.copilot_actions_configure.htm"
                        ),
                    )
                ))
            else:
                seen[dev_name] = label
                results.append(CheckResult(rule_id=rule_id, passed=True))
    return results or [CheckResult(rule_id="PM-18:global", passed=True)]


def check_pm19_voice_progress_indicator(root: ET.Element) -> list[CheckResult]:
    """PM-19: Voice agent actions must have progressIndicatorMessage set."""
    if not is_voice_agent(root):
        return [CheckResult(rule_id="PM-19:not_voice", passed=True)]
    results = []
    for topic in get_local_topics(root):
        topic_label = get_text(topic, "masterLabel", "unknown topic")
        for action in get_topic_actions(topic):
            action_label = get_text(action, "masterLabel", "unknown action")
            dev_name = get_text(action, "localDeveloperName", action_label)
            include = get_text(action, "isIncludeInProgressIndicator", "false")
            message = get_text(action, "progressIndicatorMessage", "")
            rule_id = f"PM-19:{topic_label}:{dev_name}"
            if include.lower() != "true" or not message:
                results.append(CheckResult(
                    rule_id=rule_id,
                    passed=False,
                    finding=Finding(
                        rule_id="PM-19",
                        severity="high",
                        title=f"Voice agent — {topic_label}: action '{action_label}' has no progress message — caller hears silence",
                        location=f"GenAiPlannerBundle > {topic_label} > {dev_name} > progressIndicatorMessage",
                        observed=f"isIncludeInProgressIndicator={include}, progressIndicatorMessage='{message}'",
                        expected="isIncludeInProgressIndicator=true with a spoken hold message",
                        impact="Caller hears silence while the action executes. "
                               "Actions over ~2 seconds cause callers to assume call dropped and hang up.",
                        fix=(
                            f"UI path: Setup → Agents → [your agent] → Open in Agent Builder → "
                            f"Topics tab → click '{topic_label}' → Actions section → click '{action_label}' → "
                            f"enable 'Include in Progress Indicator' toggle → "
                            f"Progress Indicator Message field → enter a short hold message such as: "
                            f"'One moment while I look that up.' → Save. "
                            f"CLI path: retrieve bundle XML → find action '{dev_name}' in topic '{topic_label}' → "
                            f"set <isIncludeInProgressIndicator>true</isIncludeInProgressIndicator> and "
                            f"<progressIndicatorMessage>One moment while I look that up.</progressIndicatorMessage> → "
                            f"deactivate → deploy → reactivate. "
                            f"Salesforce docs: https://help.salesforce.com/s/articleView?id=sf.copilot_actions_configure.htm"
                        ),
                    )
                ))
            else:
                results.append(CheckResult(rule_id=rule_id, passed=True))
    return results or [CheckResult(rule_id="PM-19:no_actions", passed=True)]


def check_pm20_voice_adaptive_response(root: ET.Element) -> list[CheckResult]:
    """PM-20: adaptiveResponseAllowed must be true for voice/telephony surface."""
    if not is_voice_agent(root):
        return [CheckResult(rule_id="PM-20:not_voice", passed=True)]
    for surface in get_planner_surfaces(root):
        if get_text(surface, "surfaceType", "") == "Telephony":
            adaptive = get_text(surface, "adaptiveResponseAllowed", "false")
            if adaptive.lower() != "true":
                return [CheckResult(
                    rule_id="PM-20:telephony_adaptive",
                    passed=False,
                    finding=Finding(
                        rule_id="PM-20",
                        severity="high",
                        title="Voice surface: adaptiveResponseAllowed is false — caller hears raw structured text",
                        location="GenAiPlannerBundle > plannerSurfaces > Telephony > adaptiveResponseAllowed",
                        observed="false",
                        expected="true",
                        impact="Agent responses are not adapted for text-to-speech. "
                               "Caller hears raw structured output including field labels, "
                               "brackets, and formatting characters.",
                        fix=(
                            "This field may not be exposed in Agent Builder UI — check first, then use CLI if needed. "
                            "UI check: Setup → Agents → [your agent] → Open in Agent Builder → "
                            "Surfaces tab → Telephony → look for 'Adaptive Response' or 'Speech Optimization' toggle → "
                            "enable it if present → Save. "
                            "CLI path: sf project retrieve start --metadata 'GenAiPlannerBundle:[AgentName]' --target-org [alias] → "
                            "open .genAiPlannerBundle → find <plannerSurfaces> with <surfaceType>Telephony</surfaceType> → "
                            "add or change <adaptiveResponseAllowed>true</adaptiveResponseAllowed> → "
                            "deactivate agent → deploy → reactivate. "
                            "Salesforce docs: https://help.salesforce.com/s/articleView?id=sf.voice_agentforce_setup.htm"
                        ),
                    )
                )]
    return [CheckResult(rule_id="PM-20:telephony_adaptive", passed=True)]


def check_pm21_call_recording_set(root: ET.Element) -> list[CheckResult]:
    """PM-21: callRecordingAllowed must be explicitly set on voice surface."""
    if not is_voice_agent(root):
        return [CheckResult(rule_id="PM-21:not_voice", passed=True)]
    for surface in get_planner_surfaces(root):
        if get_text(surface, "surfaceType", "") == "Telephony":
            val = get_text(surface, "callRecordingAllowed", "")
            rule_id = "PM-21:call_recording"
            if val == "":
                return [CheckResult(
                    rule_id=rule_id,
                    passed=False,
                    finding=Finding(
                        rule_id="PM-21",
                        severity="medium",
                        title="Voice surface: callRecordingAllowed not explicitly set — compliance risk",
                        location="GenAiPlannerBundle > plannerSurfaces > Telephony > callRecordingAllowed",
                        observed="callRecordingAllowed field absent — platform default applies",
                        expected="Explicit true or false based on org compliance requirements",
                        impact="If org requires call recording consent disclosure and this defaults to true, "
                               "recordings may be made without required disclosure. "
                               "If policy prohibits recording and this defaults to true, "
                               "data retention policy may be violated.",
                        fix=(
                            "Step 1 — Determine your org's call recording policy with your compliance team. "
                            "Step 2 — Set the value explicitly: "
                            "sf project retrieve start --metadata 'GenAiPlannerBundle:[AgentName]' --target-org [alias] → "
                            "open .genAiPlannerBundle → find <plannerSurfaces> with <surfaceType>Telephony</surfaceType> → "
                            "add <callRecordingAllowed>true</callRecordingAllowed> (if recording is permitted and consented) "
                            "or <callRecordingAllowed>false</callRecordingAllowed> (if recording is prohibited) → "
                            "deactivate agent → deploy → reactivate. "
                            "Salesforce docs: https://help.salesforce.com/s/articleView?id=sf.voice_call_recording.htm"
                        ),
                    )
                )]
            return [CheckResult(rule_id=rule_id, passed=True)]
    return [CheckResult(rule_id="PM-21:not_voice", passed=True)]


def check_pm22_has_topics(root: ET.Element) -> list[CheckResult]:
    """PM-22: Agent must have at least one topic linked via localTopicLinks."""
    links = root.findall(tag("localTopicLinks"))
    if not links:
        return [CheckResult(
            rule_id="PM-22:no_topics",
            passed=False,
            finding=Finding(
                rule_id="PM-22",
                severity="critical",
                title="Agent has no topics — all routing bypassed, only global actions available",
                location="GenAiPlannerBundle > localTopicLinks",
                observed="No localTopicLinks entries found",
                expected="At least one localTopicLinks entry pointing to a configured topic",
                impact="Every message routes directly to planner-level global actions "
                       "(typically AnswerQuestionsWithKnowledge). All topic guardrails, "
                       "action bindings, and scope restrictions are bypassed.",
                fix=(
                    "UI path: Setup → Agents → [your agent] → Open in Agent Builder → "
                    "Topics tab → '+ New Topic' button → fill in Name, Scope, and Instructions → "
                    "add at least one Action → Save → Activate agent. "
                    "At minimum, add a General Inquiry topic as a fallback for unrecognized requests. "
                    "Salesforce docs: https://help.salesforce.com/s/articleView?id=sf.copilot_topics_add.htm"
                ),
            )
        )]
    return [CheckResult(rule_id="PM-22:has_topics", passed=True)]


def check_pm23_dangling_topic_links(root: ET.Element) -> list[CheckResult]:
    """PM-23: Topic linked in localTopicLinks must have a matching localTopics entry."""
    linked_names = {
        get_text(link, "genAiPluginName", "")
        for link in root.findall(tag("localTopicLinks"))
    }
    defined_names = {
        get_text(topic, "fullName", "")
        for topic in get_local_topics(root)
    }
    results = []
    for name in linked_names:
        if not name:
            continue
        rule_id = f"PM-23:{name}"
        if name not in defined_names:
            results.append(CheckResult(
                rule_id=rule_id,
                passed=False,
                finding=Finding(
                    rule_id="PM-23",
                    severity="critical",
                    title=f"Dangling topic link: '{name}' referenced in localTopicLinks but not defined in localTopics",
                    location="GenAiPlannerBundle > localTopicLinks > genAiPluginName",
                    observed=f"genAiPluginName = '{name}' has no matching localTopics > fullName",
                    expected="Every localTopicLinks entry has a corresponding localTopics block",
                    impact="Planner attempts to route to a topic with no instructions. "
                           "Silent no-op or runtime error depending on platform version.",
                    fix=(
                        f"Option A — add the missing topic definition: "
                        f"Setup → Agents → [your agent] → Open in Agent Builder → "
                        f"Topics tab → '+ New Topic' → set developer name to '{name}' → "
                        f"fill in Scope and Instructions → Save. "
                        f"Option B — remove the dangling link (requires metadata): "
                        f"sf project retrieve start --metadata 'GenAiPlannerBundle:[AgentName]' --target-org [alias] → "
                        f"open .genAiPlannerBundle → find and delete the <localTopicLinks> block referencing '{name}' → "
                        f"deactivate → deploy → reactivate. "
                        f"Salesforce docs: https://help.salesforce.com/s/articleView?id=sf.copilot_topics_add.htm"
                    ),
                )
            ))
        else:
            results.append(CheckResult(rule_id=rule_id, passed=True))
    return results or [CheckResult(rule_id="PM-23:global", passed=True)]


def check_pm24_dangling_action_links(root: ET.Element) -> list[CheckResult]:
    """PM-24: Action in localActionLinks must have a matching localActions entry."""
    results = []
    for topic in get_local_topics(root):
        topic_label = get_text(topic, "masterLabel", "unknown topic")
        linked = {
            get_text(link, "functionName", "")
            for link in topic.findall(tag("localActionLinks"))
        }
        defined = {
            get_text(action, "fullName", "")
            for action in get_topic_actions(topic)
        }
        for name in linked:
            if not name:
                continue
            rule_id = f"PM-24:{topic_label}:{name}"
            if name not in defined:
                results.append(CheckResult(
                    rule_id=rule_id,
                    passed=False,
                    finding=Finding(
                        rule_id="PM-24",
                        severity="critical",
                        title=f"{topic_label}: action link '{name}' has no matching localActions entry",
                        location=f"GenAiPlannerBundle > {topic_label} > localActionLinks > functionName",
                        observed=f"functionName = '{name}' has no matching localActions > fullName",
                        expected="Every localActionLinks entry has a corresponding localActions block",
                        impact="Planner attempts to invoke an action with no configuration. "
                               "Silent failure at runtime — action is not called.",
                        fix=(
                            f"Option A — add the missing action in Agent Builder: "
                            f"Setup → Agents → [your agent] → Open in Agent Builder → "
                            f"Topics tab → click '{topic_label}' → Actions section → '+ Add Action' → "
                            f"search for and select the action named '{name}' → Save. "
                            f"Option B — remove the dangling link (requires metadata): "
                            f"sf project retrieve start --metadata 'GenAiPlannerBundle:[AgentName]' --target-org [alias] → "
                            f"open .genAiPlannerBundle → find topic '{topic_label}' → "
                            f"delete the <localActionLinks> block with <functionName>{name}</functionName> → "
                            f"deactivate → deploy → reactivate. "
                            f"Salesforce docs: https://help.salesforce.com/s/articleView?id=sf.copilot_actions_configure.htm"
                        ),
                    )
                ))
            else:
                results.append(CheckResult(rule_id=rule_id, passed=True))
    return results or [CheckResult(rule_id="PM-24:global", passed=True)]


def check_pm25_planner_type_surface_mismatch(root: ET.Element) -> list[CheckResult]:
    """PM-25: plannerType must match the deployed surface."""
    planner_type = get_text(root, "plannerType", "")
    surface_types = {get_text(s, "surfaceType", "") for s in get_planner_surfaces(root)}
    rule_id = "PM-25:surface_match"

    voice_planner = "Atlas__VoiceAgent" in planner_type
    has_voice_surface = "Telephony" in surface_types
    has_chat_surface = bool(surface_types - {"Telephony"})

    if voice_planner and has_chat_surface and not has_voice_surface:
        return [CheckResult(
            rule_id=rule_id,
            passed=False,
            finding=Finding(
                rule_id="PM-25",
                severity="high",
                title="plannerType is VoiceAgent but no Telephony surface configured",
                location="GenAiPlannerBundle > plannerType vs plannerSurfaces > surfaceType",
                observed=f"plannerType={planner_type}, surfaces={surface_types}",
                expected="Telephony surface present for VoiceAgent plannerType",
                impact="Voice agent has no channel to receive calls. "
                       "Agent is deployed but unreachable via voice.",
                fix=(
                    "Option A — add missing Telephony surface: "
                    "Setup → Agents → [your agent] → Open in Agent Builder → "
                    "Surfaces tab → '+ Add Surface' → select Telephony → configure Contact Center and routes → Save. "
                    "Option B — correct the plannerType to chat (requires metadata): "
                    "sf project retrieve start --metadata 'GenAiPlannerBundle:[AgentName]' --target-org [alias] → "
                    "change <plannerType> from Atlas__VoiceAgent to AiCopilot__ReAct → "
                    "deactivate → deploy → reactivate. "
                    "Salesforce docs: https://help.salesforce.com/s/articleView?id=sf.voice_agentforce_setup.htm"
                ),
            )
        )]
    if not voice_planner and has_voice_surface:
        return [CheckResult(
            rule_id=rule_id,
            passed=False,
            finding=Finding(
                rule_id="PM-25",
                severity="high",
                title="Telephony surface present but plannerType is not VoiceAgent",
                location="GenAiPlannerBundle > plannerType vs plannerSurfaces > surfaceType",
                observed=f"plannerType={planner_type}, Telephony surface present",
                expected="plannerType=Atlas__VoiceAgent when Telephony surface is configured",
                impact="Agent on Telephony surface with wrong planner type produces "
                       "responses incompatible with voice channel format.",
                fix=(
                    "Option A — set correct plannerType (requires metadata): "
                    "sf project retrieve start --metadata 'GenAiPlannerBundle:[AgentName]' --target-org [alias] → "
                    "open .genAiPlannerBundle → change <plannerType> to Atlas__VoiceAgent → "
                    "deactivate → deploy → reactivate. "
                    "Option B — remove the Telephony surface if this is a chat-only agent: "
                    "Setup → Agents → [your agent] → Open in Agent Builder → "
                    "Surfaces tab → click Telephony → Remove Surface → Save. "
                    "Salesforce docs: https://help.salesforce.com/s/articleView?id=sf.voice_agentforce_setup.htm"
                ),
            )
        )]
    return [CheckResult(rule_id=rule_id, passed=True)]


# ---------------------------------------------------------------------------
# Rules — Design Quality (DQ-06 through DQ-14, additional)
# ---------------------------------------------------------------------------

def check_dq06_fallback_one_question(root: ET.Element) -> list[CheckResult]:
    """DQ-06: Fallback topic must ask exactly one clarifying question."""
    fallback_signals = ["general", "fallback", "catch", "ambiguous", "inquiry", "faq"]
    plural_signals = ["ask questions", "ask the customer questions", "multiple questions",
                      "several questions", "questions to"]
    single_signals = ["one question", "single question", "exactly one", "one clarifying",
                      "one targeted"]
    results = []
    for topic in get_local_topics(root):
        label = get_text(topic, "masterLabel", "").lower()
        is_fallback = any(s in label for s in fallback_signals)
        if not is_fallback:
            continue
        topic_label = get_text(topic, "masterLabel", "unknown")
        instructions_text = " ".join(
            get_text(i, "description", "").lower()
            for i in topic.findall(tag("genAiPluginInstructions"))
        )
        scope = get_text(topic, "scope", "").lower()
        combined = instructions_text + " " + scope
        has_plural = any(s in combined for s in plural_signals)
        has_single = any(s in combined for s in single_signals)
        rule_id = f"DQ-06:{topic_label}"
        if has_plural and not has_single:
            results.append(CheckResult(
                rule_id=rule_id,
                passed=False,
                finding=Finding(
                    rule_id="DQ-06",
                    severity="medium",
                    title=f"{topic_label}: instructions allow multiple clarifying questions — should be exactly one",
                    location=f"GenAiPlannerBundle > {topic_label} > genAiPluginInstructions",
                    observed="Plural question phrasing found, no single-question constraint",
                    expected="'Ask exactly one clarifying question per turn'",
                    impact="Agent may ask 2-3 questions in a single turn. "
                           "Customers find this overwhelming — resolution rate drops significantly.",
                    fix=(
                        f"UI path: Setup → Agents → [your agent] → Open in Agent Builder → "
                        f"Topics tab → click '{topic_label}' → Instructions section → "
                        f"edit the instruction step that says 'ask questions' → change to: "
                        f"'Ask exactly one targeted clarifying question per turn. "
                        f"Wait for the customer to answer before asking another.' → Save. "
                        f"Salesforce docs: https://help.salesforce.com/s/articleView?id=sf.copilot_topic_instructions.htm"
                    ),
                )
            ))
        else:
            results.append(CheckResult(rule_id=rule_id, passed=True))
    return results or [CheckResult(rule_id="DQ-06:global", passed=True)]


def check_dq11_escalation_loop(root: ET.Element) -> list[CheckResult]:
    """DQ-11: Escalation topic must not create an escalation loop."""
    results = []
    for topic in get_local_topics(root):
        label = get_text(topic, "masterLabel", "unknown")
        can_escalate = get_text(topic, "canEscalate", "false").lower()
        if can_escalate != "true":
            continue
        scope = get_text(topic, "scope", "").lower()
        instructions_text = " ".join(
            get_text(i, "description", "").lower()
            for i in topic.findall(tag("genAiPluginInstructions"))
        )
        combined = scope + " " + instructions_text
        escalation_keywords = ["escalate", "transfer", "live agent", "human agent"]
        failure_fallbacks = ["log a case", "create a case", "phone number",
                             "call back", "email", "contact us", "if escalation fails",
                             "if transfer fails"]
        mentions_escalation = any(k in combined for k in escalation_keywords)
        has_terminal_fallback = any(f in combined for f in failure_fallbacks)
        rule_id = f"DQ-11:{label}"
        if mentions_escalation and not has_terminal_fallback:
            results.append(CheckResult(
                rule_id=rule_id,
                passed=False,
                finding=Finding(
                    rule_id="DQ-11",
                    severity="medium",
                    title=f"{label}: escalation topic has no terminal fallback if escalation fails",
                    location=f"GenAiPlannerBundle > {label} > genAiPluginInstructions",
                    observed="canEscalate=true with escalation instructions but no failure fallback",
                    expected="Explicit instruction: 'If escalation fails, [log a case / provide phone number]'",
                    impact="If the transfer fails (queue full, routing error, after-hours), "
                           "agent has no alternative path — customer is left with no resolution.",
                    fix=(
                        f"UI path: Setup → Agents → [your agent] → Open in Agent Builder → "
                        f"Topics tab → click '{label}' → Instructions section → "
                        f"click '+ Add Instruction' → add as the final step: "
                        f"'If escalation to a live agent is unavailable or fails, offer to log a support case "
                        f"for follow-up or provide a direct contact number. Do not attempt escalation again.' "
                        f"→ Save. "
                        f"Salesforce docs: https://help.salesforce.com/s/articleView?id=sf.copilot_topic_instructions.htm"
                    ),
                )
            ))
        else:
            results.append(CheckResult(rule_id=rule_id, passed=True))
    return results or [CheckResult(rule_id="DQ-11:global", passed=True)]


def check_dq12_topic_coverage(root: ET.Element) -> list[CheckResult]:
    """DQ-12: Agent must have topics covering all intent categories in its description."""
    description = get_text(root, "description", "").lower()
    topics = get_local_topics(root)
    topic_labels = [get_text(t, "masterLabel", "").lower() for t in topics]
    topic_text = " ".join(topic_labels)

    intent_signals = {
        "billing": ["billing", "payment", "invoice", "refund", "charge"],
        "technical support": ["technical", "error", "broken", "api", "outage"],
        "account management": ["account", "password", "login", "access", "permission"],
        "order management": ["order", "shipment", "delivery", "tracking", "return"],
        "appointment": ["appointment", "schedule", "booking", "reservation"],
    }
    gaps = []
    for intent, keywords in intent_signals.items():
        desc_mentions = any(k in description for k in keywords)
        topic_covers = any(k in topic_text for k in keywords)
        if desc_mentions and not topic_covers:
            gaps.append(intent)

    rule_id = "DQ-12:topic_coverage"
    if gaps:
        return [CheckResult(
            rule_id=rule_id,
            passed=False,
            finding=Finding(
                rule_id="DQ-12",
                severity="medium",
                title=f"Agent description mentions intent(s) with no matching topic: {', '.join(gaps)}",
                location="GenAiPlannerBundle > description vs localTopics > masterLabel",
                observed=f"Description mentions: {', '.join(gaps)} — no topic covers these",
                expected="A dedicated topic for each major intent category in the agent description",
                impact="Unmatched intents route to whichever topic is most similar or to global "
                       "actions — bypassing appropriate guardrails and action bindings.",
                fix=(
                    f"Option A — add missing topics: "
                    f"Setup → Agents → [your agent] → Open in Agent Builder → "
                    f"Topics tab → '+ New Topic' → create a topic for each of: {', '.join(gaps)} → "
                    f"add Scope, Instructions, and Actions as appropriate → Save → Activate. "
                    f"Option B — narrow the agent description: "
                    f"Agent Builder → Agent Instructions field (top-level) → "
                    f"remove mentions of {', '.join(gaps)} from the description if this agent is not meant to handle them. "
                    f"Salesforce docs: https://help.salesforce.com/s/articleView?id=sf.copilot_topics_add.htm"
                ),
            )
        )]
    return [CheckResult(rule_id=rule_id, passed=True)]


def check_dq13_actionless_topic_scope(root: ET.Element) -> list[CheckResult]:
    """DQ-13: Topic with no actions must have a response-only scope."""
    results = []
    action_verbs = ["create", "update", "delete", "send", "submit", "execute",
                    "process", "reset", "transfer", "escalate", "log"]
    for topic in get_local_topics(root):
        topic_label = get_text(topic, "masterLabel", "unknown topic")
        actions = get_topic_actions(topic)
        if actions:
            results.append(CheckResult(rule_id=f"DQ-13:{topic_label}", passed=True))
            continue
        scope = get_text(topic, "scope", "").lower()
        instructions_text = " ".join(
            get_text(i, "description", "").lower()
            for i in topic.findall(tag("genAiPluginInstructions"))
        )
        combined = scope + " " + instructions_text
        implies_action = any(v in combined for v in action_verbs)
        rule_id = f"DQ-13:{topic_label}"
        if implies_action:
            results.append(CheckResult(
                rule_id=rule_id,
                passed=False,
                finding=Finding(
                    rule_id="DQ-13",
                    severity="medium",
                    title=f"{topic_label}: topic has no actions but instructions imply action execution",
                    location=f"GenAiPlannerBundle > {topic_label} > localActions (empty) vs instructions",
                    observed="No localActions defined, but instructions contain action verbs",
                    expected="Either add actions or restrict scope to information-only responses",
                    impact="Customers are routed to a topic that promises to execute an operation "
                           "but has no mechanism to do so. Agent produces empty or misleading responses.",
                    fix=(
                        f"Option A — add an action: Setup → Agents → [your agent] → Open in Agent Builder → "
                        f"Topics tab → '{topic_label}' → Actions section → '+ Add Action' → "
                        f"select the appropriate action → Save. "
                        f"Option B — restrict to information-only: Agent Builder → Topics tab → "
                        f"'{topic_label}' → Scope field → rewrite to: "
                        f"'Your job is to provide information only. You do not execute any operations.' "
                        f"Also edit each Instruction step to remove action verbs (execute, update, create, submit). "
                        f"Salesforce docs: https://help.salesforce.com/s/articleView?id=sf.copilot_topics_add.htm"
                    ),
                )
            ))
        else:
            results.append(CheckResult(rule_id=rule_id, passed=True))
    return results or [CheckResult(rule_id="DQ-13:global", passed=True)]


def check_dq14_instruction_scope_conflict(root: ET.Element) -> list[CheckResult]:
    """DQ-14: Topic instructions must not contradict topic scope."""
    kb_prohibition = ["do not search the knowledge base", "do not use kb",
                      "do not call knowledge", "no knowledge base"]
    kb_action_signals = ["answerquestions", "knowledge", "knowledgesearch"]
    results = []
    for topic in get_local_topics(root):
        topic_label = get_text(topic, "masterLabel", "unknown topic")
        scope = get_text(topic, "scope", "").lower()
        scope_prohibits_kb = any(s in scope for s in kb_prohibition)
        if not scope_prohibits_kb:
            results.append(CheckResult(rule_id=f"DQ-14:{topic_label}", passed=True))
            continue
        instructions_text = " ".join(
            get_text(i, "description", "").lower()
            for i in topic.findall(tag("genAiPluginInstructions"))
        )
        instructions_call_kb = any(s in instructions_text for s in kb_action_signals)
        rule_id = f"DQ-14:{topic_label}"
        if instructions_call_kb:
            results.append(CheckResult(
                rule_id=rule_id,
                passed=False,
                finding=Finding(
                    rule_id="DQ-14",
                    severity="high",
                    title=f"{topic_label}: scope prohibits KB search but step instructions call KB action",
                    location=f"GenAiPlannerBundle > {topic_label} > scope vs genAiPluginInstructions",
                    observed="Scope says 'do not search knowledge base', instructions reference KB action",
                    expected="Instructions must not reference actions that the scope prohibits",
                    impact="The instruction takes precedence over scope at runtime. "
                           "The KB will be called despite the scope prohibition — "
                           "scope compliance is a false guarantee.",
                    fix=(
                        f"UI path: Setup → Agents → [your agent] → Open in Agent Builder → "
                        f"Topics tab → click '{topic_label}' → Instructions section → "
                        f"edit each instruction step that references knowledge base or AnswerQuestionsWithKnowledge "
                        f"→ remove or replace that reference with the intended action → Save. "
                        f"Alternatively, update the Scope field to remove the 'do not search knowledge base' prohibition "
                        f"if KB search is actually permitted for this topic. "
                        f"Salesforce docs: https://help.salesforce.com/s/articleView?id=sf.copilot_topic_instructions.htm"
                    ),
                )
            ))
        else:
            results.append(CheckResult(rule_id=rule_id, passed=True))
    return results or [CheckResult(rule_id="DQ-14:global", passed=True)]


# ---------------------------------------------------------------------------
# Run all checks
# ---------------------------------------------------------------------------

def run_all_checks(bundle_path: Path, plugin_files: list[Path], org: str = "") -> HealthReport:
    root = parse_bundle(bundle_path)
    agent_name = get_text(root, "masterLabel", bundle_path.stem)

    all_results: list[CheckResult] = []
    # Platform Mechanics
    all_results += check_pm01_confirmation_required(root)
    all_results += check_pm07_voice_omnichannel(root)
    all_results += check_pm08_can_escalate(root)
    all_results += check_pm11_sel_rule_blocks_pstn(root)
    all_results += check_pm12_voice_outbound_route(root)
    all_results += check_pm13_escalation_message(root)
    all_results += check_pm14_omnichannel_flow_exists(root, org)
    all_results += check_pm15_contact_center_exists(root, org)
    all_results += check_pm16_instruction_length(root)
    all_results += check_pm17_duplicate_topic_names(root)
    all_results += check_pm18_duplicate_action_names(root)
    all_results += check_pm19_voice_progress_indicator(root)
    all_results += check_pm20_voice_adaptive_response(root)
    all_results += check_pm21_call_recording_set(root)
    all_results += check_pm22_has_topics(root)
    all_results += check_pm23_dangling_topic_links(root)
    all_results += check_pm24_dangling_action_links(root)
    all_results += check_pm25_planner_type_surface_mismatch(root)
    # Design Quality
    all_results += check_dq01_topic_overlap(root)
    all_results += check_dq02_fallback_instructions(root)
    all_results += check_dq03_kb_in_action_topics(root, plugin_files)
    all_results += check_dq04_sensitive_operations(root)
    all_results += check_dq05_topic_before_action(root)
    all_results += check_dq06_fallback_one_question(root)
    all_results += check_dq10_scope_exclusions(root)
    all_results += check_dq11_escalation_loop(root)
    all_results += check_dq12_topic_coverage(root)
    all_results += check_dq13_actionless_topic_scope(root)
    all_results += check_dq14_instruction_scope_conflict(root)

    report = HealthReport(
        agent_name=agent_name,
        org_alias="",
        run_timestamp=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    for result in all_results:
        if result.passed:
            report.passed_checks.append(result.rule_id)
        elif result.finding:
            report.findings.append(result.finding)

    report.findings.sort(key=lambda f: SEVERITY_ORDER.get(f.severity, 9))
    return report


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

SEVERITY_EMOJI = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}
SEVERITY_LABEL = {"critical": "CRITICAL", "high": "HIGH", "medium": "MEDIUM", "low": "LOW"}


def render_report(report: HealthReport) -> str:
    lines = []
    total = len(report.findings) + len(report.passed_checks)
    critical = sum(1 for f in report.findings if f.severity == "critical")
    high = sum(1 for f in report.findings if f.severity == "high")
    medium = sum(1 for f in report.findings if f.severity == "medium")
    low = sum(1 for f in report.findings if f.severity == "low")
    passed = len(report.passed_checks)
    failed = len(report.findings)

    lines.append(f"# AgentEval Health Report — {report.agent_name}")
    lines.append(f"Org: {report.org_alias}  |  Run: {report.run_timestamp}")
    lines.append("")

    # Executive Summary — what architects/leaders read first
    lines.append("## Executive Summary")
    lines.append("")
    if not report.findings:
        lines.append(f"**{report.agent_name} is healthy.** All {passed} checks passed.")
        lines.append("No critical, high, medium, or low-severity issues found.")
        lines.append("")
    else:
        # Lead sentence — calibrated to severity
        if critical > 0:
            lead = (f"**{report.agent_name} has critical issues that should be addressed "
                    f"before production use.**")
        elif high > 0:
            lead = (f"**{report.agent_name} is functionally deployable but contains issues "
                    f"that may degrade customer experience.**")
        else:
            lead = (f"**{report.agent_name} is largely healthy with minor refinements available.**")
        lines.append(lead)
        lines.append("")

        # Severity bullets — only include non-zero buckets
        if critical:
            label = "issue" if critical == 1 else "issues"
            lines.append(f"- 🔴 **{critical} critical {label}** — may block expected runtime behavior")
        if high:
            label = "gap" if high == 1 else "gaps"
            lines.append(f"- 🟠 **{high} high-risk {label}** — likely to degrade customer experience")
        if medium:
            label = "item" if medium == 1 else "items"
            lines.append(f"- 🟡 **{medium} medium-risk {label}** — quality concerns worth addressing")
        if low:
            label = "item" if low == 1 else "items"
            lines.append(f"- 🔵 **{low} low-risk polish {label}** — refinements for routing clarity")
        lines.append("")

        # Top priorities — first 3 findings (already sorted by severity)
        top_findings = report.findings[:3]
        if top_findings:
            lines.append("### Top priorities")
            for i, f in enumerate(top_findings, start=1):
                emoji = SEVERITY_EMOJI.get(f.severity, "⚪")
                lines.append(f"{i}. {emoji} {f.title}")
            lines.append("")

    # Verdict — pass/fail first, then severity breakdown
    if not report.findings:
        lines.append("## Verdict: PASS")
        lines.append(f"✅ All {passed} checks passed — agent config looks healthy.")
    else:
        has_critical_or_high = critical > 0 or high > 0
        verdict = "FAIL" if has_critical_or_high else "NEEDS ATTENTION"
        lines.append(f"## Verdict: {verdict}")
        lines.append(f"**{failed} failed**  |  **{passed} passed**  |  {total} total checks")
        lines.append("")
        parts = []
        if critical: parts.append(f"🔴 {critical} critical")
        if high: parts.append(f"🟠 {high} high")
        if medium: parts.append(f"🟡 {medium} medium")
        if low: parts.append(f"🔵 {low} low")
        lines.append("Severity: " + "  ·  ".join(parts))
    lines.append("")

    # What Failed — quick-scan list with ❌
    if report.findings:
        lines.append("## What Failed")
        lines.append("")
        for f in report.findings:
            emoji = SEVERITY_EMOJI.get(f.severity, "⚪")
            label = SEVERITY_LABEL.get(f.severity, f.severity.upper())
            rule_label = format_rule_label(f.rule_id)
            lines.append(f"- ❌ {emoji} **{label}** · {rule_label} — {f.title}")
        lines.append("")

    # What Passed — quick-scan list with ✅ grouped by category
    if report.passed_checks:
        lines.append("## What Passed")
        lines.append("")
        rules_seen: dict[str, list[str]] = {}
        for check_id in report.passed_checks:
            rule = check_id.split(":")[0]
            rules_seen.setdefault(rule, []).append(check_id)
        by_category: dict[str, list[tuple[str, int]]] = {}
        for rule, checks in rules_seen.items():
            prefix = rule.split("-", 1)[0] if "-" in rule else rule
            category = RULE_CATEGORIES.get(prefix, prefix)
            by_category.setdefault(category, []).append((rule, len(checks)))
        for category in sorted(by_category):
            lines.append(f"### {category}")
            for rule_id, count in sorted(by_category[category]):
                desc = RULE_DESCRIPTIONS.get(rule_id, "")
                label = format_rule_label(rule_id)
                count_tag = f" ({count} checks)" if count > 1 else ""
                if desc:
                    lines.append(f"- ✅ **{label}** — {desc}{count_tag}")
                else:
                    lines.append(f"- ✅ **{label}**{count_tag}")
            lines.append("")
        lines.append("")

    # Issue Details — full remediation for each finding
    if report.findings:
        lines.append("---")
        lines.append("")
        lines.append("## Issue Details")
        lines.append("")
        for f in report.findings:
            emoji = SEVERITY_EMOJI.get(f.severity, "⚪")
            label = SEVERITY_LABEL.get(f.severity, f.severity.upper())
            candidate_tag = " *(candidate rule)*" if f.candidate else ""
            lines.append(f"### {emoji} {label} — {f.title}{candidate_tag}")
            lines.append(f"**Rule:** {format_rule_label(f.rule_id)} ({f.rule_id})")
            lines.append(f"**Location:** `{f.location}`")
            lines.append(f"**Found:** {f.observed}")
            lines.append(f"**Expected:** {f.expected}")
            lines.append(f"**Impact:** {f.impact}")
            lines.append(f"**Fix:** {f.fix}")
            lines.append("")

    lines.append("---")
    lines.append("*Generated by AgentEval v0.3.2 — Phase 1: Static Config Analysis*")
    lines.append("*Rules sourced from Salesforce platform docs, Well-Architected principles, and confirmed empirical observations.*")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_output(report: HealthReport, rendered: str) -> Path:
    output_dir = Path("outputs")
    output_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{report.agent_name}_{timestamp}_healthcheck.md"
    output_path = output_dir / filename
    output_path.write_text(rendered, encoding="utf-8")
    return output_path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="AgentEval Phase 1 — Agentforce Config Health Check"
    )
    parser.add_argument("--org", required=True, help="Salesforce org alias")
    parser.add_argument("--agent", help="Agent API name (DeveloperName)")
    parser.add_argument("--list-agents", action="store_true", help="List available agents in org")
    args = parser.parse_args()

    # Pre-flight: verify org access before doing anything else
    print(f"Checking access to org '{args.org}'...")
    check_org_access(args.org)

    if args.list_agents:
        agents = list_agents(args.org)
        if not agents:
            print("No agents found in org.")
        else:
            print(f"Available agents in '{args.org}':")
            for a in agents:
                print(f"  {a}")
        return

    if not args.agent:
        parser.error("--agent is required unless --list-agents is specified")

    print(f"Retrieving config for agent '{args.agent}' from org '{args.org}'...")

    with tempfile.TemporaryDirectory() as work_dir:
        ok = retrieve_agent_config(args.org, args.agent, work_dir)
        if not ok:
            sys.exit(1)

        bundle_path = find_bundle_file(work_dir, args.agent)
        if not bundle_path:
            print(f"Could not find GenAiPlannerBundle file in retrieved metadata.", file=sys.stderr)
            sys.exit(1)

        plugin_files = find_plugin_files(work_dir)
        print(f"Found bundle: {bundle_path.name}")
        print(f"Found {len(plugin_files)} plugin file(s)")

        report = run_all_checks(bundle_path, plugin_files, org=args.org)
        report.org_alias = args.org

    rendered = render_report(report)
    print("\n" + rendered)

    output_path = write_output(report, rendered)
    print(f"\nReport saved to: {output_path}")


if __name__ == "__main__":
    main()
