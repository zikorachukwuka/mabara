---
name: mabara-testing
description: How to write tests that earn trust in any repo, any stack. Load BEFORE writing or updating tests.
---

# Writing tests that earn trust

The point of a test is a regression fence: it fails when the behavior it
pins breaks, and at no other time. Everything below serves that.

## Discovery first — never assume the stack

1. Find the runner before writing anything: test config (pytest.ini,
   pyproject, jest config, package.json scripts, Cargo.toml), the test
   directory, and two or three existing test files.
2. Mirror what you find — naming, file placement, fixture style,
   assertion idioms. A repo with `test_foo.py` files does not get
   `foo.spec.ts` conventions, and vice versa.
3. NEVER introduce a new test framework or assertion library into
   someone's repo. If there is genuinely no harness, propose the
   smallest standard one for the stack out loud, and wait for a yes.

## What to test

- Test BEHAVIOR through the public surface, not implementation details.
  A good test survives a refactor; a bad one breaks with it.
- When fixing a bug: write the failing test FIRST, watch it fail for
  the right reason, then fix. The fix's witness is the test.
- Pin the edges: the empty input, the boundary value, the case that
  produced the bug report. One happy path plus the sharp edges beats
  ten happy paths.
- Every test deterministic: no timing sleeps, no network, no ordering
  dependence between tests.

## Hygiene

- Name tests after the behavior they pin ("test_denial_words_veto"),
  never after the function ("test_check2").
- One behavior per test — a failure should point at one thing.
- Keep them fast. A suite nobody runs is a fence nobody built.

## Verify

Run the suite with the run_tests tool after writing — a test you never
saw pass (and, for bug fixes, fail first) proves nothing. Say the
result plainly out loud.
