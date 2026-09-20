# Recovery replay lesson: TASE Figure-eight route

## when-to-use

Use before retrying the first Figure-eight run or changing its writer after a
force guard, Protective Stop, stale RTDE/Kunwei frame, duplicate preparation,
failed Home, or manual request to return Home.

## prior

Reuse the mature contact-yield/TASE writer, current package/read-back, resident
session identity, video recorder, existing Home route, and sealed-attempt ledger.
Do not create a second runtime or second writer.

## observed incident

The previous Protective Stop occurred before contact. The immediate evidence
was a temporary runner using multiprocessing `spawn` without an explicit main
guard, allowing a child process to repeat preparation and collide with the
resident session. The current resolver also records a separate 4 ms live-prep
timing blocker; these are distinct failure classes.

## repair and replay

The offline recovery contract is committed as `f5cd538a`; the hard-failure
Home disposition is committed as `ee21ba5b`. Focused recovery, Home, process,
writer and provider tests pass. The new contract covers first-fault sealing,
single-writer ownership, quiescence, unlock decision, atomic rollback,
automatic Home transition and no attempt concatenation.

## promotion boundary

This lesson is offline-ready only. Live execution still requires fresh
resolver/readiness, package identity, RTDE/Kunwei freshness, video, timing
qualification, stationary Home and the current live owner gates.

## stop condition

If quiescence, identity, Home proof or timing readiness fails, mark the attempt
`BLOCKED` and preserve the first fault. Do not unlock Protective Stop or start a
new attempt from stale evidence.
