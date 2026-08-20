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
GRAPH_DB_PATH = os.path.join(_PROJECT_ROOT, "graph", "mule_graph_db")


def load_and_build_graph():
    """Load transactions from KuzuDB (not raw CSV) -- this is now the single
    source of truth for detection. accounts.csv/ground_truth.csv are still
    read directly for account metadata and evaluation (those aren't part of
    the graph itself). Run graph/build_graph_db.py first if this errors with
    'no database found'.
    """
    import kuzu

    if not os.path.exists(GRAPH_DB_PATH):
        raise FileNotFoundError(
            f"No KuzuDB database at {GRAPH_DB_PATH}. Run graph/build_graph_db.py first."
        )

    db = kuzu.Database(GRAPH_DB_PATH)
    conn = kuzu.Connection(db)

    rows = []
    # Pull all three endpoint-type combinations (matches how build_graph_db.py
    # split the load) and recombine into one flat table, same shape as the
    # original transactions.csv, so every detector below is unchanged.
    queries = [
        "MATCH (a:Account)-[t:TRANSACTION]->(b:Account) RETURN a.account_id, b.account_id, t.transaction_id, t.amount, t.timestamp, t.category",
        "MATCH (a:Account)-[t:TRANSACTION]->(b:ExternalEntity) RETURN a.account_id, b.entity_id, t.transaction_id, t.amount, t.timestamp, t.category",
        "MATCH (a:ExternalEntity)-[t:TRANSACTION]->(b:Account) RETURN a.entity_id, b.account_id, t.transaction_id, t.amount, t.timestamp, t.category",
    ]
    for q in queries:
        result = conn.execute(q)
        while result.has_next():
            rows.append(result.get_next())

    txns = pd.DataFrame(rows, columns=["src_account", "dst_account", "transaction_id", "amount", "timestamp", "category"])
    txns["timestamp"] = pd.to_datetime(txns["timestamp"])
    print(f"Loaded {len(txns)} transactions from KuzuDB (source of truth: graph/mule_graph_db)")

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


def detect_fan_in(G, txns, window_minutes=400, min_senders=4, cashout_window_hours=6,
                   spike_multiplier=4.0):
    """Many accounts send to one recipient, which then empties out fast.

    RELATIVE BASELINE (not just a fixed count): before scoring, each
    candidate is compared against that SAME account's own historical rate
    of receiving from distinct senders. A marketplace naturally receiving
    from many different buyers has a high baseline -- 6 senders in a few
    hours is normal for it. A genuine fraud aggregator is usually a
    previously-quiet account -- the same 6 senders is a massive spike
    relative to its near-zero baseline. Only flag when the observed rate
    is a large multiple (spike_multiplier) of the account's own baseline,
    not just an absolute count. This is what makes the detector
    self-calibrating instead of using one fixed number for every account
    regardless of its normal traffic level.
    """
    flags = defaultdict(lambda: {"score": 0, "reasons": []})
    in_txns = txns.sort_values("timestamp")

    # Baseline: for every account, its historical distinct-sender rate
    # (senders per day, averaged over the full observation span in the data).
    span_days = max(1, (txns["timestamp"].max() - txns["timestamp"].min()).days)
    baseline_senders_per_day = (
        in_txns.groupby("dst_account")["src_account"].nunique() / span_days
    )

    for dst, grp in in_txns.groupby("dst_account"):
        grp = grp.sort_values("timestamp")
        times = grp["timestamp"].values
        baseline_rate = baseline_senders_per_day.get(dst, 0)
        for i in range(len(grp)):
            window = grp[(grp["timestamp"] >= times[i]) &
                         (grp["timestamp"] <= times[i] + np.timedelta64(window_minutes, "m"))]
            n_senders = window["src_account"].nunique()
            if n_senders >= min_senders:
                # Relative check: is this a real spike above baseline, or
                # just normal traffic for a naturally busy account?
                window_days = window_minutes / (60 * 24)
                expected_in_window = max(0.5, baseline_rate * window_days)  # floor avoids div-by-near-zero blowup
                spike_ratio = n_senders / expected_in_window
                if spike_ratio < spike_multiplier:
                    continue  # not anomalous relative to this account's own normal behavior

                inflow_end = window["timestamp"].max()
                outflow = txns[(txns["src_account"] == dst) &
                                (txns["timestamp"] > inflow_end) &
                                (txns["timestamp"] <= inflow_end + pd.Timedelta(hours=cashout_window_hours))]
                # Cashout is the PRIMARY gate for aggregator-side scoring, not
                # just a bonus. Sender-count spikes alone have too much
                # natural variance (a legitimate high-volume account's daily
                # arrivals are Poisson-distributed and will occasionally spike
                # 5-6x its own average by chance) to be trustworthy on their
                # own. Rapid cash-out afterward is what actually distinguishes
                # a fraud aggregator from a business that settles later/partially.
                if not outflow.empty:
                    score = min(35, n_senders * 3) + 20
                    reason = (f"fan_in_aggregation: received from {n_senders} accounts within "
                              f"{window_minutes}min ({spike_ratio:.1f}x its own baseline rate); "
                              f"followed by rapid outbound cash-out")
                else:
                    score = min(10, n_senders)  # weak signal alone, not enough to cross threshold by itself
                    reason = (f"fan_in_aggregation: received from {n_senders} accounts within "
                              f"{window_minutes}min ({spike_ratio:.1f}x its own baseline rate); "
                              f"no rapid cash-out observed")
                flags[dst]["score"] = max(flags[dst]["score"], score)
                flags[dst]["reasons"].append(reason)
                for feeder in window["src_account"].unique():
                    feeder_score = 32 if not outflow.empty else 15
                    flags[feeder]["score"] = max(flags[feeder]["score"], feeder_score)
                    flags[feeder]["reasons"].append(
                        f"fan_in_aggregation: one of {n_senders} accounts feeding a common recipient within {window_minutes}min"
                    )
                break
    return flags


