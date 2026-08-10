"""
Mule Network Graph Construction & Anomaly Scoring
===================================================
Loads transactions.csv (from generate_fraud_data.py), builds a directed
account graph, and scores every account/transaction for the four mule
patterns your synthetic data injects:
  - fan_out_smurfing     (one source -> many rapid recipients)
  - fan_in_aggregation   (many sources -> one recipient -> fast cash-out)
  - layering_chain       (funds bounce through a sequence of accounts)
  - rapid_passthrough    (large in, large out, same account, minutes apart)

This does NOT read ground_truth.csv while scoring (that would defeat the
point) -- it only uses ground_truth.csv at the end, to report precision/
recall/F1, exactly like a real evaluation would.

Output (written to /mnt/user-data/outputs/):
  account_risk_scores.csv  - one row per account: risk_score (0-100),
                              triggered_patterns, evidence summary
  evaluation_report.txt    - precision/recall/F1 against ground truth
"""

import pandas as pd
import numpy as np
import networkx as nx
from collections import defaultdict
from datetime import datetime

import os
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IN_DIR = os.path.join(_PROJECT_ROOT, "data")     # reads accounts/transactions/ground_truth from here
OUT_DIR = os.path.join(_PROJECT_ROOT, "detection")  # writes scores/report here

# ---------------------------------------------------------------------------
# 1. Load data & build directed multigraph
# ---------------------------------------------------------------------------
def load_and_build_graph():
    txns = pd.read_csv(f"{IN_DIR}/transactions.csv", parse_dates=["timestamp"])
    G = nx.MultiDiGraph()
    for _, r in txns.iterrows():
        G.add_edge(
            r["src_account"], r["dst_account"],
            transaction_id=r["transaction_id"],
            amount=r["amount"], timestamp=r["timestamp"], category=r["category"]
        )
    return txns, G


# ---------------------------------------------------------------------------
# 2. Pattern detectors (graph + time-window heuristics -- no ML black box,
#    so every flag has an explainable reason for the investigator dashboard)
# ---------------------------------------------------------------------------
def detect_fan_out(G, txns, window_minutes=300, min_recipients=4):
    """One account sends to many distinct recipients in a short window.

    Flags the RECIPIENTS (newly-recruited mule accounts), not the source --
    the source of a fan-out is typically an already-compromised account
    (often external / not in our scored universe), while the recipients are
    the accounts an investigator actually needs to act on.
    """
    flags = defaultdict(lambda: {"score": 0, "reasons": []})
    out_txns = txns.sort_values("timestamp")
    for src, grp in out_txns.groupby("src_account"):
        grp = grp.sort_values("timestamp")
        times = grp["timestamp"].values
        for i in range(len(grp)):
            window = grp[(grp["timestamp"] >= times[i]) &
                         (grp["timestamp"] <= times[i] + np.timedelta64(window_minutes, "m"))]
            recipients = window["dst_account"].unique()
            if len(recipients) >= min_recipients:
                for r in recipients:
                    flags[r]["score"] = max(flags[r]["score"], min(40, len(recipients) * 4))
                    flags[r]["reasons"].append(
                        f"fan_out_smurfing: received funds as one of {len(recipients)} rapid "
                        f"recipients from a single source within {window_minutes}min"
                    )
                break
    return flags


