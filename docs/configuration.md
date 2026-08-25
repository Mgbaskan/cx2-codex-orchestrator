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
    "turn_idle_timeout_sec": {
      "routine": 300,
      "standard": 450,
      "deep": 600
    },
    "turn_hard_timeout_sec": {
      "routine": 1800,
      "standard": 2700,
      "deep": 3600
    }
  }
}
```

## Route-Aware Turn Timeouts

CX2 resolves two independent monotonic limits per tier:

- **Idle timeout**: no meaningful turn progress. Defaults are `routine`: 300s, `standard`: 450s, and `deep`: 600s; values are clamped to `[30, 1800]` seconds.
- **Hard timeout**: absolute active runtime even when progress continues. Defaults are `routine`: 1800s, `standard`: 2700s, and `deep`: 3600s; values are clamped to `[60, 7200]` seconds.
- Human approval wait is excluded from hard-runtime charging. A currently active command suppresses idle timeout while remaining subject to the hard timeout.
- `execution.turn_idle_timeout_sec` takes precedence when present. If it is absent, legacy `execution.turn_timeout_sec` remains accepted as the idle override. The hard timeout always resolves independently from `execution.turn_hard_timeout_sec` or its safe default.
- If configuration resolves `hard < idle`, CX2 deterministically raises hard to idle. No configuration produces an unlimited turn.
- On either timeout, CX2 performs a final event drain, requests `turn/interrupt` once, and allows a short bounded terminal reconciliation period before raising a typed idle/hard timeout.