def detect_rapid_passthrough(G, txns, max_gap_hours=2, min_ratio=0.85, size_multiplier=3.0):
    """Account receives a large sum and forwards most of it out within hours.

    RELATIVE SIZE CHECK: a busy account processing many small transactions
    will, by pure chance, occasionally have an unrelated in/out pair within
    the time window that happens to satisfy a loose amount-ratio match --
    that's noise, not pass-through fraud. Genuine pass-through involves an
    unusually LARGE lump sum relative to that account's own normal
    transaction size. Requiring the incoming amount to be a multiple of the
    account's own median transaction size filters out routine coincidences
    (a marketplace's typical $15-250 purchases) while still catching real
    fraud (thousands of dollars moving through an otherwise-quiet account).
    """
    flags = defaultdict(lambda: {"score": 0, "reasons": []})
    # Each account's own typical transaction size, across in+out activity
    all_amounts_by_account = defaultdict(list)
    for _, r in txns.iterrows():
        all_amounts_by_account[r["src_account"]].append(r["amount"])
        all_amounts_by_account[r["dst_account"]].append(r["amount"])
    median_amount = {acct: pd.Series(amts).median() for acct, amts in all_amounts_by_account.items()}

    for acct, grp in txns.groupby("dst_account"):
        acct_out = txns[txns["src_account"] == acct]
        if acct_out.empty:
            continue
        baseline = median_amount.get(acct, 0) or 1  # avoid div-by-zero
        for _, in_row in grp.iterrows():
            if in_row["amount"] < baseline * size_multiplier:
                continue  # not unusually large for this account -- likely routine activity, skip
            candidates = acct_out[
                (acct_out["timestamp"] > in_row["timestamp"]) &
                (acct_out["timestamp"] <= in_row["timestamp"] + pd.Timedelta(hours=max_gap_hours))
            ]
            for _, out_row in candidates.iterrows():
                if in_row["amount"] > 0 and out_row["amount"] / in_row["amount"] >= min_ratio:
                    flags[acct]["score"] = max(flags[acct]["score"], 45)
                    flags[acct]["reasons"].append(
                        f"rapid_passthrough: received {in_row['amount']:.0f} ({in_row['amount']/baseline:.1f}x "
                        f"its own typical transaction size), forwarded {out_row['amount']:.0f} within {max_gap_hours}h"
                    )
    return flags


