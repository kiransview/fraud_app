"""Data-source tools for the two subagents.

Two of these are now grounded in real, public reference data instead of
fabricated numbers (see `data/reference/`):

- The amount-risk signal is looked up against real percentile/fraud-rate
  statistics computed from the ULB "Credit Card Fraud Detection" dataset
  (284,807 real anonymized card transactions, 492 confirmed frauds) --
  `data/reference/amount_baseline.json`.
- The sanctions screen does real fuzzy-name matching against the actual,
  current US Treasury OFAC SDN list (~19,000 entries), downloaded from
  `sanctionslistservice.ofac.treas.gov` -- `data/reference/ofac_sdn.csv`.
  The jurisdiction check and structuring/CTR thresholds are grounded in the
  policy document at `data/reference/compliance_policy.md` (real FATF
  grey/black lists and real BSA/CTR regulation citations).

The remaining signals -- recent transaction velocity, device fingerprint
history, IP reputation, and the linked-account graph -- are still
deterministic simulations (hashed from the input, not looked up anywhere
real). No public dataset can legitimately provide real per-account device
or session history; a real deployment sources these from its own
device-intelligence vendor, session logs, and entity graph. Swap the
`_xxx` function bodies below for those real integrations when you have
them -- the return shape is the contract the subagents rely on, so nothing
else needs to change.
"""
from __future__ import annotations

import csv
import difflib
import hashlib
import json
from functools import lru_cache
from pathlib import Path

from langchain_core.tools import tool

_REFERENCE_DIR = Path(__file__).resolve().parent.parent / "data" / "reference"


def _stable_fraction(*parts: str) -> float:
    """Deterministic pseudo-random float in [0, 1), derived from the inputs.
    Only used by the still-simulated signals below (velocity, device, IP,
    network) -- the amount and sanctions checks use real reference data."""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


# ---------------------------------------------------------- risk analysis --
def _txn_velocity_lookup(account_id: str) -> dict:
    """SIMULATED: recent transaction frequency and recency for this account."""
    f = _stable_fraction(account_id, "velocity")
    return {
        "txns_last_1h": int(f * 6),
        "txns_last_24h": int(f * 40) + 1,
        "minutes_since_last_txn": int((1 - f) * 600) + 1,
    }


def _geo_distance_check(account_id: str, geo: str, home_geo: str) -> dict:
    """SIMULATED: distance and travel plausibility between the account's
    home location and the transaction's origin, given time since the last
    session."""
    f = _stable_fraction(account_id, geo)
    distance_km = int(f * 12000)
    hours_since_last_session = round(1 + f * 20, 1)
    max_plausible_kmh = 900  # commercial flight speed, generous upper bound
    implausible_speed = distance_km / max(hours_since_last_session, 0.1) > max_plausible_kmh
    return {
        "distance_km": distance_km,
        "hours_since_last_session": hours_since_last_session,
        "impossible_travel": bool(implausible_speed and geo != home_geo),
    }


def _device_fingerprint_lookup(device_id: str, account_id: str) -> dict:
    """SIMULATED: whether this device has been seen before on this account,
    and how many other accounts it's associated with."""
    f = _stable_fraction(device_id, account_id)
    known = f > 0.55
    return {
        "known_device": known,
        "first_seen_days_ago": None if not known else int(f * 400),
        "seen_on_other_accounts": 0 if known else int((1 - f) * 4),
    }


def _ip_reputation(ip_address: str) -> dict:
    """SIMULATED: reputation signal for an IP address: proxy/VPN/datacenter
    flags and abuse history."""
    f = _stable_fraction(ip_address, "iprep")
    return {
        "is_proxy_or_vpn": f > 0.8,
        "is_datacenter": f > 0.9,
        "abuse_reports_90d": int(f * 15),
    }


def _shared_attribute_graph_query(device_id: str, ip_address: str) -> dict:
    """SIMULATED: other accounts sharing this device or IP, and how many
    are already flagged."""
    f = _stable_fraction(device_id, ip_address, "graph")
    linked = int(f * 5)
    flagged = int(linked * f)
    return {"linked_accounts": linked, "linked_flagged_accounts": flagged}


@lru_cache(maxsize=1)
def _load_amount_baseline() -> dict:
    with open(_REFERENCE_DIR / "amount_baseline.json", encoding="utf-8") as f:
        return json.load(f)


def _amount_risk_profile(amount: float) -> dict:
    """REAL: how this amount compares to actual observed transaction amounts
    and fraud rates in the ULB Credit Card Fraud Detection dataset (see
    module docstring). Not a per-account baseline -- no public dataset can
    legitimately provide that -- but a real population-level reference
    instead of an invented "typical spend" number."""
    baseline = _load_amount_baseline()
    percentile_rank = 0.0
    for p_str, threshold in sorted(baseline["amount_percentiles"].items(), key=lambda kv: float(kv[0])):
        if amount >= threshold:
            percentile_rank = float(p_str)
    bucket = next(
        (
            b for b in baseline["fraud_rate_by_amount_bucket"]
            if amount >= b["min"] and (b["max"] is None or amount < b["max"])
        ),
        None,
    )
    return {
        "percentile_rank_in_reference_dataset": percentile_rank,
        "reference_dataset_mean_amount": baseline["mean_amount"],
        "observed_fraud_rate_for_this_amount_range": bucket["fraud_rate"] if bucket else None,
        "observed_overall_fraud_rate": baseline["overall_fraud_rate"],
        "reference_dataset_size": baseline["n_transactions"],
    }


