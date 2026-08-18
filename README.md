# Jermes

> **Hermes learns. Jermes proves.**

An agent that learns reusable procedures from finished runs and **keeps only what
measurably works**. It forges executable tools, grades its own memory by
experiment, routes to whatever capabilities are already on the machine, and
serves what it built over MCP so any other agent can call it.

Jermes is a **standalone engine**: a plain Python package named `jermes`, with
**zero dependencies**, driven by its own CLI. Nothing in it imports a platform,
and nothing about it assumes one. Embedding it in some larger system is a
separate, later question; `absorb.py` prepares such a copy on request, and that
copy lands **inside this repo** by default.

> The discipline behind every feature: **a skill, a tool, or a memory is not
> trusted because it looks good. It is trusted because it was measured on cases
> it had never seen.**

Personal work of Jinsoo Kim, all rights reserved. The source is published to be
read and evaluated; see [LICENSE](LICENSE) before using it for anything else.

## Install

No dependencies. Python 3.11+.

```bash
pip install jermes        # or: pip install -e .   (from a clone)
jermes                    # what this machine can do right now + the next line to type
```

Building and publishing are described in [RELEASE.md](RELEASE.md). The runtime
dependency list is empty and CI fails the release if that ever stops being true.

| Need | Command |
|---|---|
| Just say what you want | `jermes ask "<your sentence>"` |
| See the gate separate good from bad | `jermes demo` (no LLM required) |
| Learn from your local agent sessions | `jermes learn` (Claude Code and Codex) |
| Turn a repeated procedure into a real tool | `jermes tool <name> --cases cases.csv` |
| Re-check a tool still works | `jermes improve <name> --check-only` (no LLM) |
| Put what it learned where other agents look | `jermes install` |
| Pick up the MCP tools already on this machine | `jermes capabilities --live` |
| Find out why it can only do so much | `jermes doctor` |
| Keep learning without being asked | `jermes watch [--interval 900]` |
| Teach or correct a fact yourself | `jermes memory --add/--retire/--supersede` |
| Undo a wrong routing example | `jermes forget <name> --task "<text>"` |
| See where a skill came from and what it did | `jermes show <name>` |
| See what one session left behind | `jermes trace <session-id>` |
| See what it judged about one fact | `jermes memory --show <id>` |
| Approve a shared-scope skill yourself | `jermes approve <name> --by <you>` |
| Go back to a past version | `jermes rollback <name> [--to <ver>]` |
| Let other agents call your tools | `jermes serve` (stdio MCP) |
| Watch it live | `python -m jermes.dashboard` → `:7396` |

An LLM is only needed for *drafting*. With `JERMES_BASE_URL` / `JERMES_MODEL`
unset, Jermes probes the usual local endpoints (Ollama, vLLM, LM Studio,
llama.cpp), **says which one it picked**, and stops with the list of places it
looked if there is none.

## Features

- **Learning from your own sessions, whichever agent you used**: `jermes learn`
  reads local sessions from Claude Code **and Codex**, merged into one
  recency-ordered list, and finds the places worth learning from (a hard success,
  a recovery after failure, a correction from you). It then **builds a replay
  bench out of that same session** so the gate has something to judge with: the
  bench is assembled from failure-and-recovery pairs, so whatever made it fail
  must not reappear and what the recovery actually did should. Scoring is by
  regex, not by an LLM judge. Adding a source is one module plus one line, and
  `JERMES_SOURCES` pins the list so a sandbox stays a sandbox no matter how many
  sources exist.
- **Verified promotion**: every skill is replayed on a **held-out split** before
  it is trusted. `promoted` requires the gain to reproduce on cases the skill was
  not written from; a dev-only gain lands in `staged` for a human, never in
  `verified`. One function (`gate.decide`) produces all three verdicts so the
  words cannot drift apart.
- **Tool forging that actually runs**: a repeated deterministic procedure
  becomes a Python script, and the script is **executed against the cases** to
  decide. Grading uses **no LLM at all**: feed input, compare output. On failure
  the error is fed back and the model rewrites, but only *dev* failures are
  shown, so the held-out set stays honest.
- **Permissions, not prohibitions**: a tool declares what it needs
  (`--policy strict|files|network|trusted`) and a human grants it. Secrets are
  withheld unless named (`--env`). What was granted travels with the package as
  MCP annotations, so the receiving side knows before it calls.