def detect_layering_chains(G, min_chain_length=3, max_hop_hours=2, max_starts_per_node=8, max_branches=3):
    """Find directed paths of rapid sequential P2P transfers (A->B->C->D...).

    v2: bounded depth-first search with backtracking, instead of a single
    greedy nearest-edge walk. The v1 walker picked one "next hop" per node
    and gave up if it was wrong -- with thousands of normal P2P transfers
    as noise, one wrong pick anywhere in the chain lost the whole thing.
    This version tries up to `max_branches` candidate next-hops at each
    step and backtracks on dead ends, which is what actually finds chains
    buried in noisy traffic. Still restricted to transfer_p2p edges and
    still bounded (not full path enumeration) to stay tractable on ~5.5k
    nodes / ~120k edges.
    """
    flags = defaultdict(lambda: {"score": 0, "reasons": []})
    found_chains = []

    def dfs(path, last_time, depth, budget):
        """path: list of account_ids visited so far. Returns True if a
        long-enough chain was found along this branch (for early stop)."""
        if depth >= min_chain_length:
            found_chains.append(list(path))
            return True
        if budget <= 0:
            return False
        current = path[-1]
        next_edges = sorted(
            [(u, v, d) for u, v, d in G.out_edges(current, data=True)
             if d["category"] == "transfer_p2p" and
             d["timestamp"] > last_time and
             d["timestamp"] <= last_time + pd.Timedelta(hours=max_hop_hours) and
             v not in path],
            key=lambda e: e[2]["timestamp"]
        )[:max_branches]
        for _, v, d in next_edges:
            if dfs(path + [v], d["timestamp"], depth + 1, budget - 1):
                return True  # one valid chain from this start is enough
        return False

    for node in G.nodes():
        out_edges = sorted(
            [(u, v, d) for u, v, d in G.out_edges(node, data=True) if d["category"] == "transfer_p2p"],
            key=lambda e: e[2]["timestamp"]
        )[:max_starts_per_node]
        for _, first_dst, first_data in out_edges:
            dfs([node, first_dst], first_data["timestamp"], 1, budget=200)

    for chain in found_chains:
        for acct in chain:
            flags[acct]["score"] = max(flags[acct]["score"], 30)
            flags[acct]["reasons"].append(
                f"layering_chain: part of {len(chain)}-hop rapid P2P transfer chain"
            )
    return flags


# ---------------------------------------------------------------------------
# 3. Combine signals into a single risk score per account
# ---------------------------------------------------------------------------
def combine_scores(*flag_dicts, corroboration_weight=0.2):
    """Combines scores from independent detectors.

    NOT pure summation (the old approach): the strongest single detector
    score counts fully as the PRIMARY signal; every additional detector's
    score only counts at corroboration_weight (default 40%). This reflects
    a real distinction -- two weak, independent, possibly-coincidental
    signals (e.g. a loose amount-ratio match plus a borderline recipient
    count) should NOT sum to equal one strong, confirmed pattern (e.g. a
    fan-in aggregator with an actual rapid cash-out). Pure summation let
    accounts get flagged purely by accumulating multiple weak coincidences
    across unrelated detectors -- this is what wrongly flagged legitimate
    high-volume businesses in testing.
    """
    per_account_detector_scores = defaultdict(list)
    per_account_reasons = defaultdict(list)
    for flags in flag_dicts:
        for acct, info in flags.items():
            per_account_detector_scores[acct].append(info["score"])
            per_account_reasons[acct].extend(info["reasons"])

    combined = defaultdict(lambda: {"risk_score": 0, "reasons": []})
    for acct, scores in per_account_detector_scores.items():
        scores_sorted = sorted(scores, reverse=True)
        primary = scores_sorted[0]
        corroboration = sum(scores_sorted[1:]) * corroboration_weight
        combined[acct]["risk_score"] = min(100, round(primary + corroboration))
        combined[acct]["reasons"] = per_account_reasons[acct]
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