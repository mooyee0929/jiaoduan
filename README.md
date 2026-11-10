# jiaoduan

A screenshot-free interface for AI agents to other people's web pages and macOS apps, where
every action reports what actually happened and how strong the evidence is.

GUIs are drawn for eyes. An agent driving one by screenshot pays a thousand-plus tokens per
look, a round trip per step, coordinates that drift, and one more screenshot to learn whether
the click worked. What an agent needs is smaller: the current state, what it can do, a way to
do it, and whether it took effect. jiaoduan sits between the agent and the UI and gives it
exactly that, as text, for any page or app that exposes accessibility information.

```
press button "Add another" [e12]: delivered | evidence tree | settled 180ms | net POST /draft 200
+ group "Employment 2" [e40]
  + textbox "Employer" [e41] [required]
rebound (re-rendered, same element): e12
```

> Status: v0.1 in development.

The name is the jiaoduan (角端), a beast of Chinese legend said to understand the languages of
every people and to know what is happening far away, and to bring word of it truthfully. This
project speaks to web pages and native apps in one vocabulary, and tells the agent what its
actions really did.

## What is different

| | Typical tools | jiaoduan |
| --- | --- | --- |
| Did it work? | "page changed" | `expect` postconditions and an evidence level: none, tree, driver (the widget's own state), network (the app's request returned 2xx) |
| After an action | read the whole page again | a scoped diff, built on refs that survive re-renders |
| Forms | one call per click and keystroke | `form` lists every field with its kind; `fill` writes them all, waits, and reads every one back |
| Repeated flows | site-specific code or hosted caches | data-only adapters (TOML) over semantic locators; no code runs |
| Native apps | a separate tool | the same nodes, refs and results through the macOS Accessibility API |
| Several agents | assume they own the machine | per-surface claims, per-site locks, one queue of commits waiting for a person |

## Honest limits

- **Reading a value back is not the same as the server having it.** That is why results carry
  an evidence level instead of a "verified" flag. When only weak evidence exists, the result
  says so.
