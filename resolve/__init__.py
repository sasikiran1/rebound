"""Rebound Resolve: recover what a scheduling customer meant when the original request becomes impossible.

A model interprets the conversation into a typed intent (intent.py). Deterministic code validates it against the
customer's own words and SQL-backed facts, decides whether an available slot is authorized or a question is needed
(evaluate.py), stores every approved version durably (store.py) and books through the FieldDesk API (worker.py).
The SQL oracle (oracle.py) judges the final database effect against independently authored expected outcomes.
"""