def detect_fan_in(G, txns, window_minutes=400, min_senders=4, cashout_window_hours=6):
    """Many accounts send to one recipient, which then empties out fast."""
    flags = defaultdict(lambda: {"score": 0, "reasons": []})
    in_txns = txns.sort_values("timestamp")
    for dst, grp in in_txns.groupby("dst_account"):
        grp = grp.sort_values("timestamp")
        times = grp["timestamp"].values
        for i in range(len(grp)):
            window = grp[(grp["timestamp"] >= times[i]) &
                         (grp["timestamp"] <= times[i] + np.timedelta64(window_minutes, "m"))]
            n_senders = window["src_account"].nunique()
            if n_senders >= min_senders:
                # check for rapid cash-out after aggregation
                inflow_end = window["timestamp"].max()
                outflow = txns[(txns["src_account"] == dst) &
                                (txns["timestamp"] > inflow_end) &
                                (txns["timestamp"] <= inflow_end + pd.Timedelta(hours=cashout_window_hours))]
                score = min(35, n_senders * 3)
                reason = f"fan_in_aggregation: received from {n_senders} accounts within {window_minutes}min"
                if not outflow.empty:
                    score += 20
                    reason += "; followed by rapid outbound cash-out"
                flags[dst]["score"] = max(flags[dst]["score"], score)
                flags[dst]["reasons"].append(reason)
                # feeder accounts are lower-confidence individually (could be
                # coincidental) but still worth a moderate flag for review
                for feeder in window["src_account"].unique():
                    feeder_score = 20 if not outflow.empty else 15
                    flags[feeder]["score"] = max(flags[feeder]["score"], feeder_score)
                    flags[feeder]["reasons"].append(
                        f"fan_in_aggregation: one of {n_senders} accounts feeding a common recipient within {window_minutes}min"
                    )
                break
    return flags


def detect_rapid_passthrough(G, txns, max_gap_hours=2, min_ratio=0.85):
    """Account receives a large sum and forwards most of it out within hours."""
    flags = defaultdict(lambda: {"score": 0, "reasons": []})
    for acct, grp in txns.groupby("dst_account"):
        acct_out = txns[txns["src_account"] == acct]
        if acct_out.empty:
            continue
        for _, in_row in grp.iterrows():
            candidates = acct_out[
                (acct_out["timestamp"] > in_row["timestamp"]) &
                (acct_out["timestamp"] <= in_row["timestamp"] + pd.Timedelta(hours=max_gap_hours))
            ]
            for _, out_row in candidates.iterrows():
                if in_row["amount"] > 0 and out_row["amount"] / in_row["amount"] >= min_ratio:
                    flags[acct]["score"] = max(flags[acct]["score"], 45)
                    flags[acct]["reasons"].append(
                        f"rapid_passthrough: received {in_row['amount']:.0f}, forwarded "
                        f"{out_row['amount']:.0f} within {max_gap_hours}h"
                    )
    return flags


def detect_layering_chains(G, min_chain_length=3, max_hop_hours=2, max_starts_per_node=5):
    """Find short directed paths of rapid sequential P2P transfers (A->B->C->D).

    Restricted to category == 'transfer_p2p' edges: merchant/utility payments
    are not part of layering behavior and were drowning out the real chains
    when the walker tried arbitrary edges. We also try several candidate
    start edges per node (not just the earliest) since a node's first
    outbound transaction of the day is usually a normal payment, not fraud.
    """
    flags = defaultdict(lambda: {"score": 0, "reasons": []})
    for node in G.nodes():
        out_edges = sorted(
            [(u, v, d) for u, v, d in G.out_edges(node, data=True) if d["category"] == "transfer_p2p"],
            key=lambda e: e[2]["timestamp"]
        )
        for _, first_dst, first_data in out_edges[:max_starts_per_node]:
            chain = [node, first_dst]
            last_time = first_data["timestamp"]
            current = first_dst
            for _ in range(min_chain_length):
                next_edges = [
                    (u, v, d) for u, v, d in G.out_edges(current, data=True)
                    if d["category"] == "transfer_p2p" and
                    d["timestamp"] > last_time and
                    d["timestamp"] <= last_time + pd.Timedelta(hours=max_hop_hours)
                ]
                if not next_edges:
                    break
                u, v, d = sorted(next_edges, key=lambda e: e[2]["timestamp"])[0]
                if v in chain:
                    break
                chain.append(v)
                last_time = d["timestamp"]
                current = v
            if len(chain) >= min_chain_length + 1:
                for acct in chain:
                    flags[acct]["score"] = max(flags[acct]["score"], 30)
                    flags[acct]["reasons"].append(
                        f"layering_chain: part of {len(chain)}-hop rapid P2P transfer chain"
                    )
    return flags


