# Strict admission independent review

Status: the six requested boundaries and seven prior independent compatibility probes pass. One additional same-call source-change probe fails, so final closure is pending.

The persistent pre-API `none` policy overrides an earlier named rewrite cache; incomplete actual/cache/parallel/tools policy blocks all child launches and durable runs. A linked terminal blocks changed output/new call even with a stale named request. The same original native call replays stored result without a child when source SHA matches. A new turn with fresh named policy can execute. A simulated provider extra call after none is blocked before execution and still yields `final_policy_ignored`/unverified from the unchanged final gate.

P2 remaining finding: `hermes.py:444-468,531-550` uses fresh source/artifact verification to recognize a terminal admission state. After a successful run, changing source bytes and re-delivering the exact old native call makes `terminal()` return None, reopens entry, launches another Harness child and creates another run. In the actual probe it is rejected because the old output exists; that output is unchanged. Durable terminal closure must remain distinct from fresh proof required for returning a replay result. A stale or corrupt replay should be rejected without execution.

Evidence: six specified probes passed in 0.60 seconds; combined new+prior probes produced 13 passes and this one failure in 5.46 seconds. See `test_admission.py`, `admission-evidence-pending.json` and the owned `pytest-extra-and-compat-01` directory. Source SHA is recorded in the JSON. No model/API calls, production edits, installed live-wheel changes or daily-state writes occurred.
