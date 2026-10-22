"""Read a test runner's own summary from a command's output, for when the exit status cannot say.

`npm test 2>&1 | tail -20` exits with tail's status, so the log shows success even when tests failed. The summary
line the runner printed ("Tests  2 failed | 40 passed", "=== 3 passed in 1.2s ===", "# fail 0") still says what
happened. Only summary lines are read: a failure anywhere wins, and nothing is inferred from other text.
"""
import re

_FAIL = [
    r"Test Files\s+[1-9]\d*\s+failed",                        # vitest
    r"^\s*Tests?:?\s+[1-9]\d*\s+failed",                       # vitest, jest
    r"Test Suites:\s+[1-9]\d*\s+failed",                       # jest
    r"^\s*[1-9]\d*\s+failing\b",                               # mocha
    r"^#\s*fail\s+[1-9]\d*",                                   # node:test (TAP)
    r"^\S*\s*fail\s+[1-9]\d*\s*$",                             # node:test (spec reporter: "ℹ fail 2")
    r"^=+ .*\b[1-9]\d*\s+(?:failed|errors?)\b.* in [\d.]+m?s",  # pytest
    r"^FAILED \((?:failures|errors)=\d+",                      # unittest
    r"^(?:--- )?FAIL\b",                                       # go test
    r"test result: FAILED\.",                                  # cargo test
    r"^\s*[1-9]\d*\s+failed\b",                                # playwright
    r"\b\d+ examples?, [1-9]\d* failures?\b",                  # rspec
    r"^FAILURES!",                                             # phpunit
    r"^FAILED \| \d+ passed \| [1-9]\d* failed",               # deno
    r"^\s*[1-9]\d*\s+fail\b",                                  # bun
    r"npm (?:ERR!|error) (?:Test failed|.*[Ll]ifecycle script .* failed)",
    r"ELIFECYCLE.*(?:[Tt]est failed|Command failed)",
    r"^error Command failed with exit code [1-9]",             # yarn
    r"ERR_PNPM_RECURSIVE_RUN_FIRST_FAIL|ERR_PNPM_\w*FAIL",     # pnpm
    r"^not ok \d+",                                           # TAP
    r"#\s*fail\s+[1-9]\d*",                                   # TAP summary printed inline
    r"^Node\.js v\d+\.\d+\.\d+\s*$",                            # node's banner after an uncaught error
    r"(?:^|\s)exit(?:_?code)?\s*[=:]\s*[1-9]\d*\b",              # the command printing its own exit status
]
_PASS = [
    r"Test Files\s+\d+\s+passed",
    r"^\s*Tests?:?\s+\d+\s+passed",
    r"^\s*\d+\s+passing\b",
    r"^#\s*fail\s+0\s*$",
    r"^\S*\s*fail\s+0\s*$",
    r"^=+ .*\b\d+\s+passed\b.* in [\d.]+m?s",
    r"Ran \d+ tests? in [\d.]+s\s+OK\b",
    r"^ok\s+\S+\s+[\d.]+s",
    r"^PASS$",
    r"test result: ok\.",
    r"^\s*\d+\s+passed\b",
    r"\b\d+ examples?, 0 failures\b",
    r"^OK \(\d+ tests?",
    r"^ok \| \d+ passed \| 0 failed",
    r"^\s*\d+\s+pass\s*$",
    r"#\s*pass\s+\d+\s*#\s*fail\s+0\b",
    r"(?:^|\s)exit(?:_?code)?\s*[=:]\s*0\b",
]
_FAIL_RX = re.compile("|".join("(?:%s)" % p for p in _FAIL), re.M)
_PASS_RX = re.compile("|".join("(?:%s)" % p for p in _PASS), re.M)
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def test_verdict(output):
    """'fail', 'pass' or None (no runner summary found) for the output of a test run."""
    if not isinstance(output, str) or not output:
        return None
    text = output
    if "\\n" in text and text.count("\n") < 2:
        text = text.replace("\\r", "").replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')
    text = _ANSI.sub("", text[-200000:])
    if _FAIL_RX.search(text):
        return "fail"
    if _PASS_RX.search(text):
        return "pass"
    return None
