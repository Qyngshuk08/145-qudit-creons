"""
Synthetic Transaction & Mule Network Data Generator
====================================================
Generates a realistic-looking banking transaction dataset for use case #47
(AI-Powered Scam, Mule Network & Suspicious Transaction Detection Platform).

Design goals (why this isn't just random noise):
- Accounts are graph NODES, transactions are graph EDGES -> supports graph
  analytics (fan-in/fan-out, layering, circular flows), not just row-level ML.
- "Normal" traffic follows realistic patterns: payday salary deposits,
  recurring bills, log-normal amount distributions, merchant categories.
- Fraud is injected as a small minority class (~0.6%) using known mule
  network topologies: fan-out (smurfing), fan-in (mule aggregation),
  layering chains, and rapid pass-through accounts.
- Ground truth labels are kept in a SEPARATE file so the model/dashboard
  never trains directly on the answer key -> lets you report real
  precision/recall/F1 instead of hand-waving it.

Output files (written to /mnt/user-data/outputs/):
  accounts.csv      - node table (account_id, type, opened_date, risk_seed)
  transactions.csv   - edge table (the data your model actually sees)
  ground_truth.csv   - labels: transaction_id, account_id -> is_fraud, pattern_type
  README.md          - describes schema + how patterns were injected
"""

import numpy as np
import pandas as pd
import networkx as nx
from faker import Faker
import random
import uuid
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SEED = 42
random.seed(SEED)
np.random.seed(SEED)
fake = Faker()
Faker.seed(SEED)

N_ACCOUNTS = 5000
N_MULE_RINGS = 110         # number of distinct injected fraud networks
SIM_DAYS = 90
START_DATE = datetime(2026, 1, 1)

MERCHANT_CATEGORIES = [
    "grocery", "utilities", "rent", "restaurant", "fuel", "retail",
    "subscription", "healthcare", "insurance", "transfer_p2p"
]

import os
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

# ---------------------------------------------------------------------------
# 1. Build account nodes
# ---------------------------------------------------------------------------
def build_accounts(n=N_ACCOUNTS):
    accounts = []
    for i in range(n):
        acc_id = f"ACC{i:06d}"
        acc_type = np.random.choice(
            ["retail", "retail", "retail", "small_business", "new_account"],
            p=[0.55, 0.2, 0.15, 0.07, 0.03]
        )
        opened = START_DATE - timedelta(days=int(np.random.exponential(400)))
        # payday: most retail accounts get salary on a fixed day of month
        payday = random.randint(1, 28)
        salary = round(np.random.lognormal(mean=8.0, sigma=0.4), 2) if acc_type != "new_account" else 0
        accounts.append({
            "account_id": acc_id,
            "account_type": acc_type,
            "opened_date": opened.date().isoformat(),
            "payday": payday,
            "monthly_salary": salary,
        })
    return pd.DataFrame(accounts)


# ---------------------------------------------------------------------------
# 2. Generate normal (benign) transaction traffic
# ---------------------------------------------------------------------------
def generate_normal_transactions(accounts_df):
    txns = []
    acc_ids = accounts_df["account_id"].tolist()

    for _, row in accounts_df.iterrows():
        if row["account_type"] == "new_account":
            n_txns = np.random.poisson(3)
        else:
            n_txns = np.random.poisson(25)

        for _ in range(n_txns):
            day_offset = random.randint(0, SIM_DAYS - 1)
            date = START_DATE + timedelta(days=day_offset)

            # salary deposit on payday
            if row["monthly_salary"] > 0 and date.day == row["payday"] and random.random() < 0.9:
                txns.append(make_txn(
                    src="EXTERNAL_PAYROLL", dst=row["account_id"],
                    amount=row["monthly_salary"], date=date,
                    category="salary"
                ))
                continue

            category = random.choice(MERCHANT_CATEGORIES)
            amount = round(np.random.lognormal(mean=3.5, sigma=1.0), 2)

            if category == "transfer_p2p":
                counterparty = random.choice(acc_ids)
                if counterparty == row["account_id"]:
                    continue
                txns.append(make_txn(row["account_id"], counterparty, amount, date, category))
            else:
                merchant = f"MERCHANT_{category.upper()}_{random.randint(1,50)}"
                txns.append(make_txn(row["account_id"], merchant, amount, date, category))

    return txns


