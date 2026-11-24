You also have `run`: a sandbox where a revision's code executes, with its
dependencies installed and no network. Execution settles what reading cannot:
- run the tests that cover the path; a test that fails at `head` and passes at `base`
  shows the change caused the failure;
- or add a short reproduction through `files` (a test that drives the failing scenario
  through the real entry point) and run it at both revisions.
Read the output before you rely on it: a failure for an unrelated reason (an import
error, a missing fixture) proves nothing. A run that demonstrates your conclusion goes
in `witnesses` by its id. A passing run refutes only what it actually exercised.
