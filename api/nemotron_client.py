"""
Nemotron Explanation Client
=============================
Turns raw detector evidence (rule-engine strings like
"fan_out_smurfing: received funds as one of 8 rapid recipients...") into a
short, investigator-facing narrative via NVIDIA NIM's Nemotron API.

Requires an environment variable NVIDIA_API_KEY -- never hardcode the key
in source. Set it per-session with:
    $env:NVIDIA_API_KEY = "nvapi-..."          (PowerShell)
or persist it (recommended) via System Properties > Environment Variables,
so you don't have to re-set it every terminal session.

Model: defaults to nvidia/nemotron-3-super-120b-a12b (the one from your
screenshot, confirmed working). Override with env var NEMOTRON_MODEL if you
want to test a smaller/faster variant for demo latency -- check
build.nvidia.com's model browser for exact slugs rather than guessing one.
"""

import os
from openai import OpenAI

API_KEY = os.environ.get("NVIDIA_API_KEY")
MODEL = os.environ.get("NEMOTRON_MODEL", "nvidia/nemotron-3-super-120b-a12b")

_client = None


def get_client():
    global _client
    if API_KEY is None:
        raise RuntimeError(
            "NVIDIA_API_KEY environment variable is not set. "
            "Run: $env:NVIDIA_API_KEY = \"your-key-here\" in PowerShell before starting the API."
        )
    if _client is None:
        _client = OpenAI(base_url="https://integrate.api.nvidia.com/v1", api_key=API_KEY)
    return _client


PROMPT_TEMPLATE = """You are a senior AML (Anti-Money Laundering) investigator at a bank, writing \
a case note for a colleague who will decide whether to act on this alert. Use the automated \
detection evidence below. Write like a real analyst, not a system paraphrasing its own logs --
name the actual money-laundering typology in FATF/FinCEN terms where the evidence supports it \
(e.g. "structuring", "layering", "smurfing", "rapid funds transfer/pass-through") rather than \
just restating detector labels.

Account: {account_id}
Risk score: {risk_score}/100
Triggered patterns: {patterns}
Raw evidence: {evidence}

Write exactly three things, plainly labeled:

1. SEVERITY: One of CRITICAL / HIGH / MEDIUM, based on the risk score and number of \
corroborating patterns (CRITICAL: score 80+ with 2+ patterns; HIGH: score 50-79 or a single \
strong pattern like confirmed cash-out; MEDIUM: below that). State it as just the word.

2. CASE SUMMARY: 2-3 sentences. Name the likely laundering typology, state the approximate \
dollar amounts involved if given in the evidence, and explain WHY this pattern is suspicious \
(not just what happened) -- e.g. why rapid pass-through defeats manual transaction review, or \
why fan-in from unrelated senders followed by cash-out resembles a mule collection point.

3. RECOMMENDED ACTION: Vary this by severity and pattern -- do not default to "freeze the \
account" every time. Consider the full range a real investigator would: file a SAR (Suspicious \
Activity Report) and freeze for CRITICAL cases with clear typology matches; enhanced monitoring \
plus KYC re-verification for HIGH; passive monitoring with a review flag for MEDIUM or \
single-signal cases. Pick the one that actually fits this account's evidence.

Be factual and grounded only in the evidence given. Do not invent transaction details, dollar \
figures, or dates not present in the evidence."""


def explain_account(account_id: str, risk_score: int, patterns: str, evidence: str,
                     max_retries: int = 3) -> str:
    """Calls Nemotron and returns its explanation text. Retries transient
    failures (404s from model instances scaling, timeouts, rate-limit
    hiccups) with backoff before giving up -- NVIDIA's hosted models can
    briefly return errors while an instance is being provisioned; a single
    hard failure on that is not a real reason to give up on this account.
    Raises only after exhausting retries -- callers should still catch and
    handle that (e.g. fall back to raw evidence in the UI)."""
    import time
    client = get_client()
    prompt = PROMPT_TEMPLATE.format(
        account_id=account_id, risk_score=risk_score,
        patterns=patterns.replace(";", ", "), evidence=evidence
    )
    last_error = None
    for attempt in range(max_retries):
        try:
            completion = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=700,
                stream=False,
                extra_body={
                    "chat_template_kwargs": {"enable_thinking": False},
                    "reasoning_budget": 0,
                },
            )
            raw = completion.choices[0].message.content
            return _strip_reasoning_leak(raw)
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt * 2)  # 2s, 4s, 8s backoff
    raise last_error


def _strip_reasoning_leak(text: str) -> str:
    """Safety net: if reasoning still leaks into content despite the flags
    above, cut everything before the actual "1. SEVERITY:" marker so the
    dashboard never shows raw chain-of-thought to an investigator."""
    marker = "1. SEVERITY:"
    idx = text.find(marker)
    if idx > 0:
        return text[idx:]
    return text


if __name__ == "__main__":
    # Quick manual test -- run: python api/nemotron_client.py
    # (requires NVIDIA_API_KEY set first)
    result = explain_account(
        account_id="ACC000412",
        risk_score=100,
        patterns="fan_out_smurfing;layering_chain;rapid_passthrough",
        evidence=("fan_out_smurfing: received funds as one of 8 rapid recipients from a single "
                  "source within 300min | rapid_passthrough: received 8407, forwarded 8004 within "
                  "2h | layering_chain: part of 4-hop rapid P2P transfer chain"),
    )
    print(result)