def make_txn(src, dst, amount, date, category, pattern="normal"):
    return {
        "transaction_id": str(uuid.uuid4())[:12],
        "src_account": src,
        "dst_account": dst,
        "amount": round(float(amount), 2),
        "timestamp": date.strftime("%Y-%m-%d") + f" {random.randint(0,23):02d}:{random.randint(0,59):02d}:00",
        "category": category,
        "_pattern": pattern,  # stripped before saving transactions.csv, kept in ground_truth.csv
    }


# ---------------------------------------------------------------------------
# 3. Inject mule network fraud topologies
# ---------------------------------------------------------------------------
def inject_fan_out_smurfing(base_id, accounts_df, n_mules=8):
    """One compromised account rapidly splits funds to many mule accounts."""
    source = f"MULE_SRC_{base_id}"
    mules = accounts_df["account_id"].sample(n_mules).tolist()
    txns = []
    date = START_DATE + timedelta(days=random.randint(0, SIM_DAYS - 3))
    total = np.random.uniform(8000, 20000)
    for m in mules:
        share = total / n_mules * np.random.uniform(0.8, 1.2)
        txns.append(make_txn(source, m, share, date, "transfer_p2p", pattern="fan_out_smurfing"))
        date += timedelta(minutes=random.randint(2, 40))
    return txns


def inject_fan_in_aggregation(base_id, accounts_df, n_feeders=10):
    """Many small accounts feed into one mule aggregator, then it empties out fast."""
    aggregator = accounts_df["account_id"].sample(1).iloc[0]
    feeders = accounts_df["account_id"].sample(n_feeders).tolist()
    txns = []
    date = START_DATE + timedelta(days=random.randint(0, SIM_DAYS - 3))
    for f in feeders:
        amt = np.random.uniform(300, 900)  # kept under common reporting thresholds
        txns.append(make_txn(f, aggregator, amt, date, "transfer_p2p", pattern="fan_in_aggregation"))
        date += timedelta(minutes=random.randint(5, 60))
    # rapid pass-through: aggregator empties to external within hours
    txns.append(make_txn(
        aggregator, f"EXTERNAL_CASHOUT_{base_id}",
        sum(t["amount"] for t in txns) * 0.95, date + timedelta(hours=2),
        "transfer_p2p", pattern="fan_in_aggregation"
    ))
    return txns


def inject_layering_chain(base_id, accounts_df, chain_length=5):
    """Funds bounce through a chain of accounts in quick succession to obscure origin."""
    chain = accounts_df["account_id"].sample(chain_length).tolist()
    txns = []
    date = START_DATE + timedelta(days=random.randint(0, SIM_DAYS - 3))
    amount = np.random.uniform(4000, 15000)
    for i in range(len(chain) - 1):
        amount *= np.random.uniform(0.9, 0.98)  # slight skim at each hop
        txns.append(make_txn(chain[i], chain[i + 1], amount, date, "transfer_p2p", pattern="layering_chain"))
        date += timedelta(minutes=random.randint(10, 90))
    txns.append(make_txn(chain[-1], f"EXTERNAL_CASHOUT_{base_id}", amount, date, "transfer_p2p", pattern="layering_chain"))
    return txns


def inject_rapid_passthrough(base_id, accounts_df):
    """A newly opened 'mule' account receives one large deposit and empties within minutes."""
    mule = accounts_df["account_id"].sample(1).iloc[0]
    date = START_DATE + timedelta(days=random.randint(0, SIM_DAYS - 1))
    amount = np.random.uniform(2000, 9000)
    txns = [
        make_txn(f"EXTERNAL_SCAM_VICTIM_{base_id}", mule, amount, date, "transfer_p2p", pattern="rapid_passthrough"),
        make_txn(mule, f"EXTERNAL_CASHOUT_{base_id}", amount * 0.97, date + timedelta(minutes=random.randint(5, 30)),
                  "transfer_p2p", pattern="rapid_passthrough"),
    ]
    return txns


