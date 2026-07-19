"""
Example: Generate keys with different TPS tiers and subscription durations.

This script creates virtual keys with:
- Different tokens-per-second limits per tier
- Auto-expiring subscriptions (duration-based)
- Budget limits per tier

Prerequisites:
    1. Start the proxy: litellm --config proxy_server_config_throttled.yaml
    2. Set LITELLM_MASTER_KEY in environment
    3. Run this script: python throttled_streaming_example.py
"""

import os
import requests
import time

PROXY_URL = os.getenv("LITELLM_PROXY_URL", "http://localhost:4000")
MASTER_KEY = os.getenv("LITELLM_MASTER_KEY", "sk-master-key")

HEADERS = {
    "Authorization": f"Bearer {MASTER_KEY}",
    "Content-Type": "application/json",
}

# Subscription tiers with TPS limits and durations
TIERS = {
    "basic_monthly": {
        "tps_limit": 10,
        "tpm_limit": 600,       # 10 tps * 60 seconds
        "rpm_limit": 3,
        "max_budget": 5.0,      # $5/month
        "budget_duration": "30d",
        "duration": "30d",      # Key expires after 30 days
        "description": "10 tokens/second, 1 month",
    },
    "basic_weekly": {
        "tps_limit": 10,
        "tpm_limit": 600,
        "rpm_limit": 3,
        "max_budget": 2.0,
        "budget_duration": "7d",
        "duration": "7d",       # Key expires after 7 days
        "description": "10 tokens/second, 1 week",
    },
    "pro_monthly": {
        "tps_limit": 25,
        "tpm_limit": 1500,
        "rpm_limit": 10,
        "max_budget": 20.0,
        "budget_duration": "30d",
        "duration": "30d",
        "description": "25 tokens/second, 1 month",
    },
    "pro_quarterly": {
        "tps_limit": 25,
        "tpm_limit": 1500,
        "rpm_limit": 10,
        "max_budget": 50.0,
        "budget_duration": "90d",
        "duration": "90d",      # Key expires after 90 days
        "description": "25 tokens/second, 3 months",
    },
    "enterprise_yearly": {
        "tps_limit": 100,
        "tpm_limit": 6000,
        "rpm_limit": 50,
        "max_budget": 500.0,
        "budget_duration": "365d",
        "duration": "365d",     # Key expires after 1 year
        "description": "100 tokens/second, 1 year",
    },
    "trial": {
        "tps_limit": 5,
        "tpm_limit": 300,
        "rpm_limit": 2,
        "max_budget": 0.5,
        "budget_duration": "1d",
        "duration": "1d",       # Key expires after 1 day
        "description": "5 tokens/second, 1 day trial",
    },
}


def create_key(tier_name: str, tier_config: dict, user_id: str) -> dict:
    """Create a virtual key for a specific subscription tier."""
    payload = {
        "user_id": user_id,
        "metadata": {
            "tier": tier_name,
            "tps_limit": tier_config["tps_limit"],
            "max_concurrent_streams": 1,
        },
        "tpm_limit": tier_config["tpm_limit"],
        "rpm_limit": tier_config["rpm_limit"],
        "max_budget": tier_config["max_budget"],
        "budget_duration": tier_config["budget_duration"],
        "duration": tier_config["duration"],  # Auto-expire key
        "max_parallel_requests": 1,
    }

    resp = requests.post(
        f"{PROXY_URL}/key/generate",
        headers=HEADERS,
        json=payload,
    )
    resp.raise_for_status()
    return resp.json()


def check_key_info(api_key_hash: str) -> dict:
    """Check key info including expiration."""
    resp = requests.get(
        f"{PROXY_URL}/key/info",
        headers=HEADERS,
        params={"key": api_key_hash},
    )
    resp.raise_for_status()
    return resp.json()


