# Synthetic Fraud/Mule Network Dataset

Generated for hackathon use case #47 (Scam, Mule Network & Suspicious
Transaction Detection). Deterministic (seed=42) — regenerate anytime by
rerunning generate_fraud_data.py.

## Files
- **accounts.csv** — 5000 account nodes (account_id, type, opened_date, payday, monthly_salary)
- **transactions.csv** — 123403 transactions (edges). This is the ONLY file your model should train/score on.
- **ground_truth.csv** — labels (is_fraud, pattern_type) for evaluation only. Do not feed to the model — use it to compute precision/recall/F1.

## Fraud rate
0.584% of transactions are fraudulent (110 injected mule rings), consistent with realistic AML imbalance (<1%).

## Injected patterns (see pattern_type in ground_truth.csv)
- **fan_out_smurfing** — one source rapidly splits large funds across many mule accounts
- **fan_in_aggregation** — many small feeder transfers (kept under reporting thresholds) converge into one aggregator, which cashes out within hours
- **layering_chain** — funds bounce through a chain of accounts with slight skims at each hop to obscure origin
- **rapid_passthrough** — a mule account receives one large deposit and empties it externally within minutes

## Why this isn't naive random data
- Normal traffic has structure: payday salary deposits, log-normal spend amounts, merchant categories — so your model has to learn what's actually anomalous rather than exploiting obvious randomness.
- Fraud is graph-shaped (fan-in/out, chains), not just a flagged row — this is why account-relationship/graph analytics matter for #47, not just per-transaction ML.
- Class imbalance is realistic (~0.58%), forcing you to report precision/recall/F1 instead of accuracy.
