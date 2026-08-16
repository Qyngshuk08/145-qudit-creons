# Use Case #47 -- Scam, Mule Network & Suspicious Transaction Detection
Team Qudit Creons (Abhishek Raj, Divyansh Singh) -- TechCon Hackathon, Coforge
Registration ID: 145 | Mentor: Sunil Enadle

## Status
- data/       WORKING -- synthetic account/transaction dataset, ~0.58% fraud rate.
              FROZEN: accounts.csv/transactions.csv/ground_truth.csv are committed
              to git. Do NOT rerun generate_fraud_data.py unless you intend to
              replace the baseline (the generator isn't reproducible across
              machines/NumPy versions -- see .gitignore comment).
- graph/      WORKING -- KuzuDB graph DB built from the frozen dataset. Single
              source of truth for detection (not raw CSVs directly).
- detection/  WORKING -- graph-based mule pattern scoring (fan-out, fan-in,
              layering chains, rapid pass-through). Current eval on the frozen
              dataset: precision 0.774, recall 0.626, F1 0.692
              (see detection/evaluation_report.txt).
- api/        WORKING -- FastAPI service. Endpoints:
                GET /health
                GET /accounts/flagged?min_score=&limit=   (precomputed scores)
                GET /accounts/{id}                         (live KuzuDB drill-down)
                GET /accounts/{id}/explain                 (Nemotron narrative,
                                                             precomputed cache or live)
                GET /accounts/{id}/investigate              (LangGraph agent:
                                                             network-aware escalation)
- dashboard/  WORKING -- React + Vite investigator UI. Flagged-accounts list,
              per-account drill-down, Nemotron explanation panel.
- agents/     WORKING -- LangGraph investigation agent (agents/investigation_agent.py).
              Checks an account's transaction counterparties for other flagged
              accounts before deciding freeze vs. escalate-to-network-investigation.
- docs/       idea submission deck source (.tex, Beamer)

## One-time setup
1. python -m venv venv ; .\venv\Scripts\Activate.ps1
2. pip install -r requirements.txt
3. Set NVIDIA_API_KEY as a permanent environment variable (needed for Nemotron
   explanations/narratives -- see api/nemotron_client.py). Never commit this key.

## Run order
1. python graph\build_graph_db.py           # builds KuzuDB from the frozen data/ CSVs
2. python detection\score_mule_network.py   # scores accounts -> account_risk_scores.csv
3. python api\precompute_explanations.py --top 20   # optional: cache Nemotron explanations
                                                      # for demo-safe instant loading
4. uvicorn api.main:app --port 8000         # start the API (keep running)
5. In a second terminal: cd dashboard ; npm install ; npm run dev
                                             # start the dashboard, open the printed URL

Note: only ONE process can hold graph/mule_graph_db open at a time (KuzuDB
single-writer lock). Don't run the standalone agents/investigation_agent.py
script while the API is also running against the same DB -- use the API's
/investigate endpoint instead, which reuses the API's connection.

## Known gaps / next steps
- False negatives (241 at last eval) not yet broken down by pattern type
- Fan-in detector still weakest individual signal (sender-count threshold only)
- dashboard/ has no visual network graph yet (just list + drill-down)