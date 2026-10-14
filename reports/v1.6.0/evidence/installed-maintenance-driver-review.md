# Installed maintenance driver — static review

Scope: `scripts/v16_installed_maintenance.py` only, focused on installed invocation, independent literal truths, 105-run maintenance cursor and preservation of the old active anchor. No driver execution or suite rerun.

No P1/P2 blocker found in this bounded review.

- All 105 run records originate from separate `python -I -B -m rpe_harness run --request - --db ...` CLI subprocesses. Driver SQL connections use mode=ro and query_only, and cannot fabricate the run population.
- An isolated current directory plus removal of source PYTHONPATH and verification of site-packages under sys.prefix checks the installed-package path. This driver is not the separate whole-production-source-byte comparison required by release acceptance.
- Two manually spelled JSON byte truths preserve raw numeric/string tokens, nested structure and LF semantics. Every actual output and unchanged source is checked independently of Harness verification flags.
- It activates the first real run's recipe, then creates 104 additional real runs before draining bounded cycles. It checks discovery/recheck/host/proposal budgets, exact durable run and feedback IDs, queue completion, cursor reaching event 105, ledger hash links and stable diagnostic count.
- It asserts the original active evidence ID, pointer generation and activation count remain unchanged, independently calls installed fresh verification for all 105 artifacts, and checks a repeated idle cycle creates no new proposals or activations.

Limits: this driver covers successful JSON maintenance over more than 100 runs. Waiting-run finalization, crash recovery, hostile evidence and actual Hermes/model behavior remain separate tests. It deliberately retains its unique run_root for downstream evidence inspection/owner-directed final cleanup; it does not claim to perform final resource cleanup itself.

Verdict: approved for its stated static scope; runtime acceptance still requires executing the installed driver and inspecting its report.
