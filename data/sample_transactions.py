"""Sample cases -- the same scenarios shown in the dashboard mockup -- for
local testing without a real transaction feed."""

SAMPLE_CASES = [
    {
        # Cross-border card-not-present: new device, impossible-travel geo,
        # amount well above baseline -> expected to land in the escalate band.
        "transaction": {
            "id": "TXN-88213-7745",
            "amount": 2340.00,
            "currency": "USD",
            "merchant": "Aurelia Electronics",
            "category": "electronics",
            "channel": "ecommerce",
            "geo": "Lagos, NG",
            "home_geo": "Columbus, OH",
            "device_id": "dev-9a21",
            "device_new": True,
            "ip_address": "197.210.54.12",
            "new_payee": False,
        },
        "customer_profile": {
            "account_id": "acct-4417",
            "name": "J. Whitfield",
            "prior_flag_count": 0,
        },
    },
    {
        # Outbound wire to a newly added payee -- expected to surface a
        # network/mule-link finding and escalate or decline depending on it.
        "transaction": {
            "id": "TXN-88213-7802",
            "amount": 18500.00,
            "currency": "USD",
            "merchant": "Meridian Wire Services",
            "category": "wire",
            "channel": "wire_transfer",
            "geo": "New York, NY",
            "home_geo": "New York, NY",
            "device_id": "dev-1120",
            "device_new": False,
            "ip_address": "74.125.21.9",
            "new_payee": True,
        },
        "customer_profile": {
            "account_id": "acct-2290",
            "name": "Meridian Wire Services LLC",
            "prior_flag_count": 0,
        },
    },
    {
        # Small, routine purchase on a known device -- expected to take the
        # supervisor's fast-path (velocity + behavioral only) and auto-approve.
        "transaction": {
            "id": "TXN-88212-9931",
            "amount": 42.18,
            "currency": "USD",
            "merchant": "Northgate Pharmacy",
            "category": "grocery",
            "channel": "card_present",
            "geo": "Columbus, OH",
            "home_geo": "Columbus, OH",
            "device_id": "dev-9a21",
            "device_new": False,
            "ip_address": "68.45.12.3",
            "new_payee": False,
        },
        "customer_profile": {
            "account_id": "acct-4417",
            "name": "J. Whitfield",
            "prior_flag_count": 0,
        },
    },
]
