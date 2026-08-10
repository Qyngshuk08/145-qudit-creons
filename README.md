# Use Case #47 -- Scam, Mule Network & Suspicious Transaction Detection
Team Qudit Creons (Abhishek Raj, Divyansh Singh) -- TechCon Hackathon, Coforge

## Status
- data/       WORKING -- synthetic account/transaction generator, ~0.58% fraud rate
- detection/  WORKING BUT WEAK -- graph-based mule pattern scoring.
              Current eval: precision 0.426, recall 0.126, F1 0.194 (see detection/evaluation_report.txt)
              Known gap: layering-chain detection under-fires badly; needs a
              real graph-algorithm rewrite, not another parameter tweak.
- api/        NOT STARTED
- dashboard/  NOT STARTED
- agents/     NOT STARTED
- docs/       idea submission deck source (.tex, Beamer)

## Run order
1. python data/generate_fraud_data.py     # writes accounts/transactions/ground_truth CSVs
2. python detection/score_mule_network.py # scores accounts, writes account_risk_scores.csv + evaluation_report.txt