- **Tools carry their own tests**: verification cases live inside the tool, so
  `jermes improve --check-only` re-runs them anytime **without an LLM**. A broken
  tool is repaired from its own failures; **if the repair is worse than the
  original, it is discarded.**
- **What it learned comes back, and is actually used**: `jermes ask` and
  `jermes route` recall the facts relevant to your question, and the recalled
  facts go **into the model's prompt**, not just onto your screen. That
  distinction was a real bug: it printed "the default branch is develop" at
  trust 0.95 and then filled in `base: "main"` on the next line. Relevance times
  trust decides, and **a fact sharing no word with the question is not offered
  at all**: the same rule the router already applies to capabilities.
- **You can teach it directly**: `jermes memory --add "<fact>"` writes one down,
  `--retire <id>` takes a wrong one out of circulation without deleting it, and
  `--supersede <id> --add "<new fact>"` closes the old fact's validity window so
  "what was true in March?" still answers. A hand-written fact goes through the
  same law as a learned one, and lands **unmeasured**: you wrote it down, which
  is not evidence that it is true.
- **Evidence-graded memory**: trust moves only by measurement, and the
  measurement actually runs: `jermes learn` replays each fact with it present
  and with it removed, on the same bench that judges skills, and moves trust by
  the difference. Never by proxy signals like "it was retrieved". Facts carry
  **validity windows**: a superseded fact stops being recalled but is *not
  deleted*, so "what was true in March?" stays answerable. Contradictions are
  settled by the bench first, by time only when the bench cannot tell, and the
  inference is recorded as an inference.
- **Uses whatever is already here**: agentskills packages, MCP servers, and its
  own ledger collapse into one `Capability` record. Risk is **derived from the
  MCP annotation vocabulary** (`readOnlyHint`/`destructiveHint`/…) rather than a
  private grading scheme. `jermes capabilities --live` starts the stdio MCP
  servers named in your config and pulls their real `tools/list`, then keeps the
  result so later commands route over those tools without reconnecting.
  Unresolved servers are marked unresolved; a server that fails to start is
  **reported, not silently listed as empty**.
- **Reaches remote servers too**: stdio and streamable HTTP, chosen in one
  place from the config entry, so nothing else has to know which it is. Reading
  a real config found four HTTP servers that a stdio-only client had never once
  seen. Failures say what the server said (`HTTP 401 unauthenticated`), because
  a bare exception name is not something you can fix.
- **It calls them, not just finds them**: `jermes ask` will invoke a discovered
  MCP tool. Arguments come from the server's own `inputSchema`, so a tool with no
  usage history is still callable. A tool the server **declared** read-only runs
  immediately; anything else prints the exact call it would make and stops until
  you agree with `--risky`. Unannotated is treated as *unknown*, not as safe.
- **The same rule applies to its own tools**: a tool it forged is called `safe`
  only when the code is provably pure computation, not merely when no
  known-bad name appears in it. That distinction is the whole point. A blocklist
  of dangerous names is never finished, and every gap in it turns into a
  confident lie: for a while a tool using `os.open` passed a zero-permission
  policy, earned `verified`, was advertised to other agents as read-only, and
  ran without asking. Now anything that cannot be proven pure is *unknown*,
  which means caution, no read-only claim, and a prompt before it runs. Tools
  that declare their permissions are unaffected, since a declaration is
  knowledge.
- **Drops into a host as a plugin, not just a CLI, in both directions**:
  `jermes.integrations` registers verified tools and graded memory under a
  host's own extension points (setuptools `entry_points`, checked structurally
  against a `typing.Protocol`, no import of the host's package either
  direction). The moment both are installed in the same environment, a tool
  forged a second ago is callable through the host's own tool-calling path
  with no restart and no host-side code change; confirmed end to end against
  a real harness's unmodified discovery code. The reverse also holds: jermes
  registers its own extension points (`jermes.capability_sources`), so a tool
  the host already had, that jermes never forged, shows up as an ordinary
  `route`/`ask` candidate under the same risk gating as everything else,
  proved against a tool jermes had no part in creating. `jermes install` and
  `serve` keep working the same way for hosts that would rather stay outside
  this contract.
