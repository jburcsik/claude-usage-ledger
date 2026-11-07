# Design

## Problem

One Claude account is metered as one pool across Claude Code, web, desktop and
mobile, and `/usage` in Claude Code shows the account-wide limit bars on any
device. What is missing is attribution and history: which machine and which
project burned the week, and what last month looked like once the local
transcripts have been pruned.
Anthropic closed the request for native unified statistics as not planned
(https://github.com/anthropics/claude-code/issues/15434), so this is built
locally.

## Chosen approach

Git-synced per-device JSON. Rejected alternatives:

| Option | Rejected because |
|---|---|
| OpenTelemetry to a metrics store with a Grafana front end | Several services to run, every machine must reach the store, and it records nothing retroactively: history starts the day it is switched on. |
| Cron to a hosted database plus a web dashboard | A hosted dependency and more code for the same answer. |

Git wins on two counts: no infrastructure, and the transcripts already on disk
mean the first run yields months of back history instead of zero.

## Division of labour

| Concern | Owner | Why |
|---|---|---|
| Pricing and cost per model-day | `ccusage` | It tracks a maintained pricing table. A hand-rolled price map would drift on every model launch. |
| Per-agent, per-model tokens | `ccusage daily --json --by-agent` | Also covers the other agent CLIs that ccusage reads, each with its own log format. |
| Per-project tokens | this repo | ccusage groups by date, month, week, session and billing block, but has no project breakdown. |

So `models[]` in each day file comes from ccusage and carries cost;
`projects[]` comes from the transcript scan and carries tokens only.

## Why projects carry no cost

Cost is linear in four separately priced token types. Splitting a model-day's
ccusage cost across projects would need those four rates, which means bringing
back the pricing table this design delegates. Token share answers "where did
my tokens go?" at least as well, and on a subscription the dollar is notional.

## Transcript dedupe, the one real trap

One API response is written to the transcript as several `assistant` records,
one per content block (text, thinking, each tool call), and every one repeats
the full `usage` object. Naive summing roughly doubles every number. In the
test fixture, five records collapse to two real requests.

The records of one response share `requestId` and `message.id`, so that pair
is the unit of real usage. The seen-set is global across all files, which also
folds away the history that a resumed session copies into a new transcript.
Records with no `message.id` fall back to their `uuid`.

## Conflict model

Each device writes only `data/<device>/`, so two devices never touch the same
file and content conflicts are structurally impossible. The only contention is
the branch ref, handled by `git pull --rebase --autostash` and three retries in
`sync.sh`.

`collected_at` is excluded from the change comparison, so an unchanged day is
not rewritten and settled history produces no commits. Transcript pruning can
remove part of a day's history, so a recount can come up short; a saved day's
token total is never lowered.

## Timezone

`timezone` in `claude-usage.toml` (default `UTC`) is passed to ccusage as `-z`
and used to bucket transcript timestamps. It must be one value on every
device, or a laptop in another zone would file the same work under a different
day. It is deliberately not the machine's local timezone.

## Schema

`DayRecord` is versioned (`schema: 1`) and validated on read, so a device
running an older copy of the script fails loudly instead of reporting wrong
totals. Validation happens only at that boundary; internal code trusts itself.

## Known gaps

1. claude.ai web, desktop and mobile are invisible. They log nothing locally.
   The report states this rather than implying the total is complete.
2. Agents other than Claude Code get token and model attribution but no project
   attribution, because only Claude Code records `cwd`. The report warns when
   the project total drifts from the Claude Code total by more than
   `drift_warn`.
3. Cost is API-equivalent, not billed.
