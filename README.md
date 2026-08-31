# Use Case #47 -- Scam, Mule Network & Suspicious Transaction Detection
Team Qudit Creons (Abhishek Raj, Divyansh Singh) -- TechCon Hackathon, Coforge
Registration ID: 145 | Mentor: Sunil Enadle & Subramanya NS | Advanced to Stage 3 (eval 1-2 Sept 2026)

## Live deployment
**http://80.225.235.192:4173** -- running on Oracle Cloud (Ampere A1.Flex,
4 OCPU/24GB, Always Free tier), managed as systemd services (`qudit-api`,
`qudit-dashboard`), survives reboots/disconnects. API alone:
http://80.225.235.192:8000/health

## Status
- data/       WORKING -- synthetic dataset (accounts, transactions, ground
              truth) plus legitimate-lookalike accounts (payroll processor,
              marketplace) used to stress-test for false positives on real
              high-volume businesses. FROZEN and committed to git -- do not
              regenerate locally (generator isn't reproducible across
              machines/NumPy versions).
- graph/      WORKING -- KuzuDB graph DB built from the frozen dataset.
              Single source of truth for detection.
- detection/  WORKING -- four continuous-scoring detectors (fan-out, fan-in,
              layering chain, rapid pass-through) combined via max +
              damped-corroboration scoring (not pure summation -- prevents
              weak coincidental signals from stacking into false flags).
              Current eval: precision 0.855, recall 1.000, F1 0.921
              (see detection/evaluation_report.txt). Validated against an
              external benchmark (Santander AI Lab's gen-fraud-graph) which
              exposed and drove the fix for fixed-threshold overfitting.
- api/        WORKING -- FastAPI service:
                GET  /health
                GET  /accounts/flagged?min_score=&limit=
                GET  /accounts/{id}                (live KuzuDB drill-down)
                GET  /accounts/{id}/explain         (Nemotron narrative)
                GET  /accounts/{id}/investigate      (LangGraph agent)
                POST /transactions/ingest            (live ingestion, honest
                                                       scope: immediate
                                                       insert, NOT instant
                                                       rescoring)
                POST /detection/rescan               (async full rescan --
                                                       takes minutes, not
                                                       instant; poll
                                                       /detection/status)
- dashboard/  WORKING -- React + Vite, financial-terminal aesthetic (IBM Plex
              fonts, severity-based color, risk-meter bars), structured
              Nemotron explanation display (SEVERITY / CASE SUMMARY /
              RECOMMENDED ACTION parsed and rendered, not a wall of text).
- agents/     WORKING -- LangGraph investigation agent
              (agents/investigation_agent.py). Checks an account's direct
              transaction counterparties for other independently-flagged
              accounts before recommending freeze vs. escalate-to-network.
- docs/       idea_submission deck, stakeholder-map.md, design-decisions.md,
              stage3-final.tex/pdf (final Stage 3 presentation, C4-notation
              architecture diagram).
- validation/ gen_fraud_graph_validation.py -- runs the unchanged detection
              code against Santander's external synthetic benchmark.

## One-time setup (local dev)
1. python -m venv venv ; .\venv\Scripts\Activate.ps1 (Windows) or
   source venv/bin/activate (Linux/Mac)
2. pip install -r requirements.txt
3. Set NVIDIA_API_KEY as a permanent environment variable (needed for
   Nemotron explanations/narratives). Never commit this key.

## Run order (local dev)
1. python graph/build_graph_db.py
2. python detection/score_mule_network.py
3. python api/precompute_explanations.py --top 20   (optional, demo-safe caching)
4. uvicorn api.main:app --port 8000
5. cd dashboard ; npm install ; npm run dev

Note: only ONE process can hold graph/mule_graph_db open at a time (KuzuDB
single-writer lock). Don't run the standalone agents/investigation_agent.py
script while the API is also running -- use the API's /investigate endpoint
instead, which reuses the API's connection.

## Production deployment (Oracle Cloud)
Both services run as systemd units so they survive reboots and disconnects:
- /etc/systemd/system/qudit-api.service       (uvicorn, port 8000)
- /etc/systemd/system/qudit-dashboard.service (serve -s dist, port 4173)
Restart either with: sudo systemctl restart qudit-api / qudit-dashboard
Logs: sudo journalctl -u qudit-api -f (or qudit-dashboard)

## Known gaps / honest limitations
- Evaluation is on the same dataset used to tune thresholds, not a held-out
  split -- a genuine next step, not yet done
- fan_out_smurfing and layering_chain use simpler continuous scoring than
  fan_in/rapid_passthrough (both already fixed and validated)
- Live ingestion inserts immediately but rescoring is a full batch rescan
  (minutes), not true per-transaction incremental scoring
- Third-party Nemotron API has shown transient failures (retry logic added,
  not eliminated)