- **The permission a tool declared is enforced while it runs**, not only read off
  the source beforehand. Reading names is how the earlier holes happened: each
  time one was patched (`open(mode=)`, `Path.write_text`, a `shutil` alias,
  `os.open`, `os.startfile`) another spelling of the same operation was still
  open. A tool now runs under an audit hook, which sees the operation at the
  interpreter level no matter which name reached it, and which cannot be
  uninstalled from inside the tool. Writes outside its own directory, deletes,
  sockets, subprocesses and native calls each require the matching permission.
  Reads are never blocked - the standard library has to read itself to work, and
  reading is not what `allow_write` is about.
- **Resource limits come from the kernel, not from Python.** Everything above
  watches Python constructs, and allocating a gigabyte breaks no Python rule -
  it took 0.4 seconds and nothing stopped it. A timeout does not help, because
  memory is not time. So the limits are set where the kernel enforces them:
  `setrlimit` for address space, file size and CPU on POSIX; a Job Object for
  memory and child processes on Windows, closed with kill-on-job-close so a
  spawned process cannot outlive the run. Windows has no per-process file-size
  limit, so the parent watches the working directory and stops the tool when it
  exceeds the cap - weaker than a kernel cap, but the difference between
  "unbounded" and "a quarter second's worth" is whether the machine survives.
  The limits travel with the packaged tool, so it runs the same elsewhere.
  What is still out of scope: this does not isolate the tool from files the same
  user could already read. That needs a separate account or a container, and
  this does not pretend otherwise.
- **What it learns belongs to the project it learned it in.** A fact like "this
  repo's default branch is develop" is true here and false next door, so the
  scope comes from the working directory and recall filters on it. Facts about
  the person rather than the repo are marked global and follow you everywhere.
  The mechanism had existed for a while and was never actually used: every fact
  was written as global and recall was called without a scope, so nothing was
  ever separated. Code that exists but is never reached is worse than code that
  is missing, because the tests pass and you believe it works.
- **Trust only moves when a fact is measured against the failures it is about.**
  Measured against everything, a correct fact is averaged down to no effect,
  and no effect reads as "neutral" - so trust never moved at all. Six facts,
  six measurements, zero changes. Same root cause as the skill gate, and fixing
  one without the other left the defect half-present.
- **A skill is scored only against the failures it is about.** Otherwise a skill
  about one thing is averaged over every unrelated failure in the session and
  the result reads as "does not help" - a wrong diagnosis, since it was never
  given a chance. Relevance is computed by the same lexical router, so no model
  is involved in the verdict. When there are too few related cases the verdict is
  `staged` with that stated as the reason, and the material is pooled across
  recent sessions so a recurring failure accumulates enough cases to judge.
- **`verified` means more than one held-out case**: with four cases the holdout
  is a single case, and one lucky case is not evidence. Below the threshold the
  verdict is `staged` and the command says how many cases would settle it.
- **It keeps going until the question is answered**: one tool rarely finishes a
  real request. After each step `jermes ask` reads the actual result and decides
  what is next, so two tools chain from a single sentence. What matters is what
  it refuses to do: it can only pick a name that is actually on the list (a
  hallucinated tool stops the run and says so), a step that is not read-only asks
  you first and right then rather than through some blanket consent taken in
  advance, and it always terminates. Repeating the same call with the same
  arguments is treated as spinning and stops immediately, before the step cap.
  A failed step is not the end: the input and the error go into the next
  decision, which is exactly how it recovered from a wrong first pick in the
  measurement below.
- **It learns from its own work, not only from other agents' sessions**: what
  you asked it to do becomes learning material like any other session. Success
  alone teaches nothing, though. Whether the answer was right is not something
  it knows; what it knows is that something failed and then worked, so only
  failure-recovery pairs count. Otherwise it would be citing itself.
- **Learns without being asked**: `jermes watch` picks up sessions that
  finished since last time. Three rules keep automation from becoming an
  accident: never learn the same session twice (recorded on disk), stop at the
  budget instead of quietly continuing, and take only a few per round. It calls
  the same `learn` path, so automatic learning is never held to a looser
  standard than manual learning.
- **One command tells you what is missing**: `jermes doctor` checks the LLM
  endpoint, session sources, ledger, memory, constitution, MCP servers, and
  whether verification is possible at all, and prints the next line to type for
  each thing that is not ready.
