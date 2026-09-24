# Interview question bank

Ask at most four questions per round, in one AskUserQuestion call. Every question quotes the person's own number
and offers a "keep it as it is" option. Pick the questions whose findings are largest; skip the rest.

| Finding (in findings.json) | Question | Options |
|---|---|---|
| `definition_of_done.push.tested_final_version` well below `n` | "Between <low%> and <high%> of your <n> pushes shipped a version a passing test had run on; the usual gap is <top reason>. Require tests on the final version before a push?" | Yes, and tell the agent what to run (fix) / Only before merging / Keep it as it is |
| `definition_of_done.<step>.why_not.switched_version` large | "<N> <steps> shipped a version checked out or pulled after the tests ran (usually main, just before deploying). Run the tests on that version first?" | Yes / No, CI covers it (label your CI check) / Keep it as it is |
| `definition_of_done.<step>.why_not.result_hidden` large | "<N> test runs had their result hidden (piped into tail with no summary). Tell the agent to keep the result visible?" | Yes (a require_before with passed: true does this) / Keep it as it is |
| `unplaced` has entries | "These commands recur but I could not place them: <top three with counts>. What do they do?" (multiSelect per command, or one question per command) | Run tests or checks / Deploy or release / Build / Something else, leave it |
| `definition_of_done.<step>.exceptions` > 0 | "Your agents ran <checks> before <N of M> <steps>. Make that a rule?" | Always (tell the agent) / Only on main / Warn in the replay only / Keep it as it is |
| `risky.<kind>.ran` > 0 | "Which of these should wait for a person?" (multiSelect, with counts) | Deploy (N) / Publish (N) / Merge a pull request (N) / Database changes (N) |
| a hold rule is chosen | "How should approval work for <steps>?" | Once per task, for an hour (Recommended) / Every time / Only production (a command pattern) |
| rehearsal `budget.over` | "These rules would ask you about <N> times a week (budget <B>). Which way to ask less?" | One approval per task / Hold only production / Tell the agent instead of asking / Raise the budget |
| `after_checks.deploy` missed | "<N of M> deploys had no check within 30 minutes. What counts as checking?" | Load the live site / Run the smoke tests / Both / Keep it as it is |
| `risky.force_push` > 0 | "There were <N> force-pushes. Refuse them on main?" | Refuse on main and master / Refuse everywhere / Keep it as it is |
| `secrets.commands` > 0 | "<N> commands carried a literal secret. Refuse commands that do?" | Yes, refuse / Warn only / Keep it as it is. Also recommend rotating those secrets. |
| `friction.refused` high | "<N> calls were refused, most often <pattern>. Should that kind of call be allowed, or stay refused?" | Allow it / Keep refusing / Ask each time |
| `rework.fix_and_rerun_loops` high | "<N> fix-and-rerun loops. Should the agent run the relevant tests before asking for review?" | Yes / Keep it as it is |
| Always, last | "What must someone else show before you accept their work (a contractor, another team, another company's agent)?" (multiSelect) | Tests passed on the final version / A named person reviewed it / The exact version you approved is what shipped / Sources or evidence attached |
| The person mentions a client or someone else approving | "Should <who> approve <steps> themselves, on your standard's page?" | Yes, on the page / No, I approve / Keep it as it is |

## After the answers

- Restate each answer as one plain-English sentence; that sentence becomes the rule's `says`.
- Pick the rule type from the answer: "wait for a person" is `hold`; "never" is `block`; "must have happened first"
  is `require_before` (with `"if_missing": "fix"` unless they want to look themselves); "must follow" is
  `require_after`; "at most" is `limit`; anything judgement-based is `unsupported`.
- Label commands before writing rules that depend on them, then rescan.
- Rehearse before anything is switched on, and show the cost (asks a week, fixes a week) next to what the rule would
  have caught.