# ---------------------------------------------------------------------------
# 3. Combine signals into a single risk score per account
# ---------------------------------------------------------------------------
def combine_scores(*flag_dicts):
    combined = defaultdict(lambda: {"risk_score": 0, "reasons": []})
    for flags in flag_dicts:
        for acct, info in flags.items():
            combined[acct]["risk_score"] = min(100, combined[acct]["risk_score"] + info["score"])
            combined[acct]["reasons"].extend(info["reasons"])
    return combined


# ---------------------------------------------------------------------------
# 4. Evaluate against ground truth (precision/recall/F1) -- read only here
# ---------------------------------------------------------------------------
def evaluate(combined, threshold=30):
    gt = pd.read_csv(f"{IN_DIR}/ground_truth.csv")
    fraud_accounts = set(gt[gt["is_fraud"] == 1]["src_account"]) | set(gt[gt["is_fraud"] == 1]["dst_account"])
    # exclude synthetic external/mule-source placeholder nodes from scoring universe
    all_accounts = set(pd.read_csv(f"{IN_DIR}/accounts.csv")["account_id"])
    fraud_accounts = fraud_accounts & all_accounts

    flagged = {a for a, info in combined.items() if info["risk_score"] >= threshold} & all_accounts

    tp = len(flagged & fraud_accounts)
    fp = len(flagged - fraud_accounts)
    fn = len(fraud_accounts - flagged)

    precision = tp / (tp + fp) if (tp + fp) else 0
    recall = tp / (tp + fn) if (tp + fn) else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0

    report = f"""Evaluation @ risk_score threshold={threshold}
Fraud accounts in ground truth (excl. external nodes): {len(fraud_accounts)}
Accounts flagged by detector: {len(flagged)}
True Positives:  {tp}
False Positives: {fp}
False Negatives: {fn}
Precision: {precision:.3f}
Recall:    {recall:.3f}
F1:        {f1:.3f}
"""
    return report


# ---------------------------------------------------------------------------
def main():
    print("Loading data and building graph...")
    txns, G = load_and_build_graph()
    print(f"Graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")

    print("Running detectors (this scans time windows, may take a bit)...")
    fan_out = detect_fan_out(G, txns)
    print(f"  fan_out_smurfing: {len(fan_out)} accounts flagged")
    fan_in = detect_fan_in(G, txns)
    print(f"  fan_in_aggregation: {len(fan_in)} accounts flagged")
    passthrough = detect_rapid_passthrough(G, txns)
    print(f"  rapid_passthrough: {len(passthrough)} accounts flagged")
    layering = detect_layering_chains(G)
    print(f"  layering_chain: {len(layering)} accounts flagged")

    combined = combine_scores(fan_out, fan_in, passthrough, layering)

    rows = []
    for acct, info in combined.items():
        patterns = sorted(set(r.split(":")[0] for r in info["reasons"]))
        rows.append({
            "account_id": acct,
            "risk_score": info["risk_score"],
            "triggered_patterns": ";".join(patterns),
            "evidence": " | ".join(info["reasons"][:3]),  # cap evidence length
        })
    scores_df = pd.DataFrame(rows).sort_values("risk_score", ascending=False)
    scores_df.to_csv(f"{OUT_DIR}/account_risk_scores.csv", index=False)
    print(f"Wrote {len(scores_df)} scored accounts to account_risk_scores.csv")

    print("Evaluating against ground truth...")
    report = evaluate(combined)
    print(report)
    with open(f"{OUT_DIR}/evaluation_report.txt", "w") as f:
        f.write(report)


if __name__ == "__main__":
    main()
