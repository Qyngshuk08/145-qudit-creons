"""
Risk Scoring API -- Use Case #47
==================================
Hybrid design (deliberate, not a shortcut):

  GET /accounts/flagged      -> serves PRECOMPUTED risk scores from
                                 detection/account_risk_scores.csv. Fast
                                 (in-memory), matches the evaluation numbers
                                 exactly. Rerun detection/score_mule_network.py
                                 to refresh; this endpoint doesn't recompute.

  GET /accounts/{id}         -> LIVE query against graph/mule_graph_db
                                 (KuzuDB) for that account's actual current
                                 transaction neighborhood -- real-time at the
                                 investigation level, not full-scan detection
                                 level.

Why not fully live scoring per request: the window-scan detectors
(fan-out/fan-in/layering) take real time to run over the full transaction
set -- not viable as a per-request cost. Why not fully static: an
investigator drilling into one account should see that account's real graph
data, not a screenshot from whenever detection last ran.

Run: uvicorn api.main:app --reload --port 8000
Docs: http://127.0.0.1:8000/docs (FastAPI auto-generates this)
"""

import os
import pandas as pd
import kuzu
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCORES_PATH = os.path.join(_PROJECT_ROOT, "detection", "account_risk_scores.csv")
GRAPH_DB_PATH = os.path.join(_PROJECT_ROOT, "graph", "mule_graph_db")
EXPLANATIONS_PATH = os.path.join(_PROJECT_ROOT, "detection", "account_explanations.csv")

_state = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load precomputed scores once at startup (small file, fine to hold in memory)
    if not os.path.exists(SCORES_PATH):
        raise FileNotFoundError(f"No {SCORES_PATH}. Run detection/score_mule_network.py first.")
    _state["scores_df"] = pd.read_csv(SCORES_PATH)

    if not os.path.exists(GRAPH_DB_PATH):
        raise FileNotFoundError(f"No graph DB at {GRAPH_DB_PATH}. Run graph/build_graph_db.py first.")
    _state["db"] = kuzu.Database(GRAPH_DB_PATH)
    _state["conn"] = kuzu.Connection(_state["db"])

    # Precomputed explanations are optional -- API works without them, the
    # /explain endpoint just falls back to live generation if missing.
    if os.path.exists(EXPLANATIONS_PATH):
        exp_df = pd.read_csv(EXPLANATIONS_PATH)
        _state["explanations"] = dict(zip(exp_df["account_id"], exp_df["explanation"]))
        print(f"Loaded {len(_state['explanations'])} precomputed explanations.")
    else:
        _state["explanations"] = {}
        print("No precomputed explanations found -- /explain will generate live only.")

    print(f"Loaded {len(_state['scores_df'])} precomputed scores; connected to KuzuDB.")
    yield
    _state.clear()


app = FastAPI(title="Mule Network Detection API", lifespan=lifespan)

# Dev-only CORS -- tighten before anything resembling production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok", "scored_accounts": len(_state["scores_df"])}


@app.get("/accounts/flagged")
def get_flagged_accounts(min_score: int = 30, limit: int = 50):
    """Precomputed risk scores, ranked highest first. This is the list an
    investigator dashboard would show as its main queue."""
    df = _state["scores_df"]
    filtered = df[df["risk_score"] >= min_score].sort_values("risk_score", ascending=False).head(limit)
    return filtered.to_dict(orient="records")


