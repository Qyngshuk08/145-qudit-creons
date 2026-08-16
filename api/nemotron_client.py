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


PROMPT_TEMPLATE = """You are assisting a fraud investigator at a bank. Given the automated \
detection evidence below for one account, write a short case narrative.

Account: {account_id}
Risk score: {risk_score}/100
Triggered patterns: {patterns}
Raw evidence: {evidence}

Write exactly two things, plainly labeled:
1. SUMMARY: 2-3 sentences explaining in plain language why this account was flagged, \
suitable for an investigator with no technical background in the detection system.
2. NEXT ACTION: one concrete recommended next step (e.g. freeze account, request \
KYC re-verification, escalate to senior investigator, monitor only).

Be factual and grounded only in the evidence given. Do not invent transaction details \
not present in the evidence."""


def explain_account(account_id: str, risk_score: int, patterns: str, evidence: str) -> str:
    """Calls Nemotron once and returns its explanation text. Raises on API error --
    callers should catch and handle (e.g. fall back to raw evidence in the UI)."""
    client = get_client()
    prompt = PROMPT_TEMPLATE.format(
        account_id=account_id, risk_score=risk_score,
        patterns=patterns.replace(";", ", "), evidence=evidence
    )
    completion = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,   # low -- this is investigator-facing, want consistency not creativity
        max_tokens=700,    # was 400 -- too low, was truncating some responses mid-sentence
        stream=False,
        extra_body={
            "chat_template_kwargs": {"enable_thinking": False},
            "reasoning_budget": 0,  # belt-and-suspenders -- enable_thinking alone didn't fully suppress it
        },
    )
    raw = completion.choices[0].message.content
    return _strip_reasoning_leak(raw)


def _strip_reasoning_leak(text: str) -> str:
    """Safety net: if reasoning still leaks into content despite the flags
    above, cut everything before the actual "1. SUMMARY:" marker so the
    dashboard never shows raw chain-of-thought to an investigator."""
    marker = "1. SUMMARY:"
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