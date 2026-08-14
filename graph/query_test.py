"""
Quick sanity-check queries against the loaded KuzuDB graph.
Run: python graph/query_test.py
"""

import kuzu
import os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mule_graph_db")


def main():
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(f"No database at {DB_PATH}. Run graph/build_graph_db.py first.")

    db = kuzu.Database(DB_PATH)
    conn = kuzu.Connection(db)

    print("--- Node/edge counts ---")
    print("Accounts:", conn.execute("MATCH (a:Account) RETURN count(a)").get_next()[0])
    print("External entities:", conn.execute("MATCH (e:ExternalEntity) RETURN count(e)").get_next()[0])
    print("Transactions:", conn.execute("MATCH ()-[t:TRANSACTION]->() RETURN count(t)").get_next()[0])

    print("\n--- Sample: 5 largest account-to-account transactions ---")
    result = conn.execute("""
        MATCH (a:Account)-[t:TRANSACTION]->(b:Account)
        RETURN a.account_id, b.account_id, t.amount, t.category
        ORDER BY t.amount DESC LIMIT 5
    """)
    while result.has_next():
        print(result.get_next())

    print("\n--- Sample: accounts with the most outgoing transactions (potential fan-out) ---")
    result = conn.execute("""
        MATCH (a:Account)-[t:TRANSACTION]->()
        RETURN a.account_id, count(t) AS out_degree
        ORDER BY out_degree DESC LIMIT 5
    """)
    while result.has_next():
        print(result.get_next())

    print("\nAll queries ran successfully -- database is populated and queryable.")


if __name__ == "__main__":
    main()