def generate_fraud_transactions(accounts_df, n_rings=N_MULE_RINGS):
    txns = []
    injectors = [inject_fan_out_smurfing, inject_fan_in_aggregation, inject_layering_chain, inject_rapid_passthrough]
    for i in range(n_rings):
        fn = injectors[i % len(injectors)]
        txns.extend(fn(i, accounts_df))
    return txns


# ---------------------------------------------------------------------------
# 4. Assemble, split into transactions.csv + ground_truth.csv
# ---------------------------------------------------------------------------
def main():
    print("Generating accounts...")
    accounts_df = build_accounts()

    print("Generating normal transaction traffic...")
    normal_txns = generate_normal_transactions(accounts_df)

    print("Injecting mule network fraud patterns...")
    fraud_txns = generate_fraud_transactions(accounts_df)

    all_txns = normal_txns + fraud_txns
    random.shuffle(all_txns)
    txns_df = pd.DataFrame(all_txns)

    fraud_rate = (txns_df["_pattern"] != "normal").mean()
    print(f"Total transactions: {len(txns_df)} | Fraud rate: {fraud_rate:.3%}")

    # Ground truth: kept separate from the "working" dataset
    ground_truth = txns_df[["transaction_id", "src_account", "dst_account", "_pattern"]].copy()
    ground_truth["is_fraud"] = (ground_truth["_pattern"] != "normal").astype(int)
    ground_truth = ground_truth.rename(columns={"_pattern": "pattern_type"})

    # Public-facing transactions table (no answer key)
    transactions_public = txns_df.drop(columns=["_pattern"])

    import os
    os.makedirs(OUT_DIR, exist_ok=True)
    accounts_df.to_csv(f"{OUT_DIR}/accounts.csv", index=False)
    transactions_public.to_csv(f"{OUT_DIR}/transactions.csv", index=False)
    ground_truth.to_csv(f"{OUT_DIR}/ground_truth.csv", index=False)

    # Quick graph sanity check
    G = nx.DiGraph()
    for _, r in transactions_public.iterrows():
        G.add_edge(r["src_account"], r["dst_account"], amount=r["amount"])
    print(f"Graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")

    readme = f"""# Synthetic Fraud/Mule Network Dataset

Generated for hackathon use case #47 (Scam, Mule Network & Suspicious
Transaction Detection). Deterministic (seed={SEED}) — regenerate anytime by
rerunning generate_fraud_data.py.

## Files
- **accounts.csv** — {len(accounts_df)} account nodes (account_id, type, opened_date, payday, monthly_salary)
- **transactions.csv** — {len(transactions_public)} transactions (edges). This is the ONLY file your model should train/score on.
- **ground_truth.csv** — labels (is_fraud, pattern_type) for evaluation only. Do not feed to the model — use it to compute precision/recall/F1.

## Fraud rate
{fraud_rate:.3%} of transactions are fraudulent ({N_MULE_RINGS} injected mule rings), consistent with realistic AML imbalance (<1%).

## Injected patterns (see pattern_type in ground_truth.csv)
- **fan_out_smurfing** — one source rapidly splits large funds across many mule accounts
- **fan_in_aggregation** — many small feeder transfers (kept under reporting thresholds) converge into one aggregator, which cashes out within hours
- **layering_chain** — funds bounce through a chain of accounts with slight skims at each hop to obscure origin
- **rapid_passthrough** — a mule account receives one large deposit and empties it externally within minutes

## Why this isn't naive random data
- Normal traffic has structure: payday salary deposits, log-normal spend amounts, merchant categories — so your model has to learn what's actually anomalous rather than exploiting obvious randomness.
- Fraud is graph-shaped (fan-in/out, chains), not just a flagged row — this is why account-relationship/graph analytics matter for #47, not just per-transaction ML.
- Class imbalance is realistic (~{fraud_rate:.2%}), forcing you to report precision/recall/F1 instead of accuracy.
"""
    with open(f"{OUT_DIR}/README.md", "w") as f:
        f.write(readme)

    print("Done. Files written to", OUT_DIR)


if __name__ == "__main__":
    main()