- **Routing without a model call**: a task string ranks the capabilities and
  only the top few enter the context. Zero LLM calls, zero embedding server.
  Ranking is lexical **plus two signals nobody else has**: whether the capability
  was *verified*, and what it has *actually handled before*.
- **Nothing enters the context unlabeled**: every rendered block carries
  verified/unverified and the evidence behind it. Blocks are built in exactly one
  place, because that place is a security boundary: content arrives from traces
  and model output and must not be able to close its own tag.
- **Serves what it built**: `jermes serve` is a stdio MCP server. Only
  **verified** tools are exposed by default, annotations come from the granted
  permissions (a caller cannot widen them), and the verification evidence rides
  along in the description.
- **Every zero has a reason**. "learned nothing" is broken into signals=0,
  prefix mismatch, too few cases, or the model returning an empty list. A silent
  zero is a bug, not a result.

## Measured

Numbers come from `experiments/`, run against **real corpora, not questions I
wrote**: 190 real user questions from a production agent platform (ground truth
= the workflow that actually
served them) and 1,824 real GitLab merge requests (ground truth = the repo they
landed in).

### Tool vs. document skill vs. raw LLM (E7)

| Procedure | Raw LLM | Document skill | **Forged tool** |
|---|---|---|---|
| Easy · string assembly | 20/20 · 6.6s · 720 tok | 20/20 · 6.9s · 1380 tok | **20/20 · 1.4s · 0 tok** |
| Hard · discount + VAT + rounding | 5/20 | 6/20 | **20/20** |

Writing the procedure down does not help when the model cannot do the
arithmetic. Executing it does. Forge cost: 4.2s once; break-even around 8 uses.
*This gain is limited to deterministic procedures; judgment stays a skill.*

### Routing on real data (E9)

The reality of that corpus first: **user workflows have no descriptions** (the 8 that do are
all templates). So today a human, and a router, must choose from the name alone.

| Real user questions → workflow (same 48-item holdout) | top-1 | top-5 | macro | no answer |
|---|---|---|---|---|
| Name only (what that platform has today) | 16.7% | 47.9% | 28.6% | 9 |
| **+ what it actually handled before** | **72.9%** | **97.9%** | **92.9%** | **0** |

Node composition, measured over all 190, changed nothing: 15.3% / 48.4%, the
same as name-only.

| MR title → repo (365-item holdout, 15 repos) | top-1 | top-5 |
|---|---|---|
| Name only | 6.3% | 10.7% |
| **Past MR titles as the description** | **55.6%** | **95.3%** |

Always guessing the most common repo scores 16.4%. **Name-only does worse than
that.** Node composition did not help: it is not domain vocabulary. What
mattered was *what the capability has actually done*.

Korean queries needed one more thing. Syllable bigrams survive particles
(`배포를` / `배포하기` still share `배포`) but **not conjugation**: `더한다`
tokenizes to `더한`/`한다` and `더해줘` to `더해`/`해줘`, sharing nothing, so
"40 이랑 2 더해줘" scored exactly 0.000 against a tool described as
"a 와 b 를 더한다". Emitting single syllables as well recovers the stem. It is
only the **first** syllable is emitted, because Korean stems come first and it
is the endings that change. Emitting every syllable scored higher on the weak
baseline (top-5 62.6%) but let a particle win: an unrelated tool ranked first
on `겹침 를`. A number on a configuration nobody ships is not worth breaking
"irrelevant scores zero" for. The shipped version moves name-only top-5 from
43.8% to 48.4% and MR top-5 from 93.2% to 95.3%, and leaves the strong
condition untouched at 97.9%.

### Finding an English tool with a Korean sentence

Real MCP servers describe their tools in English. Ask in another language and
lexical overlap is zero, so nothing matches no matter how good the ranking is.
`jermes capabilities --translate` adds a one-line hint per capability, cached,
and the hint goes in as an **example, never overwriting the server's own
description**. Eight Korean questions against 23 real tools on this machine:

| | top-1 | top-3 |
|---|---|---|
| Description as the server wrote it | 1/8 | 4/8 |
| **+ one translated hint line** | **3/8** | **6/8** |