@tool
def gather_risk_evidence(
    account_id: str, amount: float, category: str, geo: str, home_geo: str,
    device_id: str, ip_address: str,
) -> dict:
    """Gather all transaction-risk evidence for this case in one call: how
    this amount compares to a real reference dataset of fraudulent and
    legitimate transactions, recent activity, travel plausibility between
    home and origin location, device/IP reputation, and linked-account
    signals."""
    return {
        "amount_vs_real_reference_data": _amount_risk_profile(amount),
        "recent_activity": _txn_velocity_lookup(account_id),
        "geo_check": _geo_distance_check(account_id, geo, home_geo),
        "device": _device_fingerprint_lookup(device_id, account_id),
        "ip_reputation": _ip_reputation(ip_address),
        "linked_entities": _shared_attribute_graph_query(device_id, ip_address),
    }


# ------------------------------------------------------------------ compliance --
# ISO 3166-1 alpha-2 codes for the countries named in compliance_policy.md's
# FATF lists (June 2026 update) -- kept in sync with that document.
_FATF_BLACKLIST = {"IR": "Iran", "KP": "North Korea", "MM": "Myanmar"}
_FATF_GREYLIST = {
    "AO": "Angola", "BO": "Bolivia", "BA": "Bosnia and Herzegovina", "BG": "Bulgaria",
    "CM": "Cameroon", "CI": "Cote d'Ivoire", "CD": "Democratic Republic of Congo",
    "HT": "Haiti", "IQ": "Iraq", "KE": "Kenya", "KW": "Kuwait", "LA": "Laos",
    "LB": "Lebanon", "MC": "Monaco", "NP": "Nepal", "PG": "Papua New Guinea",
    "SS": "South Sudan", "SY": "Syria", "VE": "Venezuela", "VN": "Vietnam",
    "VG": "Virgin Islands (UK)", "YE": "Yemen",
}


@lru_cache(maxsize=1)
def _load_sdn_list() -> tuple[list[str], dict[str, dict]]:
    """Parses data/reference/ofac_sdn.csv (the real Treasury SDN export,
    columns: ent_num, SDN_Name, SDN_Type, Program, ...). Returns the
    upper-cased names (for fuzzy matching) and a lookup back to program/id."""
    names: list[str] = []
    by_name: dict[str, dict] = {}
    with open(_REFERENCE_DIR / "ofac_sdn.csv", encoding="utf-8", errors="replace", newline="") as f:
        for row in csv.reader(f):
            if len(row) < 4:
                continue
            ent_num, name, program = row[0].strip(), row[1].strip(), row[3].strip()
            if not name:
                continue
            name_norm = name.upper()
            names.append(name_norm)
            by_name[name_norm] = {"ent_num": ent_num, "program": None if program == "-0-" else program}
    return names, by_name


def _ofac_sanctions_check(name: str) -> dict:
    """REAL: fuzzy-match `name` against the actual, current OFAC SDN list
    (~19,000 real sanctioned individuals/entities/vessels), not a hash."""
    names, by_name = _load_sdn_list()
    query = (name or "").strip().upper()
    if not query:
        return {"candidate_match": False, "match_confidence": 0.0, "matched_name": None, "program": None}

    close = difflib.get_close_matches(query, names, n=1, cutoff=0.72)
    if not close:
        return {"candidate_match": False, "match_confidence": 0.0, "matched_name": None, "program": None}

    best = close[0]
    ratio = round(difflib.SequenceMatcher(None, query, best).ratio(), 2)
    info = by_name[best]
    return {
        "candidate_match": True,
        "match_confidence": ratio,
        "matched_name": best,
        "program": info["program"],
    }


def _jurisdiction_check(geo: str) -> dict:
    """REAL (per compliance_policy.md): checks the trailing token of a
    "City, XX" geo string against the current FATF grey/black lists."""
    token = (geo or "").rsplit(",", 1)[-1].strip().upper()
    if token in _FATF_BLACKLIST:
        return {"list": "blacklist", "jurisdiction": _FATF_BLACKLIST[token]}
    if token in _FATF_GREYLIST:
        return {"list": "greylist", "jurisdiction": _FATF_GREYLIST[token]}
    return {"list": None, "jurisdiction": None}


def _business_rules_engine(amount: float) -> dict:
    """Structuring and CTR thresholds per compliance_policy.md section 2 --
    real Bank Secrecy Act figures (31 CFR 1010.311, 31 U.S.C. Sec. 5324),
    not invented ones."""
    return {
        "round_dollar_flag": amount % 100 == 0 and amount >= 500,
        "structuring_flag": 9000 <= amount < 10000,
    }


