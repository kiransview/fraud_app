# Sentinel Compliance Policy (reference document for the Compliance agent)

This is the policy text the Compliance subagent is given in its system
prompt (see `fraud_agent/subagents.py`) so its reasoning can cite a specific
rule instead of an unexplained number. It's short by design -- the whole
document is injected into the prompt directly rather than retrieved via a
vector store, since it easily fits in context.

## 1. Sanctions screening (OFAC)

Every transaction's counterparty name must be screened against the US
Treasury Office of Foreign Assets Control (OFAC) Specially Designated
Nationals and Blocked Persons (SDN) list, per 31 CFR Chapter V. A candidate
match (name similarity above the matching threshold) requires the
transaction to be blocked pending manual review; a confirmed match must be
rejected and reported to OFAC.

Screening data source: `data/reference/ofac_sdn.csv`, the real, current SDN
list downloaded from the Treasury's public sanctions list service
(`sanctionslistservice.ofac.treas.gov`) -- roughly 19,000 sanctioned
individuals, entities, and vessels. Matching is fuzzy-name matching
(Python's `difflib`) against the real list, not a synthetic check.

## 2. Structuring and Currency Transaction Reports (BSA)

Per 31 CFR 1010.311, a Currency Transaction Report (CTR) is required for
transactions over $10,000. Per 31 U.S.C. § 5324, deliberately structuring
transactions to fall under that threshold to evade reporting is a federal
crime. In practice this shows up as transactions clustered just below
$10,000 -- **the $9,000-$9,999.99 range is treated as a structuring
indicator** and should raise the compliance score materially, even with no
sanctions hit.

Round-dollar amounts ($500, $1,000, $5,000, ...) at or above $500 are a
secondary, weaker indicator worth noting but not, by themselves, a strong
signal.

## 3. High-risk jurisdictions (FATF)

The Financial Action Task Force (FATF) maintains two lists of jurisdictions
with strategic AML/CFT deficiencies. A transaction whose origin or
counterparty is in one of these jurisdictions should raise the compliance
score; a match against the black list should be treated as more severe than
a match against the grey list.

**Grey list -- Jurisdictions Under Increased Monitoring** (as of the June
2026 FATF update): Angola, Bolivia, Bosnia and Herzegovina, Bulgaria,
Cameroon, Cote d'Ivoire, Democratic Republic of Congo, Haiti, Iraq, Kenya,
Kuwait, Laos, Lebanon, Monaco, Nepal, Papua New Guinea, South Sudan, Syria,
Venezuela, Vietnam, Virgin Islands (UK), Yemen.

**Black list -- High-Risk Jurisdictions Subject to a Call for Action**: Iran,
North Korea, Myanmar. A transaction touching one of these three should be
treated as a severe compliance concern on its own.

## 4. What this policy does not cover

This is a starting policy for a demo pipeline, not a full AML program. It
does not cover PEP (politically exposed person) screening, beneficial
ownership checks, or transaction-monitoring typologies beyond structuring.
Extend this document -- and the Compliance agent's prompt that reads it --
before relying on it for anything real.
