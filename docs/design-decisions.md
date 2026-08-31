# Design Decisions & Trade-off Analysis — Use Case #47

Every major architectural choice, why it was made, what the alternative was,
and what we gave up. Written to directly address the "Design Clarity" and
"Achieves Results" rubric criteria (Stage 3): articulate choice of technology,
trade-off analysis, and comparative evaluation — not just what was built.

## 1. Detection approach: rule-based scoring vs. Graph Neural Network (GNN)
**Chosen:** Hand-coded, explainable rule engine over four fraud patterns.

**Alternative considered:** GNN-based detection (the current industry research
direction — dynamic heterogeneous graphs, GNN + LLM hybrids for explanation).

**Trade-off:**
- GNN pros: state-of-the-art pattern recognition, learns patterns not
  explicitly coded, industry momentum
- GNN cons: requires a labeled training set at meaningful scale (we have no
  company data); is a black box requiring a *second* model (SHAP/GNNExplainer)
  bolted on just to explain its own decisions — directly working against the
  regulatory need for auditable reasoning
- Rule-based cons: doesn't adapt automatically to new fraud patterns; ceiling
  on sophistication vs. a trained model at scale

**Why we chose it:** Every flag is explainable by construction — the
evidence string IS the reasoning, not a post-hoc approximation of it. For a
regulated financial use case, that auditability outweighs the raw pattern-
detection ceiling of an unexplainable model, especially without a real
training dataset available.

## 2. Graph database: KuzuDB vs. Neo4j
**Chosen:** KuzuDB (embedded).

**Trade-off:**
- Neo4j: larger community, more tutorials/Stack Overflow coverage, but
  requires a separate JVM server process running continuously
- KuzuDB: embedded (no separate service, no JVM overhead), lighter resource
  footprint — matters directly on constrained hardware (16GB dev laptop,
  4-core/24GB cloud instance)
- Neither team member had prior Cypher experience, so the "more tutorials"
  advantage of Neo4j was the only real point in its favor, and it was
  outweighed by the resource cost

**Why we chose it:** Given zero prior graph-DB experience on the team, the
learning curve was equivalent either way; KuzuDB's lower resource footprint
won on a resource-constrained environment.

## 3. Score combination: max + damped corroboration vs. pure summation
**Chosen:** The strongest single detector signal counts fully; additional
corroborating signals count at reduced weight (not full additive stacking).

**Trade-off:**
- Pure summation (our original approach): simple, but multiple independent
  *weak* signals could sum past the flagging threshold even when none of
  them individually indicated fraud — directly caused false positives on
  legitimate high-volume businesses in testing
- Damped corroboration: correctly suppresses coincidental multi-signal
  noise, but risked compressing genuine multi-pattern fraud scores too
  (caught and rebalanced during testing — see evaluation log)

**Why we chose it:** Validated empirically, not just theoretically — the
legitimate-lookalike test (payroll/marketplace accounts) directly measured
the false-positive cost of pure summation, and this fix eliminated it
without weakening recall (stayed at 1.000 throughout).

## 4. Fraud pattern thresholds: relative/statistical vs. fixed absolute
**Chosen:** Thresholds calibrated against each account's own historical
baseline (e.g., "this account's fan-in rate is 5x its own normal"), not a
single fixed number applied to every account.

**Trade-off:**
- Fixed thresholds: simple to reason about, but assume a uniform "normal"
  across every account — a marketplace naturally receiving from many buyers
  looks identical to a mule aggregator under a fixed count-based rule
- Relative thresholds: self-calibrating per account, but requires meaningful
  transaction history to establish a baseline (a brand-new account has none)

**Why we chose it:** Directly exposed by external validation — running our
original fixed-threshold detectors against Santander AI Lab's open-source
`gen-fraud-graph` synthetic benchmark produced a 100% false-positive rate,
because that dataset's traffic timing didn't match our own generator's
assumptions. This proved fixed thresholds were overfit to one dataset's
shape, not a generalizable detection principle.

## 5. Data strategy: synthetic generation vs. waiting for company data
**Chosen:** Built our own synthetic account/transaction generator immediately
rather than blocking on data that was never going to be provided.

**Trade-off:**
- Unblocked development from day one, but introduced a real risk: does a
  synthetic generator's traffic actually resemble real-world patterns?

**Why we chose it, and how we managed the risk:** Rather than assume our
synthetic data was representative, we validated against an independent,
externally-built synthetic benchmark (Santander's `gen-fraud-graph`) as a
sanity check — this is what surfaced the fixed-threshold generalization gap
above. We treat this as an ongoing validation discipline, not a one-time
check.

## 6. Explanation layer: LLM-generated (Nemotron) vs. templated text
**Chosen:** NVIDIA Nemotron (via NIM) generates investigator-facing case
narratives from raw evidence.

**Trade-off:**
- Templated text: fully deterministic, zero external dependency risk, but
  reads as robotic and doesn't adapt language to context
- LLM-generated: reads like a real analyst's case note, names actual AML
  typologies, varies recommended action by severity — but introduces a
  third-party API dependency (observed transient failures in testing,
  mitigated with retry logic) and a cost-per-call consideration at scale

**Why we chose it:** The quality gap was substantial and directly visible in
testing — early templated-style output read as mechanical paraphrasing;
Nemotron output named real typologies and varied its recommendation
appropriately. The dependency risk is real but manageable (retry logic,
precomputed caching for demo-critical paths).

## 7. Agent orchestration: network-aware LangGraph agent vs. no agent layer
**Chosen:** A LangGraph agent that checks whether a flagged account's direct
transaction counterparties are *also* independently flagged before deciding
the recommendation.

**Trade-off:**
- No agent layer: simpler, but every account is scored in isolation — misses
  the "this is a ring, not one bad actor" signal entirely
- Agent layer: adds real decision-making (genuine branching logic verified
  against real data — 15/20 test accounts escalated, 5/20 didn't, driven by
  actual network structure), but adds a dependency on the explanation layer
  and more moving parts

**Why we chose it:** Deliberately scoped narrower than a full multi-agent
system — we assessed that wrapping the existing linear pipeline in LangGraph
without adding real multi-step reasoning would be decorative, not
functional. This agent was built specifically because it does something a
single-account score cannot: it looks beyond the flagged account itself.

## 8. Deployment: Oracle Cloud (Ampere A1.Flex) vs. AWS/GCP/Azure free tiers
**Chosen:** Oracle Cloud's Always Free Ampere ARM tier (4 OCPU, 24GB RAM).

**Trade-off:**
- AWS/GCP/Azure free tiers: better-documented, more Stack Overflow coverage,
  but capped around 1GB RAM on always-free instance types
- Oracle: less commonly used, but the only option with enough RAM headroom
  to run the graph database, API, and dashboard simultaneously without
  resource contention

**Why we chose it:** Direct resource requirement — running KuzuDB, FastAPI,
and the dashboard concurrently needs more than 1GB RAM; Oracle was the only
free-tier option that could actually host the full stack at once.