def test_throttled_stream(api_key: str, model: str = "gpt-4o"):
    """Test a throttled streaming request."""
    print(f"\n--- Testing throttled stream ---")

    resp = requests.post(
        f"{PROXY_URL}/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [{"role": "user", "content": "Write a 100 word essay about AI."}],
            "stream": True,
            "max_tokens": 100,
        },
        stream=True,
    )

    if resp.status_code != 200:
        print(f"Error: {resp.status_code} - {resp.text}")
        return

    start = time.monotonic()
    token_count = 0

    print("Response: ", end="", flush=True)
    for line in resp.iter_lines():
        if not line:
            continue
        line = line.decode("utf-8")
        if line.startswith("data: "):
            data = line[6:]
            if data.strip() == "[DONE]":
                break
            import json
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = chunk.get("choices", [])
            if choices:
                delta = choices[0].get("delta", {})
                content = delta.get("content", "")
                if content:
                    token_count += 1
                    print(content, end="", flush=True)

    elapsed = time.monotonic() - start
    actual_tps = token_count / elapsed if elapsed > 0 else 0

    print(f"\n\n--- Stats ---")
    print(f"Tokens: {token_count}")
    print(f"Time: {elapsed:.2f}s")
    print(f"Actual TPS: {actual_tps:.1f}")


def test_expired_key(api_key: str):
    """Test that an expired key is rejected."""
    print(f"\n--- Testing expired key rejection ---")

    resp = requests.post(
        f"{PROXY_URL}/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
        },
    )

    print(f"Status: {resp.status_code}")
    if resp.status_code == 401:
        print("Key correctly rejected as expired!")
        detail = resp.json().get("detail", {})
        error = detail.get("error", {})
        print(f"Error message: {error.get('message', 'N/A')}")
    elif resp.status_code == 200:
        print("Key still active (not expired yet)")
    else:
        print(f"Unexpected response: {resp.text}")


if __name__ == "__main__":
    print("=" * 60)
    print("  LiteLLM Stream Throttling + Subscription Expiry Demo")
    print("=" * 60)

    # Show available tiers
    print("\nAvailable subscription tiers:")
    print("-" * 60)
    for name, config in TIERS.items():
        print(f"  {name:25s} | {config['description']}")
    print()

    # Create keys for each tier
    created_keys = {}
    for tier_name, tier_config in TIERS.items():
        print(f"Creating {tier_name} key ({tier_config['duration']} duration)...")
        result = create_key(tier_name, tier_config, f"user_{tier_name}")
        key = result.get("key", "")
        key_hash = result.get("token", "")
        created_keys[tier_name] = {"key": key, "hash": key_hash}
        print(f"  API Key:    {key[:25]}...")
        print(f"  Key Hash:   {key_hash[:25]}...")
        print(f"  TPS:        {tier_config['tps_limit']}")
        print(f"  Expires:    {tier_config['duration']}")
        print(f"  Max Budget: ${tier_config['max_budget']}")
        print()

    # Test with the pro tier
    print("=" * 60)
    print("Testing PRO monthly tier (25 tokens/second, 30 day expiry)...")
    test_throttled_stream(created_keys["pro_monthly"]["key"])

    # Show how to check key info
    print("\n" + "=" * 60)
    print("Key info for pro_monthly:")
    try:
        info = check_key_info(created_keys["pro_monthly"]["hash"])
        key_data = info.get("info", {})
        print(f"  Expires: {key_data.get('expires', 'N/A')}")
        print(f"  Spend:   ${key_data.get('spend', 0):.4f}")
        print(f"  Budget:  ${key_data.get('max_budget', 'N/A')}")
    except Exception as e:
        print(f"  (Could not fetch key info: {e})")

    print("\n" + "=" * 60)
    print("To test with a trial key (1 day expiry):")
    print(f"  Use key: {created_keys['trial']['key'][:25]}...")
    print("  It will expire in 24 hours.")
    print()
    print("To manually check expiry:")
    print(f"  curl '{PROXY_URL}/key/info?key={created_keys['trial']['hash']}' \\")
    print(f"    -H 'Authorization: Bearer {MASTER_KEY}'")
