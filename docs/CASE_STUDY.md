# Engineering case study — dependable agent execution

## Problem

An automated worker can finish work but lose its result when the coordinator
crashes. If recovery naively repeats the request, external side effects may be
duplicated. A plain text status file cannot prove accepted work.

## Implementation strategy

1. Write a durable intent before dispatch.
2. Bind the request to a stable operation identity and input digest.
3. Enforce a strict sequence of hash-linked journal records.
4. Record the result only after the trusted coordinator verifies completion.
5. Reconstruct completion on a fresh process; do not repeat completed work.
6. Fail closed on a missing result, mismatched input, or corrupted journal.
7. For real side effects, require an external idempotency contract or operator
   reconciliation. The journal alone cannot provide exactly-once semantics.

The public module intentionally demonstrates only local evidence primitives.
It does not claim to sandbox code or contact an external provider.

## Verification protocol

```sh
python3 -m unittest discover -s tests -v
```

| Test category | What it proves |
| --- | --- |
| Restart after recorded completion | A new process reconstructs the result |
| Crash after intent | An uncertain operation blocks replay |
| Input or result conflict | Reused IDs cannot silently change inputs/results |
| Torn/modified/deleted records | Journal corruption is detected |
| Five concurrent writer processes | A linear hash sequence survives contention |

## Limitations

This is a self-contained Linux/Python demonstration. `flock` coordinates
journal appends but is not a security boundary against an attacker who can
edit the file as the same account. Durable fsync does not guarantee survival
of all storage-device failures, and a missing receipt does not prove an
external request never ran. Full production systems need independent
attestation, OS principal separation and application-specific reconciliation.

## Further private project experience

A related private ICM engineering effort includes sandbox admission and
attestation, Git acceptance references, fault-injected SIGKILL tests, an
independent retrospective verifier and multi-stage retry recovery. A test
report from that private environment must not be confused with this repo's
current public CI results.
