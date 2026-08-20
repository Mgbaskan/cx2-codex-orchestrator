# Routing Model

CX2 employs a deterministic rule-based classifier to evaluate prompt intent and complexity before dispatching turns to Codex.

## Task Tiers

1. **Routine (Low Complexity / Read-Only)**:
   - Typical tasks: Explanations, code search, status checks, log reviews.
   - Model: `gpt-5.6-luna`
   - Reasoning Effort: `low`
   - Sandbox: `read-only`

2. **Standard (Medium Complexity / Workspace Mutation)**:
   - Typical tasks: Bug fixes, feature additions, refactoring, unit test creation.
   - Model: `gpt-5.6-terra`
   - Reasoning Effort: `medium`
   - Sandbox: `workspace-write`

3. **Deep (High Complexity / Architecture & Security)**:
   - Typical tasks: Large migrations, concurrency debugging, root cause analysis.
   - Model: `gpt-5.6-sol`
   - Reasoning Effort: `high`
   - Sandbox: `workspace-write`

## Write Negation Awareness

Prompts containing explicit negative instructions (e.g., "do not modify any files", "just explain") are automatically routed to `read-only` mode regardless of keywords.
