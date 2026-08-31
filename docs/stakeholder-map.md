# Stakeholder Map — Use Case #47

Who benefits from this solution, and what do they specifically need from it.
Written to directly address the "Stakeholder Identification" rubric criterion
(Stage 3): identify all beneficiaries and tie solution aspects to their needs.

## 1. Fraud/AML Investigators (primary end user)
**Who:** Front-line analysts who review flagged accounts and decide next action.

**Needs:**
- A ranked queue, not a raw alert flood — highest-risk cases first
- Explainable evidence per flag (not a black-box score)
- Low false-positive burden — every wrongly-flagged legitimate account is
  wasted investigation time
- Network context, not just single-account facts — is this account part of
  a larger ring, or isolated?

**How the solution addresses this:**
- Dashboard ranks by continuous risk score (0–100), not a flat threshold cutoff
- Every flag carries plain-language evidence (`fan_in_aggregation: received
  from 11 accounts within 400min (22.0x its own baseline rate)`)
- LangGraph investigation agent checks an account's transaction neighbors and
  explicitly recommends "escalate to network investigation" vs. "single-account
  review" — the investigator gets a routing decision, not just a score

## 2. Compliance Officers / Head of Financial Crime
**Who:** Own the bank's regulatory obligations; decide whether to file a
Suspicious Activity Report (SAR).

**Needs:**
- Defensible, auditable reasoning behind every flag (regulators can ask
  "why was this account frozen?")
- Language aligned with recognized typologies (FATF/FinCEN terms), not
  internal jargon
- A system that doesn't quietly punish legitimate high-volume customers
  (regulatory and reputational risk if a real business gets wrongly frozen)

**How the solution addresses this:**
- Nemotron-generated case notes name the actual typology (structuring,
  layering, smurfing) and cite the specific evidence, not just a score
- Severity tiering (CRITICAL/HIGH/MEDIUM) with a stated recommended action
  per tier, mirroring how a real SAR-decision workflow is structured
- Deliberately stress-tested against legitimate high-volume businesses
  (payroll processor, marketplace) before deployment — this is the direct
  answer to "how do you know this won't wrongly flag real customers"

## 3. The Bank's Customers (indirect beneficiary)
**Who:** Account holders, both potential fraud victims and legitimate
high-volume users (marketplaces, payroll recipients, etc.)

**Needs:**
- Fraud caught and stopped before their money is lost
- Legitimate account activity not disrupted by false flags

**How the solution addresses this:**
- Faster detection (near-real-time detection pass, seconds not days) means
  faster fund recovery windows
- The legitimate-lookalike testing (payroll/marketplace accounts) exists
  specifically because customer-side false positives are a real harm, not
  just a technical inconvenience

## 4. Regulators (RBI/FIU-IND context; FATF/FinCEN typology-aligned globally)
**Who:** External bodies the bank must demonstrate due diligence to.

**Needs:**
- Evidence the bank has systematic, defensible AML controls
- Auditable decision trails, not "the algorithm said so"

**How the solution addresses this:**
- Rule-based detection was deliberately chosen over a black-box ML model
  specifically because every flag is explainable by construction — no
  post-hoc explainability layer (SHAP/GNNExplainer) is needed to justify
  a decision after the fact

## 5. Bank IT / Security Teams
**Who:** Own production infrastructure and security posture.

**Needs:**
- A system that integrates without requiring a core-banking rebuild
- Reasonable resource footprint, standard open-source components

**How the solution addresses this:**
- Fully open-source stack (KuzuDB, FastAPI, React, LangGraph) — no
  proprietary vendor lock-in
- API-first architecture — the detection/agent layer is decoupled from the
  dashboard, so it can plug into an existing investigator tool instead of
  replacing it

## 6. Bank Leadership / Executives
**Who:** Own the business case — cost, risk, ROI.

**Needs:**
- Quantifiable reduction in fraud losses
- Quantifiable reduction in investigator hours wasted on false positives
- A credible path from prototype to production

**How the solution addresses this:**
- Precision improved from 0.746 to 0.855 in one testing cycle (110 false
  positives, down from 222) — a direct, quantifiable investigator-hours story
- Recall held at 1.000 throughout — zero fraud missed while cutting false
  positives, the actual cost/risk trade-off executives care about