@app.get("/accounts/{account_id}")
def get_account_detail(account_id: str):
    """Live KuzuDB query: this account's actual current transaction
    neighborhood (in + out), plus its precomputed risk info if it has any."""
    conn = _state["conn"]

    # Confirm the account exists
    exists = conn.execute(
        "MATCH (a:Account) WHERE a.account_id = $id RETURN a.account_id",
        {"id": account_id}
    )
    if not exists.has_next():
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found")

    # Live: outgoing transactions (to other accounts or external entities)
    out_txns = []
    for q, label in [
        ("MATCH (a:Account)-[t:TRANSACTION]->(b:Account) WHERE a.account_id = $id "
         "RETURN b.account_id, t.amount, t.timestamp, t.category ORDER BY t.timestamp DESC LIMIT 20", "account"),
        ("MATCH (a:Account)-[t:TRANSACTION]->(b:ExternalEntity) WHERE a.account_id = $id "
         "RETURN b.entity_id, t.amount, t.timestamp, t.category ORDER BY t.timestamp DESC LIMIT 20", "external"),
    ]:
        result = conn.execute(q, {"id": account_id})
        while result.has_next():
            row = result.get_next()
            out_txns.append({"counterparty": row[0], "counterparty_type": label,
                              "amount": row[1], "timestamp": row[2], "category": row[3]})

    # Live: incoming transactions
    in_txns = []
    for q, label in [
        ("MATCH (a:Account)-[t:TRANSACTION]->(b:Account) WHERE b.account_id = $id "
         "RETURN a.account_id, t.amount, t.timestamp, t.category ORDER BY t.timestamp DESC LIMIT 20", "account"),
        ("MATCH (a:ExternalEntity)-[t:TRANSACTION]->(b:Account) WHERE b.account_id = $id "
         "RETURN a.entity_id, t.amount, t.timestamp, t.category ORDER BY t.timestamp DESC LIMIT 20", "external"),
    ]:
        result = conn.execute(q, {"id": account_id})
        while result.has_next():
            row = result.get_next()
            in_txns.append({"counterparty": row[0], "counterparty_type": label,
                             "amount": row[1], "timestamp": row[2], "category": row[3]})

    # Precomputed risk info, if this account was flagged
    scores_df = _state["scores_df"]
    risk_row = scores_df[scores_df["account_id"] == account_id]
    risk_info = risk_row.to_dict(orient="records")[0] if not risk_row.empty else {
        "risk_score": 0, "triggered_patterns": "", "evidence": "not flagged"
    }

    return {
        "account_id": account_id,
        "risk_info_source": "precomputed (last detection run)",
        "risk_score": risk_info.get("risk_score", 0),
        "triggered_patterns": risk_info.get("triggered_patterns", ""),
        "evidence": risk_info.get("evidence", ""),
        "live_outgoing_transactions": out_txns,
        "live_incoming_transactions": in_txns,
        "transaction_neighborhood_source": "live (KuzuDB, this request)",
    }


@app.get("/accounts/{account_id}/explain")
def explain_account_endpoint(account_id: str):
    """Investigator-facing narrative explanation via Nemotron.

    Checks the precomputed cache first (instant, demo-safe). Falls back to
    a live NIM call if not cached -- this is the "technical depth" showcase
    path, but costs real latency and API quota, so don't rely on it for
    every account you click during a live demo.
    """
    scores_df = _state["scores_df"]
    risk_row = scores_df[scores_df["account_id"] == account_id]
    if risk_row.empty:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found or not flagged")
    risk_info = risk_row.to_dict(orient="records")[0]

    if account_id in _state["explanations"]:
        return {
            "account_id": account_id,
            "explanation": _state["explanations"][account_id],
            "source": "precomputed",
        }

    try:
        from api.nemotron_client import explain_account
        explanation = explain_account(
            account_id=account_id,
            risk_score=int(risk_info["risk_score"]),
            patterns=risk_info["triggered_patterns"],
            evidence=risk_info["evidence"],
        )
        return {"account_id": account_id, "explanation": explanation, "source": "live (Nemotron)"}
    except Exception as e:
        # Don't let an API/key failure break the dashboard -- fall back to raw evidence
        raise HTTPException(
            status_code=502,
            detail=f"Explanation generation failed ({e}). Raw evidence: {risk_info['evidence']}"
        )


@app.get("/accounts/{account_id}/investigate")
def investigate_account_endpoint(account_id: str):
    """Runs the LangGraph investigation agent, reusing this process's
    already-open KuzuDB connection (_state["conn"]) rather than opening a
    second one -- avoids KuzuDB's single-process lock conflict that occurs
    if the standalone agent script is run while this API is also running."""
    from agents.investigation_agent import fetch_account, check_network_risk, decide_action, generate_narrative

    state = {
        "account_id": account_id, "risk_score": 0, "triggered_patterns": "",
        "evidence": "", "flagged_counterparties": [], "recommended_action": "", "narrative": "",
    }
    try:
        state = fetch_account(state)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found in risk scores")

    state = check_network_risk(state, conn=_state["conn"])  # reuse the API's open connection
    state = decide_action(state)
    state = generate_narrative(state)

    return {
        "account_id": state["account_id"],
        "risk_score": state["risk_score"],
        "flagged_counterparties": state["flagged_counterparties"],
        "recommended_action": state["recommended_action"],
        "narrative": state["narrative"],
    }