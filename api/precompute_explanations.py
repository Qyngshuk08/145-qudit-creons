"""
Precompute Explanations
=========================
Batch-generates Nemotron explanations for the top N highest-risk accounts
and caches them to detection/account_explanations.csv. Run this once
before a demo so the dashboard's "Explain" button returns instantly
(precomputed) instead of hitting Nemotron live for every click, which
burns your 40rpm rate limit and adds latency mid-demo.

Requires NVIDIA_API_KEY set (see api/nemotron_client.py for details).

Run: python api/precompute_explanations.py [--top N]
Default N=20 -- enough to cover what you'd realistically click through
live, without burning excessive API quota. Increase if you want broader
coverage; each account costs one Nemotron call.
"""

import os
import sys
import argparse
import time
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from api.nemotron_client import explain_account

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCORES_PATH = os.path.join(_PROJECT_ROOT, "detection", "account_risk_scores.csv")
OUT_PATH = os.path.join(_PROJECT_ROOT, "detection", "account_explanations.csv")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=20, help="Number of top-risk accounts to explain")
    args = parser.parse_args()

    if not os.path.exists(SCORES_PATH):
        raise FileNotFoundError(f"No {SCORES_PATH}. Run detection/score_mule_network.py first.")

    scores_df = pd.read_csv(SCORES_PATH)
    top = scores_df.sort_values("risk_score", ascending=False).head(args.top)
    print(f"Generating explanations for top {len(top)} accounts (rate limit: 40rpm, pacing accordingly)...")

    results = []
    for i, row in enumerate(top.itertuples(), 1):
        try:
            explanation = explain_account(
                account_id=row.account_id,
                risk_score=int(row.risk_score),
                patterns=row.triggered_patterns,
                evidence=row.evidence,
            )
            results.append({"account_id": row.account_id, "explanation": explanation})
            print(f"  [{i}/{len(top)}] {row.account_id} done")
        except Exception as e:
            print(f"  [{i}/{len(top)}] {row.account_id} FAILED: {e}")
        time.sleep(1.6)  # ~37/min, stays under the 40rpm limit with margin

    pd.DataFrame(results).to_csv(OUT_PATH, index=False)
    print(f"Wrote {len(results)} explanations to {OUT_PATH}")


if __name__ == "__main__":
    main()