@tool
def gather_compliance_evidence(name: str, amount: float, geo: str) -> dict:
    """Gather all compliance evidence for this case in one call: real OFAC
    sanctions-list screening, a real FATF high-risk-jurisdiction check on
    the transaction's origin, and structuring/CTR business-rule checks."""
    return {
        "sanctions_screen": _ofac_sanctions_check(name),
        "jurisdiction_check": _jurisdiction_check(geo),
        "rules": _business_rules_engine(amount),
    }


# ------------------------------------------------------------------ citations --
# The functions below build a "where did this evidence actually come from"
# list for the UI, computed the same deterministic way the evidence itself
# was -- not written by the LLM. An LLM can misquote or invent a citation;
# calling the real underlying lookups again here (cheap, since they're
# either a dict lookup or already-cached data) can't.

def explain_risk_evidence(
    account_id: str, amount: float, category: str, geo: str, home_geo: str,
    device_id: str, ip_address: str,
) -> list[dict]:
    baseline = _load_amount_baseline()
    profile = _amount_risk_profile(amount)
    fraud_rate = profile["observed_fraud_rate_for_this_amount_range"]
    fraud_rate_str = f"{fraud_rate * 100:.3f}%" if fraud_rate is not None else "n/a"
    return [
        {
            "label": "Amount vs. a real fraud dataset",
            "source": baseline["source"],
            "excerpt": (
                f"This ${amount:,.2f} transaction sits at the "
                f"{profile['percentile_rank_in_reference_dataset']:g}th percentile of "
                f"real transaction amounts in the reference dataset "
                f"({profile['reference_dataset_size']:,} real transactions). Observed "
                f"fraud rate for this amount range: {fraud_rate_str} "
                f"(dataset-wide rate: {profile['observed_overall_fraud_rate'] * 100:.3f}%)."
            ),
            "path": "data/reference/amount_baseline.json",
        },
        {
            "label": "Recent activity, device, IP, network signals",
            "source": "Simulated -- not from a real data source",
            "excerpt": (
                "These four signals are deterministic simulations for this demo "
                "(hashed from the input, not looked up anywhere real) -- no public "
                "dataset can legitimately expose real per-account device or session "
                "history. Swap fraud_agent/tools.py's _txn_velocity_lookup, "
                "_geo_distance_check, _device_fingerprint_lookup, _ip_reputation, and "
                "_shared_attribute_graph_query for a real vendor/DB integration."
            ),
            "path": "fraud_agent/tools.py",
        },
    ]


def explain_compliance_evidence(name: str, amount: float, geo: str) -> list[dict]:
    sanctions = _ofac_sanctions_check(name)
    jurisdiction = _jurisdiction_check(geo)
    rules = _business_rules_engine(amount)

    if sanctions["candidate_match"]:
        sanctions_excerpt = (
            f"Candidate match: \"{sanctions['matched_name']}\" "
            f"(confidence {sanctions['match_confidence']:.2f}), sanctions program: "
            f"{sanctions['program'] or 'unspecified'}."
        )
    else:
        sanctions_excerpt = f"No candidate match for \"{name}\" against the list."

    if jurisdiction["list"]:
        list_name = "black list" if jurisdiction["list"] == "blacklist" else "grey list"
        jurisdiction_excerpt = (
            f"\"{geo}\" resolves to {jurisdiction['jurisdiction']}, which is on the "
            f"FATF {list_name} per policy section 3."
        )
    else:
        jurisdiction_excerpt = f"\"{geo}\" is not on either FATF list (policy section 3)."

    rule_bits = []
    if rules["structuring_flag"]:
        rule_bits.append(
            "amount falls in the $9,000-$9,999.99 range, which policy section 2 "
            "(31 U.S.C. Sec. 5324) treats as a structuring indicator"
        )
    if rules["round_dollar_flag"]:
        rule_bits.append("round-dollar amount >= $500 (policy section 2, secondary indicator)")
    rules_excerpt = "; ".join(rule_bits) + "." if rule_bits else "No structuring or round-dollar flags (policy section 2)."

    return [
        {
            "label": "OFAC sanctions screen",
            "source": "US Treasury OFAC SDN list (~19,000 entries, sanctionslistservice.ofac.treas.gov)",
            "excerpt": sanctions_excerpt,
            "path": "data/reference/ofac_sdn.csv",
        },
        {
            "label": "FATF jurisdiction check",
            "source": "compliance_policy.md, section 3 (FATF grey/black lists, June 2026 update)",
            "excerpt": jurisdiction_excerpt,
            "path": "data/reference/compliance_policy.md",
        },
        {
            "label": "Structuring / CTR rules",
            "source": "compliance_policy.md, section 2 (31 CFR 1010.311, 31 U.S.C. Sec. 5324)",
            "excerpt": rules_excerpt,
            "path": "data/reference/compliance_policy.md",
        },
    ]
