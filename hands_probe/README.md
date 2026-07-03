# Golem Probe Sandbox

This directory is a safe sandbox for checking whether Golem's reports match
real filesystem, test, and git state.

Rules for every probe:
- make only the requested change inside `hands_probe/` unless instructed otherwise;
- run the exact verification command;
- commit only the relevant files;
- report the commit hash and the command output summary.

Progression:
1. Micro edit with one deterministic test.
2. Small parser change with multiple tests.
3. Multi-file feature with docs and tests.
4. Intentional failing-test diagnosis before fix.
5. Controlled refactor with no behavior change.
