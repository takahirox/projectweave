## Summary

Describe what changed.

## Outcome

Describe what changes as a result, including before/after behavior when useful.

## Validation

Describe how the change was validated.

### Pre-merge acceptance

Record applicable mandatory checks and their actual results, including any required pre-merge human acceptance still outstanding.

### Required post-merge verification

List checks needed only after merge, or state that none are required. Report unperformed, pending, or environment-blocked checks accurately. Follow the [evidence and follow-up guidance](https://github.com/takahirox/projectweave/blob/main/docs/development-flow.md#6-verify-after-merge) to record results after merge and track actual defects in new linked Issues after checking for equivalents.

## Related issues

Use `Closes #N` for a fully implemented Issue with applicable mandatory pre-merge acceptance satisfied. Pending post-merge-only verification does not make the implementation partial or require removing the closing reference. Follow the [merge and closure lifecycle](https://github.com/takahirox/projectweave/blob/main/docs/development-flow.md#5-merge-and-close-the-issue).

For intentional partial work, state the remaining scope and use a non-closing reference so the parent Issue stays open. Link other relevant Issues as needed.
