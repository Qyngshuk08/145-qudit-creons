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

_state = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not os.path.exists(SCORES_PATH):
        raise FileNotFoundError(f"No {SCORES_PATH}. Run detection/score_mule_network.py first.")
    _state["scores_df"] = pd.read_csv(SCORES_PATH)

    if not os.path.exists(GRAPH_DB_PATH):
        raise FileNotFoundError(f"No graph DB at {GRAPH_DB_PATH}. Run graph/build_graph_db.py first.")
    _state["db"] = kuzu.Database(GRAPH_DB_PATH)
    _state["conn"] = kuzu.Connection(_state["db"])

    print(f"Loaded {len(_state['scores_df'])} precomputed scores; connected to KuzuDB.")
    yield
    _state.clear()


app = FastAPI(title="Mule Network Detection API", lifespan=lifespan)

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
    df = _state["scores_df"]
    filtered = df[df["risk_score"] >= min_score].sort_values("risk_score", ascending=False).head(limit)
    return filtered.to_dict(orient="records")


@app.get("/accounts/{account_id}")
def get_account_detail(account_id: str):
    conn = _state["conn"]

    exists = conn.execute(
        "MATCH (a:Account) WHERE a.account_id = $id RETURN a.account_id",
        {"id": account_id}
    )
    if not exists.has_next():
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found")

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