Small sample, and it is a retrieval aid rather than evidence about the tool:
the hint never sets `verified`. The one-line answer also goes through the fast
completer, because reasoning does not earn its keep on a one-line answer. That
took the whole pass from minutes to 23 seconds for 23 capabilities.

### Self-improvement actually closes (E11)

Real questions streamed one at a time from a cold start. Each step: choose →
score → *then* record.

| Cumulative | Not learning | **Learning** | Δ |
|---|---|---|---|
| 38 | 5.3% | **71.1%** | +65.8pp |
| 76 | 31.6% | **89.5%** | +57.9pp |
| 190 (total) | 44.2% | **88.9%** | **2.0×** |

Both arms saw the same questions in the same order, so the first-bucket gap is
not explained by ordering. *The control also rises later, which is data order,
not learning, which is why the metric is same-point difference, not slope.*

### What was taken from a competitor's retriever, and what was thrown away (E12)

| Piece | Real questions top-1 | MR top-1 | MR top-5 |
|---|---|---|---|
| As-is | 66.7% | 55.3% | 93.7% |
| + particle stripping | 66.7% | 55.6% | 93.7% |
| **+ BM25 saturation** | **72.9%** | **57.0%** | **95.6%** |
| Both | 72.9% | 55.9% | 95.6% |

BM25 saturation was kept. **Particle stripping was deleted outright**: no gain,
and *worse* combined with BM25 (57.0% → 55.9%), because the syllable bigrams
already absorb particle variants and stripping double-counts the same word.
Sophisticated elsewhere does not mean useful here, and dead code behind a flag is
exactly the accretion this project avoids.

### Weight (E13)

| | |
|---|---|
| Dependencies to install | **0** (standard library only) |
| Code | 6.3k lines / 23 modules |
| Import | 105 ms |
| Routing query | 0.08 ms @10 caps · 1.6 ms @200 · 10.4 ms @1000 |
| Tool run / verify 12 cases | 59 ms / 649 ms |

LLM calls per operation: routing **0**, tool verification **0**, tool regression
**0**, memory recall **0**. What is spent is reported (`LLM 호출 1회 · 토큰 265 ·
$0.0398`) and can be capped. Forging a tool costs 1–3 calls *once*. The expensive
one is skill benching (cases × 2). That is the price of the guarantee.

Measuring this found a defect: the router was re-tokenizing the whole catalogue
on **every** query (38 ms at 1000 capabilities). Tokenizing once at index time
made queries 3.7–5.8× faster. An inverted index would take 10 ms to under 1 ms,
but next to a 500 ms model call that is optimizing the wrong thing.

### Next to Hermes Agent

Hermes Agent (NousResearch) is the closest thing to this idea in the wild, and
it is a much larger product: a desktop app, a gateway, plugins, dozens of
integrations. The rows below were read out of its source, not its README, at
`893792c99344` (2026-08-09). Here is where the two actually differ for someone using
them.

| From a user's seat | Hermes Agent | Jermes |
|---|---|---|
| Learns skills from your sessions | yes | yes |
| **Can it tell you a skill works?** | no such check exists | held-out replay, and the verdict is printed |
| What ages a skill out | `activity_count` (use / view / patch) and `last_used_at`, plus an LLM review pass | measured gain on cases it never saw |
| A repeated procedure becomes | a document the model must re-read every time | a script that is **executed against cases** before it is trusted |
| Re-check later without a model | not offered | `jermes improve --check-only`, zero LLM calls |
| Install weight | 32 pinned runtime dependencies | **0** |
| Codebase you are trusting | 3,991 Python files | 26 files, 7,331 lines |
| Cold import | (app-scale startup) | 140 ms |

Where Hermes is ahead: breadth. It ships OAuth for MCP, a desktop GUI, a plugin
ecosystem, and far more surface than this does. If you want a finished assistant,
that is the mature choice.

The single thing it does not do is the thing this is built around. Hermes
promotes a skill and then keeps it because it was *used*; being retrieved often
is not evidence that it helped. Jermes replays the run with and without the
skill on a held-out split and prints the number. On a real local session that
gate rejected all four drafted skills:

```
rejected safe-file-edit-verify: dev 0.333->0.250 (-0.083)
         holdout 0.500->0.333 (-0.167); no dev gain
```

Four skills that would have been kept elsewhere. That is the whole argument.

## How it works

