"""
Investigation Agent (LangGraph)
==================================
Real orchestration, not decoration: given one flagged account, this agent
decides whether to escalate based on what it finds in the account's
transaction network -- not just restating that one account's own evidence.

Graph:
  fetch_account  -> check_network_risk  -> decide_action -> generate_narrative
"""

import os
import sys
import pandas as pd
import kuzu
from typing import TypedDict
from langgraph.graph import StateGraph, END

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from api.nemotron_client import explain_account

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCORES_PATH = os.path.join(_PROJECT_ROOT, "detection", "account_risk_scores.csv")
GRAPH_DB_PATH = os.path.join(_PROJECT_ROOT, "graph", "mule_graph_db")

NETWORK_ESCALATION_THRESHOLD = 2
COUNTERPARTY_FLAG_THRESHOLD = 50


class InvestigationState(TypedDict):
    account_id: str
    risk_score: int
    triggered_patterns: str
    evidence: str
    flagged_counterparties: list
    recommended_action: str
    narrative: str


def _load_scores():
    return pd.read_csv(SCORES_PATH)


def fetch_account(state: InvestigationState) -> InvestigationState:
    scores_df = _load_scores()
    row = scores_df[scores_df["account_id"] == state["account_id"]]
    if row.empty:
        raise ValueError(f"Account {state['account_id']} not found in risk scores")
    r = row.iloc[0]
    state["risk_score"] = int(r["risk_score"])
    state["triggered_patterns"] = r["triggered_patterns"]
    state["evidence"] = r["evidence"]
    return state


def check_network_risk(state: InvestigationState, conn=None) -> InvestigationState:
    """The actual investigation step: look at this account's direct
    transaction counterparties and see how many are ALSO flagged.

    Pass an existing `conn` (e.g. from the API's already-open connection)
    to avoid KuzuDB's single-process lock. Only opens its own connection
    if none is provided (standalone CLI use, when the API is NOT running)."""
    owns_connection = conn is None
    if owns_connection:
        db = kuzu.Database(GRAPH_DB_PATH)
        conn = kuzu.Connection(db)

    scores_df = _load_scores()
    flagged_ids = set(scores_df[scores_df["risk_score"] >= COUNTERPARTY_FLAG_THRESHOLD]["account_id"])
    flagged_ids.discard(state["account_id"])

    counterparties = set()
    for q in [
        "MATCH (a:Account)-[:TRANSACTION]->(b:Account) WHERE a.account_id = $id RETURN DISTINCT b.account_id",
        "MATCH (a:Account)-[:TRANSACTION]->(b:Account) WHERE b.account_id = $id RETURN DISTINCT a.account_id",
    ]:
        result = conn.execute(q, {"id": state["account_id"]})
        while result.has_next():
            counterparties.add(result.get_next()[0])

    state["flagged_counterparties"] = sorted(counterparties & flagged_ids)
    return state


def decide_action(state: InvestigationState) -> InvestigationState:
    n_flagged_neighbors = len(state["flagged_counterparties"])
    if n_flagged_neighbors >= NETWORK_ESCALATION_THRESHOLD:
        state["recommended_action"] = (
            f"ESCALATE TO NETWORK INVESTIGATION: {n_flagged_neighbors} of this account's direct "
            f"counterparties are also independently flagged ({', '.join(state['flagged_counterparties'][:5])}"
            f"{'...' if n_flagged_neighbors > 5 else ''}). This suggests a coordinated ring, not an "
            f"isolated account -- route to senior investigator for network-wide review, not a single-account freeze."
        )
    elif state["risk_score"] >= 70:
        state["recommended_action"] = "Freeze account pending individual investigation and KYC re-verification."
    else:
        state["recommended_action"] = "Flag for monitoring; no immediate freeze warranted at this risk level."
    return state


def generate_narrative(state: InvestigationState) -> InvestigationState:
    expanded_evidence = state["evidence"]
    if state["flagged_counterparties"]:
        expanded_evidence += (
            f" | network_finding: {len(state['flagged_counterparties'])} direct transaction "
            f"counterparties are independently flagged as high-risk accounts."
        )
    try:
        state["narrative"] = explain_account(
            account_id=state["account_id"],
            risk_score=state["risk_score"],
            patterns=state["triggered_patterns"],
            evidence=expanded_evidence,
        )
    except Exception as e:
        state["narrative"] = f"(Narrative generation failed: {e}) Recommended action: {state['recommended_action']}"
    return state


def build_graph():
    graph = StateGraph(InvestigationState)
    graph.add_node("fetch_account", fetch_account)
    graph.add_node("check_network_risk", check_network_risk)
    graph.add_node("decide_action", decide_action)
    graph.add_node("generate_narrative", generate_narrative)

    graph.set_entry_point("fetch_account")
    graph.add_edge("fetch_account", "check_network_risk")
    graph.add_edge("check_network_risk", "decide_action")
    graph.add_edge("decide_action", "generate_narrative")
    graph.add_edge("generate_narrative", END)

    return graph.compile()


_compiled_graph = None


def investigate(account_id: str) -> dict:
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_graph()
    initial_state = {
        "account_id": account_id, "risk_score": 0, "triggered_patterns": "",
        "evidence": "", "flagged_counterparties": [], "recommended_action": "", "narrative": "",
    }
    return _compiled_graph.invoke(initial_state)


if __name__ == "__main__":
    acct = sys.argv[1] if len(sys.argv) > 1 else "ACC000412"
    result = investigate(acct)
    print(f"Account: {result['account_id']}  Risk: {result['risk_score']}")
    print(f"Flagged counterparties: {result['flagged_counterparties']}")
    print(f"Recommended action: {result['recommended_action']}")
    print(f"Narrative: {result['narrative'][:200]}...")