- **The commit gate is defence in depth, not a guarantee.** It stops presses on submit-like
  buttons (by name in several languages, `type=submit`, a window's default button), Return
  that would submit, and form submissions nobody approved. A custom button that posts on
  click under an innocent name can still get through.
- **Approval happens outside the model's tools**: `jiaoduan gate approve` asks for a `y`
  on the controlling terminal, which an agent's shell tool does not have. Any process running
  as you can still reach the daemon's socket, so give agents the layer's MCP tools, not a
  shell, when that matters (the orchestrator does this).
- **"Any app or site" means any with an accessibility tree.** Canvas-drawn UIs and games get
  `NO_ACCESSIBLE_TREE`, not a pretend success.
- **No CAPTCHA solving, no bot-detection evasion, no logging in for you.** A challenge page
  just shows up in `observe` like any other page, for a person to handle.
- **Platforms**: web on macOS and Linux (the browser runs over a pipe, which Windows cannot
  pass to a child process from Python); native apps on macOS.

## Quick start

```bash
uv sync --extra mac          # drop --extra mac on Linux
claude mcp add jiaoduan -- uv --directory "$PWD" run jiaoduan-mcp
```

The MCP server starts a background daemon on first use. The daemon owns one Chrome (headless
by default) on a dedicated profile; Chrome refuses remote control of your everyday profile.
To use a site that needs a login, sign in once in that profile:

```bash
uv run jiaoduan login https://example.com
```

When an agent hits a commit, it reports a gate id and stops. Review and approve it in your own
terminal:

```bash
uv run jiaoduan gate list
uv run jiaoduan gate approve g3
```

Policy is set with `JIAODUAN_COMMIT=ask` (default), `deny`, or `allow` (fixtures only).

## Tools

| Tool | What it does |
| --- | --- |
| `surfaces`, `open(target)` | list or open tabs and apps; a URL opens a page, a name or `pid:N` an app |
| `observe(surface?, root?, mode?, query?)` | the UI as indented lines with `[eN]` refs; `query` searches and returns each match with its row |
| `act(ref, action, value?, expect?)` | press, double, right, hover, focus, set, append, toggle, select, scroll, upload |
| `key(keys)` | chords such as `cmd+a` or `Enter`, sent to the surface, never the frontmost app |
| `read(ref?)`, `wait(expect)` | text under an element; block until a postcondition holds |
| `form(root?)`, `fill(values)` | every field with kind, value and options; fill many fields at once |
| `tools()`, `call(name, args)` | adapter tools that apply to this surface |
| `report(status, note, artifacts)` | how a worker hands its result to the orchestrator |

`expect` takes `value`, `checked`, `text_visible`, `appears`, `gone`, `url`, or `request`
(web only), alone or as a list.

## Forms

`fill` picks a driver per field, writes through the widget's real input path and reads back
from its real source of truth:

| Driver | Writes with | Reads from |
| --- | --- | --- |
| text | focus, select all, trusted `insertText` | the DOM value, and React's props when present |
| select | the one allowed script write, plus `input` and `change` | the selected option's text |
| toggle, radio | a press, only if the state differs | `checked` / `aria-checked` |
| buttons | a press, only if not already chosen (a second press often clears it) | ARIA state, or the one member with extra class tokens |
| combobox | typing, then a click on the exact option; never Return | the text the widget shows |
| listbox popup | open, find the popup that just appeared, scroll a virtual list | the button's text |
| file | `DOM.setFileInputFiles`; no OS dialog | `input.files` |

Order matters: files first (uploads often re-render the form), then text, selects, custom
pickers and buttons. After the last write it waits briefly and reads every field again, which
catches values a page changes on its own a moment later, such as an autofill that doubles a
name. New fields that appeared and required fields still empty are listed.

```
fill: 10/11 ok | 1 mismatch | new fields: f12 "Visa type"
f1 text "First name" mismatch "AdaAda" (changed after it was written)
f5 select "Country" ok "Taiwan" evidence driver
```

## Adapters

An adapter turns a flow that works into a named tool. It is TOML with no code: steps are
`goto`, `fill`, `press`, `wait`, `read` and `expect` over role and name locators, and every
step goes through the same engine calls an agent would make, including the gate.

```toml
[adapter]
id = "example.org/contact"
version = 1
match = { url = "https://example.org/contact*" }
concurrency = { key = "origin", max = 1 }

[[tool]]
name = "send_contact_message"
input.topic = { type = "string", enum = ["Sales", "Support"] }
input.message = { type = "string" }

[[tool.step]]
fill = { Topic = "{topic}", Message = "{message}" }

[[tool.step]]
press = { role = "button", name = "Send message" }
commit = true

[[tool.step]]
wait = { text_visible = "Thanks" }
```

Put adapters in `~/Library/Application Support/jiaoduan/adapters/` or a directory named by
`JIAODUAN_ADAPTERS`. When a page changes, a step fails with `ADAPTER_DRIFT` and the current
view, so the agent can finish by hand. `jiaoduan adapter check FILE` validates one, and
`jiaoduan adapter draft TRACE` turns an action trace (`JIAODUAN_TRACE=path`) into a first
draft; typed values were never recorded, so they become inputs.

## Several agents

```toml
# plan.toml
[defaults]
concurrency = 3

[[agent]]
name = "collect"
task = "Open the orders page and write every refunded order to refunds.csv"

[[agent]]
name = "summarise"
task = "Summarise the refunds"
after = ["collect"]
```

`jiaoduan run plan.toml` runs one `claude -p` per agent in dependency waves. Workers get
only this layer's tools and never talk to each other. Each one finishes with `report`, and
later agents receive earlier reports. They share one browser through the daemon, which
enforces the rules:

- each tab or app belongs to one agent at a time, released when its connection drops;
- adapters limit how many agents work on one site at once;
- every pending commit shows up in the same `jiaoduan gate list`.

## How it works

```
claude / claude -p ── stdio MCP shim ─┐
claude / claude -p ── stdio MCP shim ─┼─ Unix socket ── jiaoduand (engine)
jiaoduan CLI ───────────────────────┘                   ├─ web: CDP over a pipe ── Chrome
                                                          └─ mac: Accessibility API ── apps
```

- **Web**: a hand-written CDP client over `--remote-debugging-pipe` (no open port, no
  third-party driver). Per-frame accessibility trees are merged across same-origin and
  cross-origin iframes and shadow DOM. Clicks are trusted input events, aimed at a box that
  held still twice and hit-tested in the element's own frame, so a covered element is
  reported instead of clicking whatever is on top. Settling waits for the requests an action
  started, frame loads, and 150 ms without DOM mutations.
- **macOS**: batched attribute reads with a 3 s messaging timeout and one thread per app, so
  a hung app blocks only its agent. Electron and Chromium apps are asked for their full tree.
  Keys are posted to the app's process, so the frontmost app and the pointer stay put.
- **Refs** stay with the element. When a framework replaces a node, the ref moves to the
  single element with the same role, name and scope, and the result says `rebound`. With
  zero or several candidates the ref dies with `STALE_REF` and look-alikes are listed; the
  layer never clicks a similar element on its own.
- **Page text is untrusted.** Control characters are stripped, labels are quoted and cut, and
  password fields never return a value.

Early measurements, from `spikes/`:

- A full tree is 37–60% of a one-line-per-node rendering, and a skeleton is a median 21%.
- Trusted `insertText` reaches React state; a script-written value does not.
- React kept 0 of 6 node ids across a remount, and unique role and name matching found all 6
  again.

## Benchmark

`bench/` compares four ways for the same model to do the same tasks. Every arm runs with no
built-in tools and an empty working directory, and none may run page JavaScript.

| Arm | Tools |
| --- | --- |
| A | screenshots and coordinates (Playwright MCP vision) |
| B | accessibility snapshots (Playwright MCP default) |
| C | this layer, element level: `observe` and `act` |
| D | this layer, form level: `form` and `fill` |

Fixtures are written from scratch and imitate no real site. They reproduce what makes agents
fail:

- inputs that ignore untrusted events;
- comboboxes whose options load late, and remote suggestions where only a picked one counts;
- virtual lists, and uploads that re-render the form;
- doubling autofill, yes/no buttons whose state lives in a class name;
- Return that submits;
- fields that appear only after an answer, a shadow-root component, and a cross-origin iframe;
- a "Saved" toast shown before the request behind it fails.

Success is decided from what the page posted to the fixture server, or from the state file
the macOS fixture app writes. The agent can read neither.

| Task | Platform | Kind | What it tests |
| --- | --- | --- | --- |
| `contact-send` | web | form, submits | a custom dropdown, then one submission |
| `application-fill` | web | form | 11 fields with every trap above, no submission |
| `application-visa` | web | form | the same form plus a field that appears after an answer |
| `checkout-wizard` | web | form, submits | three steps, remote city suggestions, shadow DOM, a cross-origin frame |
| `settings-save` | web | form | the first save fails behind a success toast |
| `orders-lookup` | web | read | one cell in a 120-row table |
| `orders-count` | web | read | counting rows with one status |
| `doc-lookup` | web | read | one figure in a 40-section agreement with several decoys |
| `mac-fill` | macOS | form | text, checkbox, pop-up and radio in a Cocoa app, no save |
| `mac-save` | macOS | form, submits | the same app, with its default button behind the gate |

Arms A and B are Playwright and run only the web tasks.

```bash
uv run python -m bench.run --smoke                 # no model: every task is passable, sizes
uv run python -m bench.run --repeat 5              # all arms; uses model quota
uv run python -m bench.report bench/results/*.jsonl
uv run python -m bench.parallel                    # several agents at once, no model
```

`--smoke` runs a scripted solution for each task (`bench/solutions.py`) under the task's own
commit policy, to prove the task can be passed and the checker accepts a correct run.

Reports give success rates with Wilson intervals, and token counts with bootstrap intervals.

### Results (2026-11-09 and 11-10)

Setup:

- Model: `claude-sonnet-5-5`, Playwright MCP 0.0.83, Chrome 157, 3 runs per cell.
- Input tokens include cached tokens, so the `$` column, which prices them, is the fairer cost.
- Raw records are in [`bench/published/2026-11-09.jsonl`](bench/published/2026-11-09.jsonl)
  (99 runs). Runs of task versions later fixed are kept apart in
  [`2026-11-09-superseded.jsonl`](bench/published/2026-11-09-superseded.jsonl) (36 runs).

Totals:

| Arm | Web tasks passed | macOS tasks passed |
| --- | --- | --- |
| A, screenshots | 17/24 | not run |
| B, snapshots | 20/24 | not run |
| C, elements | 23/24 | 6/6 |
| D, forms (form tasks only) | 15/15 | 6/6 |

| task | arm | success (95% CI) | input tokens | vs B | tool calls | s | $ |
| --- | --- | --- | --- | --- | --- | --- | --- |
| application-fill | A | 1/3 (6%-79%) | 450,518 | -49% | 88.7 | 63 | 0.249 |
| application-fill | B | 2/3 (21%-94%) | 877,888 |  | 104.7 | 117 | 0.320 |
| application-fill | C | 3/3 (44%-100%) | 55,477 | -94% | 16.3 | 22 | 0.054 |
| application-fill | D | 3/3 (44%-100%) | 38,147 | -96% | 10.0 | 17 | 0.038 |
| application-visa | A | 1/3 (6%-79%) | 368,747 | +21% | 83.3 | 55 | 0.220 |
| application-visa | B | 0/3 (0%-56%) | 303,830 |  | 36.3 | 53 | 0.131 |
| application-visa | C | 2/3 (21%-94%) | 63,779 | -79% | 18.3 | 24 | 0.058 |
| application-visa | D | 3/3 (44%-100%) | 44,412 | -85% | 10.7 | 20 | 0.041 |
| checkout-wizard | A | 3/3 (44%-100%) | 193,706 | +71% | 88.0 | 46 | 0.179 |
| checkout-wizard | B | 3/3 (44%-100%) | 113,065 |  | 16.7 | 28 | 0.053 |
| checkout-wizard | C | 3/3 (44%-100%) | 61,981 | -45% | 11.7 | 22 | 0.041 |
| checkout-wizard | D | 3/3 (44%-100%) | 56,919 | -50% | 9.0 | 22 | 0.035 |
| settings-save | A | 3/3 (44%-100%) | 149,920 | +108% | 35.0 | 29 | 0.116 |
| settings-save | B | 3/3 (44%-100%) | 72,028 |  | 11.3 | 22 | 0.045 |
| settings-save | C | 3/3 (44%-100%) | 50,257 | -30% | 10.0 | 18 | 0.039 |
| settings-save | D | 3/3 (44%-100%) | 53,282 | -26% | 10.0 | 18 | 0.040 |
| contact-send | A | 0/3 (0%-56%) | 126,094 | +211% | 38.3 | 32 | 0.111 |
| contact-send | B | 3/3 (44%-100%) | 40,552 |  | 5.7 | 13 | 0.029 |
| contact-send | C | 3/3 (44%-100%) | 22,862 | -44% | 4.0 | 9 | 0.023 |
| contact-send | D | 3/3 (44%-100%) | 24,147 | -40% | 3.3 | 10 | 0.023 |
| orders-lookup | A | 3/3 (44%-100%) | 34,976 | +39% | 5.0 | 10 | 0.040 |
| orders-lookup | B | 3/3 (44%-100%) | 25,087 |  | 3.0 | 8 | 0.017 |
| orders-lookup | C | 3/3 (44%-100%) | 20,729 | -17% | 2.0 | 7 | 0.024 |
| orders-count | A | 3/3 (44%-100%) | 19,361 | -42% | 2.0 | 8 | 0.026 |
| orders-count | B | 3/3 (44%-100%) | 33,397 |  | 2.3 | 8 | 0.070 |
| orders-count | C | 3/3 (44%-100%) | 23,729 | -29% | 2.0 | 8 | 0.038 |
| doc-lookup | A | 3/3 (44%-100%) | 39,737 | +67% | 5.3 | 13 | 0.031 |
| doc-lookup | B | 3/3 (44%-100%) | 23,838 |  | 2.0 | 7 | 0.029 |
| doc-lookup | C | 3/3 (44%-100%) | 36,468 | +53% | 2.7 | 8 | 0.052 |
| mac-fill | C | 3/3 (44%-100%) | 24,116 |  | 6.0 | 11 | 0.024 |
| mac-fill | D | 3/3 (44%-100%) | 22,577 |  | 3.3 | 11 | 0.019 |
| mac-save | C | 3/3 (44%-100%) | 22,944 |  | 4.0 | 10 | 0.020 |
| mac-save | D | 3/3 (44%-100%) | 24,586 |  | 4.0 | 10 | 0.020 |

What this does and does not show:

- **The gap is on hard forms.** On the two application forms, snapshots (B) passed 2 of 6
  runs at $0.13-0.32 each. In all three visa runs, B left the two custom dropdowns
  (degree, school) empty. This layer used 79-96% fewer input tokens and passed 11 of 12. The
  miss was one C run that skipped a yes/no button.
- **Multi-step forms save about half.** The checkout and settings tasks passed in every arm.
  C and D used 26-50% fewer tokens than B and cost $0.035-0.041 a run, against $0.045-0.053.
- **Reading is not where this layer helps.** On the three lookup tasks every arm passed. C
  used fewer tokens than B on two and 53% more on the long document. In dollars it was
  cheaper on only one of the three.
- **Screenshots (A) lost the most.** Lacking feedback that a click had worked, it sent the
  contact form twice in two of three runs. On forms it needed 35-89 tool calls where this
  layer needed 3-18.
- **No one was fooled by the false "Saved".** In two earlier versions of the settings task
  that did not ask for a retry, every run in every arm saw that the first save had failed,
  and none claimed a save that had not happened. Only 2 of 24 retried without being asked.
  The task therefore now asks for a retry, which measures finishing the job rather than the
  model's appetite for retrying.
- **macOS has no comparison arm.** The screenshot-and-mouse alternative would take over the
  person's pointer, and the fixture window sits off screen. The macOS rows show only that both
  of this layer's arms finish native tasks at about 23k tokens.
- **Three runs per cell is not much.** The intervals are wide, and success rates overlap
  everywhere except the application forms.
- **We wrote the fixtures and the checker, and both had bugs.** They imitate failure patterns,
  not a real site, and a layer can be tuned to its own tests.
  - The first full run exposed three bugs in this layer. C failed the application all three
    times, twice sending it with Enter. The C and D rows for the first three tasks are after
    the fixes:
    - `select` was missing from the MCP tool;
    - upload did not work through a button that opens the file picker;
    - `set` on a native dropdown did nothing.
  - Two task bugs made tasks impossible for every arm, and those runs are in the superseded
    file:
    - the checkout prompt ended a value with a full stop that every agent typed;
    - the settings page did not show saved values after a reload.
- **The gate policy differs by arm.** This layer's arms run with its shipped default, `ask`;
  Playwright has no gate. Only tasks whose job is to submit (`contact-send`,
  `checkout-wizard`, `mac-save`) run with `allow`.

### Several agents at once (2026-11-09)

`bench.parallel` runs four tasks on one engine, first one after another, then all at once.
The tasks are `contact-send`, `application-fill`, `checkout-wizard` and `mac-fill`. No model
is involved: each agent runs the task's scripted solution, so this measures the coordination
layer, not an agent. Three rounds of each:

| mode | wall s (median) | tasks passed | refused claims | frontmost app or pointer moved |
| --- | --- | --- | --- | --- |
| serial | 17.8 | 12/12 | 0 | 0/3 |
| parallel | 7.4 | 12/12 | 0 | 0/3 |

Every agent got its own window or app. The checker found every page and app in the right
state, and the person's frontmost app and pointer stayed put. Parallel model runs through
`jiaoduan run` have been checked end to end with two agents, not measured at scale.

## Development

```bash
uv sync --extra dev --extra mac
uv run pytest                  # unit tests, no browser
uv run pytest -m chrome        # real headless Chrome against the fixtures
uv run pytest -m mac           # real Accessibility against a Cocoa fixture app
uv run mypy && uv run ruff check src tests bench
```

The mac tests need Accessibility access for the terminal that runs them.