```
RunTrace
   │
   ├─ remember()   lessons + refined memory only (raw tool output never hardens into memory)
   ├─ signals()    complex-success / recovery / user-correction detection
   ├─ draft()      LLM draft (3-layer recovery + ensemble); no fallback on failure
   ├─ curate()     dedupe · safety · patch-over-create
   ├─ gate()       constitution → held-out verification → promoted | staged | rejected
   ├─ reconcile()  contradiction → bench decides → loser is disputed, never deleted
   └─ recall()     verified skills + relevant memory, carried forward with labels
                   (`jermes ask` and `jermes route` call this; a fact with no
                    word in common with the question is not offered at all)
```

```python
from jermes import JermesAgent, ForgeGate, InMemorySkillLedger

agent = JermesAgent(InMemorySkillLedger(), ForgeGate(my_bench_runner))
report = agent.cycle(trace, bench_cases=cases, memory_score=my_memory_runner)
print(report.summary())
# run=r1 | 신호 3 · 초안 2 | 스킬 검증 1 / 대기 1 / 거절 0 | 기억 +2 · 측정 2(↑1 ↓0) | 모순 0 → 판정 0 · 보류 0
```

### One concept, one place

The rule that keeps this from turning into accretion: **the same idea is decided
in exactly one location.**

| Concept | The only place | Everything else |
|---|---|---|
| Anything usable | `discovery.Capability` | adapters convert into it |
| Held-out split | `gate.split_holdout` | `tools.split_cases` delegates |
| The three verdicts | `gate.decide` | skills and tools measure differently, decide identically |
| Prompt blocks | `Capability.render` / `render_all` | agent · router · recall only choose *what* |
| Verification cases | `ToolCase.from_dict` / `read_cases` | file or manifest, same reading |
| Relevance | `router.relevance` | memory recall uses it too |

`tests/test_core_invariants.py` enforces this **structurally**. It fails if a
module splits the holdout on its own, builds a prompt block by hand, constructs a
`ToolCase` directly, or reads the clock inside the core.

Setting that rule up surfaced three live bugs: the skill gate degenerating to a
**zero-item holdout 10% of the time** (so it could never promote), every MCP tool
being graded `dangerous` (so none were usable under the default policy), and
memory recall ignoring the task entirely (so the same five items were injected no
matter what was being done).

### Observation

The dashboard has a box you type a task into. It shows not just what was chosen
but **why**: the overlapping words, the verification status, the evidence, and a
warning when the evidence is thin.

```
Capability      Score  Why                          Risk    Evidence
business-day    0.98   overlap 날짜를 · verified     safe    dev 9/9 · holdout 3/3
                       ⚠ thin: only 25% of the task explained
```

A skill list alone cannot answer "why wasn't this one called?". That
question is what uncovered E3's defect, where the catalogue dropped descriptions
and verified skills never reached the model. The query is side-effect free, so
the dashboard stays read-only.

`collect()` (the function behind `/api/state`) is plain data, not HTML — a
host embedding jermes can call it directly and render its own view instead of
iframing this one. It carries two axes a skill list alone does not: what a
tool is actually **permitted** to do (`ToolPolicy.granted()`, not just
"verified"), and the memory loop's own self-report — how many facts were
never measured versus measured and found uninformative versus actually moved
trust, the same three-way split the gate uses for skills, because a bare
count of "measured" hides which of those it means.

## Modules

| Module | Role |
|---|---|
| `signals` `curator` `synthesis` | detect → screen → synthesize (guide / config / tool) |
| `gate` | held-out split, the three verdicts, constitution enforcement |
| `ledger` `recall` | semver · provenance · lineage ledger, outcome feedback |
| `bench` | deterministic replay bench (regex scoring, **zero LLM-judge**) |
| `memory` | measured trust · decay · contradiction · validity windows |
| `constitution` | `never_learn` enforced by the gate for skills **and** memory; the agent cannot edit its own law |
| `portable` | agentskills.io `SKILL.md` import/export with evidence attached |
| `tools` | forging · execution · `ToolPolicy` · regression · repair · packaging |
| `discovery` | capability discovery (skill dirs · MCP · ledger), MCP annotation vocabulary |
| `mcp_client` | speaks MCP to other people's servers: stdio and HTTP, list and call |
| `sources` | one `RunTrace` out of Claude Code or Codex sessions; add a module to add a source |
| `router` | pre-selects capabilities for a task; verified + track record as signals |
| `agent` | one cycle over all of it |
| `drafter` `host` | LLM seam (with failover), spine adapter |
| `cli` · `dashboard` · `mcp_server` | user-facing surfaces (kept out of any embedded copy) |

