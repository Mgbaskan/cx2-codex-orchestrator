# Routing Model

CX2 employs a deterministic, repository-aware risk engine (Risk Engine v2) to evaluate prompt intent, complexity, and mutation risk before dispatching turns to Codex.

Routing is strictly rule-based and model-free: no language model or network calls are made during classification.

## Signal Architecture

Risk Engine v2 calculates a deterministic risk score from distinct signal classes:

```text
lexical_complexity
+ repository_risk
+ task_scope_risk
+ mutation_risk
+ sensitive_surface_risk
- routine_reductions
= final_risk_score
```

### 1. Lexical Complexity Signals
- **Critical Concurrency & Deadlocks** (+4, Dominance Rule): Concurrency bugs, race conditions, lock contention, thread-safety issues, and distributed transaction consistency bugs.
- **Structural Architecture & Major Refactoring** (+3): Architectural redesign, system design, major multi-module refactoring.
- **Root Cause Analysis** (+2): Diagnostic bug triage and root-cause investigations.
- **Analysis & Flow Inspection** (+2): Flow explanations, architecture inspections, security audits, authentication flow analysis.
- **Domain Context** (+1 each, bounded to max +3): Contextual domain words (auth, security, migration, production, deployment, microservices). Domain words alone do not force high tiers.

### 2. Task Scope & Task-Shape Signals
- **Broad Scope** (+3): Monorepo-wide refactors, cross-service tasks, whole-codebase operations.
- **Composite Broad Project Audit** (+4): Whole-project audits, complete codebase inspection, and repository-wide defect/vulnerability hunting across Turkish and English phrasing.
- **Multi-Surface Implementation** (+4): Coordinated implementations across multiple subprojects (e.g. mobile, backend, web, desktop).
- **Plan & Code Reconciliation** (+3): Tasks requiring code implementation to align with external plan/spec documents.
- **Verification Matrix** (+3): Tasks specifying multi-command or multi-surface quality requirements.
- **Targeted Scope**: Explicit single-file or single-component tasks receive bounded scope attribution.

### 3. Sensitive Surface & Mutation Risk
Evaluated only when positive write intent is detected:
- **Production / Rollback DB Migration Mutation** (+5): Production database migrations, schema migration rollbacks.
- **Standard DB Migration Mutation** (+3): Database/schema migrations, alter table operations.
- **Infrastructure & Deployment Mutation** (+3): Kubernetes manifests, production deployment configs, CI/CD pipeline mutations.
- **Auth & Token Logic Mutation** (+2): Refresh token logic, token rotation, JWT signing, password hashing.
- **Secret & Credential Handling** (+2): Secret rotation, credential management, API key rotation.
- **Dependency & Lockfile Rewrites** (+1): Package manifest upgrades and lockfile rewrites.

### 4. Repository Context Signals
- **Monorepo Structure** (+1): Detected via `pnpm-workspace.yaml`, `turbo.json`, `nx.json`, `lerna.json`.
- **Repository Size**: Tracked file count buckets (<200: +0, 200–1999: +1, 2000+: +2 bounded).
- **Large Dirty Tree** (+1): Working tree with >=20 dirty files.

### 5. Routine / Low-Risk Reductions
- Routine UI styling, CSS/Tailwind, margins, padding, button colors, typos, labels, documentation, and comments reduce risk by up to -3 points.

### 6. Dominance Rules
- **Critical Concurrency Dominance**: If critical concurrency or deadlock signals are detected, the score is guaranteed to meet or exceed `deep_min` (7), regardless of routine keyword presence.
- **Broad Project Audit Dominance**: If broad-scope audit or whole-codebase defect hunting is detected (without routine reductions), the task is guaranteed to meet or exceed `deep_min` (7) and route to `deep` tier.
- **Multi-Surface Task-Shape Dominance**: Tasks coordinating implementation changes across 3+ surfaces meet or exceed `deep_min` (7) and route to `deep` tier.
- **Routine Scope Capping**: Single-file UI or documentation modifications cannot be forced into `deep` merely by the presence of a domain keyword (e.g. "Change the Authentication button color").

## Task Tiers

| Tier       | Typical use                                                      | Reasoning | Typical sandbox               |
| ---------- | ---------------------------------------------------------------- | --------- | ----------------------------- |
| `routine`  | Inspection, explanations, simple UI styling, typo fixes          | `low`     | `read-only` or task-dependent |
| `standard` | Bug fixes, features, standard refactors                          | `medium`  | task-dependent                |
| `deep`     | Architecture, concurrency, security, complex root cause analysis | `high`    | task-dependent                |

## Write Intent Separation

Task complexity and workspace mutation permissions remain strictly separated:
- Prompts with explicit negative instructions (e.g., `do not modify any files`, `read-only`, `sadece oku`, `dosyalarda değişiklik yapma`) always route to `read-only` mode (`mutating: false`).
- A complex architecture explanation can be `deep` or `standard` while remaining `read-only`.
- A button color change can be `routine` while requiring `workspace-write`.
