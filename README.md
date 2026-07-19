# LiteLLM AI Gateway (Fork)

Throttled AI API gateway with subscription-based access control.

## How It Works

Users get a **virtual API key** that controls:
- **TPS (Tokens Per Second)** - How fast they receive tokens
- **Duration** - When their subscription expires
- **Max Budget** - Spending limit
- **Max Parallel Requests** - Concurrent streams (default: 1)

The gateway buffers tokens from upstream providers and streams them to users at their subscribed speed.

---

## Upstream Models

| Model | Provider | Speed | Best For |
|-------|----------|-------|----------|
| `deepseek-v4-flash` | DeepSeek | Fast | Quick tasks, chat |
| `deepseek-v4-pro` | DeepSeek | Medium | Complex reasoning |
| `kimi-k2.6` | Moonshot | Fast | General use |
| `kimi-k3` | Moonshot | Medium | Advanced tasks |

---

## Subscription Tiers

| Tier | TPS | Duration |
|------|-----|----------|
| Standard | 15 | 30 days |
| Premium | 30 | 30 days |
| Ultra | 45 | 30 days |

---

## User Setup

### Create a User Key

```bash
curl -X POST http://YOUR_SERVER:4000/key/generate \
  -H "Authorization: Bearer sk-master-key" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user_123",
    "metadata": {
      "tps_limit": 15
    },
    "tpm_limit": 900,
    "rpm_limit": 5,
    "duration": "30d",
    "max_budget": 10.0,
    "budget_duration": "30d",
    "max_parallel_requests": 1
  }'
```

Response:
```json
{
  "key": "sk-abc123...",
  "expires": "2026-08-18T00:00:00Z",
  "user_id": "user_123"
}
```

### User Makes Requests

```python
import openai

client = openai.OpenAI(
    api_key="sk-abc123...",
    base_url="http://YOUR_SERVER:4000"
)

# Streams at exactly 15 tokens/second
response = client.chat.completions.create(
    model="deepseek-v4-flash",
    messages=[{"role": "user", "content": "Hello"}],
    stream=True
)
```

---

## Admin Commands

### Update User Speed (TPS)

```bash
curl -X POST http://YOUR_SERVER:4000/key/update \
  -H "Authorization: Bearer sk-master-key" \
  -H "Content-Type: application/json" \
  -d '{
    "key": "sk-abc123...",
    "metadata": {
      "tps_limit": 30
    }
  }'
```

### Extend Subscription

```bash
curl -X POST http://YOUR_SERVER:4000/key/update \
  -H "Authorization: Bearer sk-master-key" \
  -H "Content-Type: application/json" \
  -d '{
    "key": "sk-abc123...",
    "duration": "60d"
  }'
```

### Add Budget

```bash
curl -X POST http://YOUR_SERVER:4000/key/update \
  -H "Authorization: Bearer sk-master-key" \
  -H "Content-Type: application/json" \
  -d '{
    "key": "sk-abc123...",
    "max_budget": 50.0
  }'
```

### Check Key Info

```bash
curl "http://YOUR_SERVER:4000/key/info?key=sk-abc123..." \
  -H "Authorization: Bearer sk-master-key"
```

### Revoke Key

```bash
curl -X POST http://YOUR_SERVER:4000/key/delete \
  -H "Authorization: Bearer sk-master-key" \
  -H "Content-Type: application/json" \
  -d '{"keys": ["sk-abc123..."]}'
```

---

## Duration Formats

| Format | Meaning |
|--------|---------|
| `30d` | 1 month |

---

## Error Responses

### Subscription Expired
```json
{
  "error": {
    "message": "Subscription expired. Your key expired at 2026-08-18T00:00:00+00:00. Please renew your subscription.",
    "type": "subscription_expired",
    "code": "key_expired"
  }
}
```

### Concurrent Stream Limit
```json
{
  "error": {
    "message": "User user_123 already has an active stream. Wait for it to finish before sending another request.",
    "type": "throttler_error",
    "code": "concurrent_stream_limit"
  }
}
```

### Rate Limited
```json
{
  "error": {
    "message": "Rate limit reached: 900 tokens per minute",
    "type": "rate_limit_error",
    "code": "rate_limit_exceeded"
  }
}
```

---

## Quick Reference

| Parameter | Where | Description |
|-----------|-------|-------------|
| `tps_limit` | `metadata.tps_limit` | Tokens per second |
| `duration` | top-level | Key expiration |
| `max_budget` | top-level | Spend limit ($) |
| `budget_duration` | top-level | Budget reset period |
| `tpm_limit` | top-level | Tokens per minute |
| `rpm_limit` | top-level | Requests per minute |
| `max_parallel_requests` | top-level | Concurrent streams |