## Interop

Agent Skills is an open standard with wide adoption, but **its validator checks
frontmatter syntax and naming, not efficacy.** That empty seat is
where the held-out gate sits.

- **Export**: a verified skill becomes a spec-compliant `SKILL.md`; the evidence
  (held-out gain, source runs, ledger status) rides in the spec's own `metadata`,
  so the standard is not broken. Tools additionally ship `scripts/tool.py`, which
  runs on its own with no Jermes present.
- **Import**: someone else's `SKILL.md` becomes a candidate. Their `verified`
  flag is **recorded and disbelieved**: the claim is kept for audit and this
  environment's bench decides. Always lands `staged`.
- **Serve**: `jermes serve` speaks stdio MCP. Anything that already has an MCP
  client needs no Jermes-specific code at all.

## Local use, verified

Everything below was run as a user would, through the installed `jermes` command
in a separate process, against a throwaway home. `smoke.py` is that walkthrough
and it is part of the repo, so the claim is re-checkable rather than a promise.

```
python smoke.py          # 22 checks, no LLM needed
python smoke.py --llm    # 24 checks, including tool forging and ask
```

| Checked | Result |
|---|---|
| bare `jermes`, `demo`, `list`, `law`, `memory`, `capabilities`, `sessions` | pass |
| `tool --script` with CSV, JSONL and JSON cases | pass, all three promoted |
| `run`, `show`, `improve --check-only` | pass, no LLM |
| `route` picks the right tool for a sentence | pass |
| `export` produces a package whose `scripts/tool.py` runs standalone | pass |
| `install` writes it where agents look, and it is discovered again | pass |
| `import` refuses to overwrite a verified record | pass |
| `serve` answers real MCP `tools/list` and `tools/call` | pass, returned 42 |
| `tool` with an LLM writing the script | pass, promoted |
| `ask` end to end from one sentence | pass, answered in 63 ms |

Two defects surfaced here that the unit tests could not see, because they live in
the wiring rather than the logic:

**Importing your own export erased its verification.** Import always lands
unverified, so re-importing an exported skill overwrote a verified `tool` with an
unverified `guide`. Import now refuses a name that already exists and offers
`--as <name>` or an explicit `--replace`.

**A tool built without `--task` could never be found.** Its description was just
its own name, so `ask` and `route` would never match it. The command now says so
while forging, instead of leaving a tool that exists but cannot be reached.

## Operating

```bash
python -m pytest                  # 446 tests
python smoke.py [--llm]           # the same features through the real CLI, in a temp home
python demo.py                    # offline end-to-end, no LLM
python audit_dead_paths.py        # code that runs but does nothing
python audit_live_paths.py --llm  # functions no real user flow ever reaches
python -m jermes.dashboard        # observation dashboard, loopback :7396
```

The experiment harness behind the **Measured** section is not published. It runs
against private corpora (a company GitLab and an internal agent platform) that
are not mine to release, so the numbers are reported here and the raw data stays
where it belongs. Everything else above runs on a clean checkout.

### Two auditors, because tests do not catch this

A command can exit 0, print numbers, and do nothing. That happened here:
`jermes learn` reported verdicts while the scorer it handed the gate was
`lambda case, skill: 0.0` and the case list was empty. Nothing failed, so
nothing said so.

`audit_dead_paths.py` reads the source: functions nobody calls, constant-returning
stubs passed to real machinery, flags parsed but never read, exceptions swallowed
in silence. `audit_live_paths.py` does the opposite. It runs **only real user
flows**, with tests excluded, and counts functions no flow ever entered. Test
coverage cannot find this class, because a test calling a function directly makes
it look alive.

Both refuse to be satisfied by an empty list. Every exemption carries a written
reason keyed by `file:function`, so "nothing left" means someone looked at each
one, not that the detector stopped looking. Both have been wrong and been fixed:
the live one once reported seven perfectly healthy functions as dead because a
decorated function's first line is the decorator, not the `def`.
