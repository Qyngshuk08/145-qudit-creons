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
def build_legitimate_business_accounts(n_payroll=3, n_marketplace=3):
    """Registers the payroll/marketplace accounts as REAL accounts (not
    external placeholders) so they're part of the scored universe -- without
    this, they'd be invisible to detection entirely and the false-positive
    test would be meaningless (you'd never know if the detector handles
    them correctly or is just never looking at them)."""
    accounts = []
    for p in range(n_payroll):
        accounts.append({
            "account_id": f"LEGIT_PAYROLL_{p}", "account_type": "business_payroll",
            "opened_date": (START_DATE - timedelta(days=800)).date().isoformat(),
            "payday": 0, "monthly_salary": 0,
        })
    for m in range(n_marketplace):
        accounts.append({
            "account_id": f"LEGIT_MARKETPLACE_{m}", "account_type": "business_marketplace",
            "opened_date": (START_DATE - timedelta(days=800)).date().isoformat(),
            "payday": 0, "monthly_salary": 0,
        })
    return pd.DataFrame(accounts)


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
            date = START_DATE + timedelta(days=day_offset, hours=random.randint(0, 23), minutes=random.randint(0, 59))

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
        "timestamp": date.strftime("%Y-%m-%d %H:%M:00"),
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
    date = START_DATE + timedelta(days=random.randint(0, SIM_DAYS - 3), hours=random.randint(0, 20), minutes=random.randint(0, 59))
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
    date = START_DATE + timedelta(days=random.randint(0, SIM_DAYS - 3), hours=random.randint(0, 20), minutes=random.randint(0, 59))
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
    date = START_DATE + timedelta(days=random.randint(0, SIM_DAYS - 3), hours=random.randint(0, 20), minutes=random.randint(0, 59))
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
    date = START_DATE + timedelta(days=random.randint(0, SIM_DAYS - 1), hours=random.randint(0, 20), minutes=random.randint(0, 59))
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
def generate_legitimate_lookalikes(accounts_df, n_payroll=3, n_marketplace=3):
    """Generates realistic LEGITIMATE high-volume accounts that structurally
    resemble fan-out/fan-in fraud patterns -- a payroll processor paying many
    employees looks like fan-out smurfing by volume alone; a marketplace
    receiving from many buyers looks like fan-in aggregation. These are the
    real-world "toy model" trap: a detector using raw volume thresholds
    alone will wrongly flag these. Marked in ground truth as
    'legitimate_lookalike' (NOT fraud) so you can measure false positives
    on them specifically, separate from your overall FP count.

    Distinguishing signal from real fraud (deliberately built in, so a
    correctly-designed detector CAN tell them apart): recurring payments to
    the SAME set of counterparties over multiple weeks, regular amounts,
    and -- critically -- no rapid full cash-out afterward.
    """
    txns = []
    real_account_ids = accounts_df["account_id"].tolist()

    # Payroll processor: pays the SAME ~40 employees biweekly, consistent
    # amounts -- looks like recurring fan-out, but recurring + stable
    # recipients + no cashout is what separates it from smurfing.
    for p in range(n_payroll):
        processor_id = f"LEGIT_PAYROLL_{p}"
        employees = random.sample(real_account_ids, k=min(40, len(real_account_ids)))
        base_salary = np.random.uniform(2500, 6000)
        for week_start in range(0, SIM_DAYS, 14):  # biweekly pay cycles
            date = START_DATE + timedelta(days=week_start, hours=9)
            for emp in employees:
                amt = base_salary * np.random.uniform(0.95, 1.05)  # stable amount, not random each time
                txns.append(make_txn(processor_id, emp, amt, date, "salary", pattern="legitimate_lookalike"))
                date += timedelta(seconds=random.randint(1, 30))  # payroll runs are batch-fast, not fraud-fast

    # Marketplace: receives from many DIFFERENT buyers continuously (unlike
    # payroll's stable recipient list) -- but never rapidly cashes out the
    # full balance, which is what separates it from a fan-in mule aggregator.
    for m in range(n_marketplace):
        marketplace_id = f"LEGIT_MARKETPLACE_{m}"
        for day in range(SIM_DAYS):
            date = START_DATE + timedelta(days=day)
            n_buyers_today = np.random.poisson(6)
            for _ in range(n_buyers_today):
                buyer = random.choice(real_account_ids)
                amt = np.random.uniform(15, 250)  # typical purchase amounts
                txns.append(make_txn(buyer, marketplace_id, amt, date + timedelta(minutes=random.randint(0, 600)),
                                      "retail", pattern="legitimate_lookalike"))
            # partial, delayed settlement payout -- NOT a rapid full cashout
            if day % 7 == 6:  # weekly settlement, days later than the purchases, partial amount
                settlement_amt = np.random.uniform(500, 2000)
                txns.append(make_txn(marketplace_id, f"EXTERNAL_SETTLEMENT_{m}", settlement_amt,
                                      date + timedelta(days=3), "transfer_p2p", pattern="legitimate_lookalike"))

    return txns


def main():
    print("Generating accounts...")
    accounts_df = build_accounts()
    legit_business_df = build_legitimate_business_accounts()
    accounts_df = pd.concat([accounts_df, legit_business_df], ignore_index=True)

    print("Generating normal transaction traffic...")
    normal_txns = generate_normal_transactions(accounts_df)

    print("Injecting mule network fraud patterns...")
    fraud_txns = generate_fraud_transactions(accounts_df)

    print("Generating legitimate high-volume lookalikes (payroll, marketplace)...")
    lookalike_txns = generate_legitimate_lookalikes(accounts_df)

    all_txns = normal_txns + fraud_txns + lookalike_txns
    random.shuffle(all_txns)
    txns_df = pd.DataFrame(all_txns)

    fraud_rate = (txns_df["_pattern"].isin(["normal", "legitimate_lookalike"]) == False).mean()
    lookalike_rate = (txns_df["_pattern"] == "legitimate_lookalike").mean()
    print(f"Total transactions: {len(txns_df)} | Fraud rate: {fraud_rate:.3%} | Legitimate-lookalike rate: {lookalike_rate:.3%}")

    # Ground truth: kept separate from the "working" dataset
    ground_truth = txns_df[["transaction_id", "src_account", "dst_account", "_pattern"]].copy()
    ground_truth["is_fraud"] = (~ground_truth["_pattern"].isin(["normal", "legitimate_lookalike"])).astype(int)
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