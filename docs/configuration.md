# Configuration & Policy

CX2 uses a central JSON policy located at `~/.cx/policy.json`.

## Configuration Schema

```json
{
  "version": 1,
  "models": {
    "routine": ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"],
    "standard": ["gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna"],
    "deep": ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]
  },
  "reasoning": {
    "routine": "low",
    "standard": "medium",
    "deep": "high"
  },
  "thresholds": {
    "routine_max": 1,
    "deep_min": 7
  },
  "budget": {
    "enabled": true,
    "thresholds": {
      "conserve_at": 70,
      "critical_at": 85,
      "emergency_at": 95,
      "hard_stop_at": 100
    }
  },
  "session": {
    "enabled": true,
    "resume_ttl_minutes": 45,
    "resume_across_branch_change": false,
    "context_warn_percent": 75,
    "compaction": "native"
  }
}
```
