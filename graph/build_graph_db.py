"""
KuzuDB Schema + Loader
=======================
Creates a local, embedded KuzuDB graph database from data/accounts.csv and
data/transactions.csv, so the mule-network graph lives in a real graph DB
instead of an in-memory NetworkX object rebuilt from scratch every run.

Node tables:
  Account         -- real accounts from accounts.csv
  ExternalEntity  -- external placeholders (EXTERNAL_PAYROLL, MERCHANT_*,
                     MULE_SRC_*, EXTERNAL_CASHOUT_*, EXTERNAL_SCAM_VICTIM_*)
                     that appear as src/dst in transactions but aren't in
                     accounts.csv. Kept as a separate table so account-level
                     graph queries aren't polluted by non-account nodes.

Relationship table:
  TRANSACTION(FROM AnyNode, TO AnyNode) -- Kuzu requires declared endpoint
  types, so we declare it twice (Account->Account, Account->ExternalEntity,
  ExternalEntity->Account) to cover every direction that occurs in the data.

Run: python graph/build_graph_db.py
Output: graph/mule_graph_db/  (a folder -- this IS the database, on disk)
"""

import kuzu
import pandas as pd
import os
import shutil

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(_PROJECT_ROOT, "data")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mule_graph_db")


def main():
    accounts_path = os.path.join(DATA_DIR, "accounts.csv")
    txns_path = os.path.join(DATA_DIR, "transactions.csv")

    if not os.path.exists(accounts_path) or not os.path.exists(txns_path):
        raise FileNotFoundError(
            f"Expected accounts.csv and transactions.csv in {DATA_DIR}. "
            "Run data/generate_fraud_data.py first."
        )

    # Fresh DB each run -- rerunning this script is meant to be idempotent
    if os.path.exists(DB_PATH):
        print(f"Removing existing DB at {DB_PATH} for a clean rebuild...")
        if os.path.isdir(DB_PATH):
            shutil.rmtree(DB_PATH)
        else:
            os.remove(DB_PATH)

    print("Loading CSVs...")
    accounts_df = pd.read_csv(accounts_path)
    txns_df = pd.read_csv(txns_path)

    # Every distinct account_id referenced in transactions that ISN'T a real
    # account (external placeholders / merchants) goes into ExternalEntity.
    real_ids = set(accounts_df["account_id"])
    all_txn_ids = set(txns_df["src_account"]) | set(txns_df["dst_account"])
    external_ids = sorted(all_txn_ids - real_ids)
    external_df = pd.DataFrame({"entity_id": external_ids})
    print(f"{len(accounts_df)} real accounts, {len(external_df)} external entities, {len(txns_df)} transactions")

    print(f"Creating database at {DB_PATH}...")
    db = kuzu.Database(DB_PATH)
    conn = kuzu.Connection(db)

    print("Creating schema...")
    conn.execute("""
        CREATE NODE TABLE Account(
            account_id STRING,
            account_type STRING,
            opened_date STRING,
            payday INT64,
            monthly_salary DOUBLE,
            PRIMARY KEY(account_id)
        )
    """)
    conn.execute("""
        CREATE NODE TABLE ExternalEntity(
            entity_id STRING,
            PRIMARY KEY(entity_id)
        )
    """)
    # Declare the relationship for every endpoint-type combination that
    # actually occurs in the data (Kuzu requires each FROM/TO pair explicitly).
    conn.execute("""
        CREATE REL TABLE TRANSACTION(
            FROM Account TO Account,
            FROM Account TO ExternalEntity,
            FROM ExternalEntity TO Account,
            transaction_id STRING,
            amount DOUBLE,
            timestamp STRING,
            category STRING
        )
    """)

    print("Loading accounts...")
    tmp_accounts = os.path.join(DATA_DIR, "_tmp_accounts_for_kuzu.csv")
    accounts_df.to_csv(tmp_accounts, index=False)
    conn.execute(f'COPY Account FROM "{tmp_accounts.replace(chr(92), "/")}" (HEADER=true)')
    os.remove(tmp_accounts)

    print("Loading external entities...")
    tmp_external = os.path.join(DATA_DIR, "_tmp_external_for_kuzu.csv")
    external_df.to_csv(tmp_external, index=False)
    conn.execute(f'COPY ExternalEntity FROM "{tmp_external.replace(chr(92), "/")}" (HEADER=true)')
    os.remove(tmp_external)

    print("Loading transactions (this is the big one, ~120k rows)...")
    # Split transactions by endpoint-type combination since COPY needs a
    # single FROM/TO node-table pair per load.
    real_ids_set = real_ids
    txns_df["_src_is_account"] = txns_df["src_account"].isin(real_ids_set)
    txns_df["_dst_is_account"] = txns_df["dst_account"].isin(real_ids_set)

    combos = [
        ("Account", "Account", txns_df["_src_is_account"] & txns_df["_dst_is_account"]),
        ("Account", "ExternalEntity", txns_df["_src_is_account"] & ~txns_df["_dst_is_account"]),
        ("ExternalEntity", "Account", ~txns_df["_src_is_account"] & txns_df["_dst_is_account"]),
    ]
    cols = ["src_account", "dst_account", "transaction_id", "amount", "timestamp", "category"]
    for from_tbl, to_tbl, mask in combos:
        subset = txns_df.loc[mask, cols]
        if subset.empty:
            continue
        tmp_path = os.path.join(DATA_DIR, f"_tmp_txn_{from_tbl}_{to_tbl}.csv")
        subset.to_csv(tmp_path, index=False)
        conn.execute(
            f'COPY TRANSACTION FROM "{tmp_path.replace(chr(92), "/")}" (HEADER=true, FROM="{from_tbl}", TO="{to_tbl}")'
        )
        os.remove(tmp_path)
        print(f"  loaded {len(subset)} {from_tbl} -> {to_tbl} transactions")

    print("Done. Database ready at", DB_PATH)


if __name__ == "__main__":
    main()