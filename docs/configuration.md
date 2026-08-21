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
  },
  "execution": {
    "turn_timeout_sec": {
      "routine": 300,
      "standard": 450,
      "deep": 600
    }
  }
}
```

## Route-Aware Turn Timeouts

The `execution.turn_timeout_sec` section configures per-tier turn execution deadlines:

- **Defaults**: `routine`: 300s (5m), `standard`: 450s (7.5m), `deep`: 600s (10m).
- **Bounds**: Values are clamped to safe bounds `[30.0, 1800.0]` seconds (30s to 30m).
- **Behavior**: If a turn reaches its deadline, CX2 dispatches a best-effort `turn/interrupt` to App Server and safely terminates the local turn with `TimeoutError`.
