# agent-layer

A screenshot-free interface for AI agents to other people's web pages and macOS apps, where
every action reports what actually happened and how strong the evidence is.

GUIs are drawn for eyes. An agent driving one by screenshot pays a thousand-plus tokens per
look, a round trip per step, coordinates that drift, and one more screenshot to learn whether
the click worked. What an agent needs is smaller: the current state, what it can do, a way to
do it, and whether it took effect. agent-layer sits between the agent and the UI and gives it
exactly that, as text, for any page or app that exposes accessibility information.

```
press button "Add another" [e12]: delivered | evidence tree | settled 180ms | net POST /draft 200
+ group "Employment 2" [e40]
  + textbox "Employer" [e41] [required]
rebound (re-rendered, same element): e12
```

> Status: v0.1 in development. The name is provisional.

## What is different

| | Typical tools | agent-layer |
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
- **Approval happens outside the model's tools**: `agentlayer gate approve` asks for a `y`
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
claude mcp add agent-layer -- uv --directory "$PWD" run agentlayer-mcp
```

The MCP server starts a background daemon on first use. The daemon owns one Chrome (headless
by default) on a dedicated profile; Chrome refuses remote control of your everyday profile.
To use a site that needs a login, sign in once in that profile:

```bash
uv run agentlayer login https://example.com
```

When an agent hits a commit, it reports a gate id and stops. Review and approve it in your own
terminal:

```bash
uv run agentlayer gate list
uv run agentlayer gate approve g3
```

Policy is set with `AGENTLAYER_COMMIT=ask` (default), `deny`, or `allow` (fixtures only).

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

Put adapters in `~/Library/Application Support/agent-layer/adapters/` or a directory named by
`AGENTLAYER_ADAPTERS`. When a page changes, a step fails with `ADAPTER_DRIFT` and the current
view, so the agent can finish by hand. `agentlayer adapter check FILE` validates one, and
`agentlayer adapter draft TRACE` turns an action trace (`AGENTLAYER_TRACE=path`) into a first
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

`agentlayer run plan.toml` runs one `claude -p` per agent in dependency waves. Workers get
only this layer's tools and never talk to each other. Each one finishes with `report`, and
later agents receive earlier reports. They share one browser through the daemon, which
enforces the rules:

- each tab or app belongs to one agent at a time, released when its connection drops;
- adapters limit how many agents work on one site at once;
- every pending commit shows up in the same `agentlayer gate list`.

## How it works

```
claude / claude -p ── stdio MCP shim ─┐
claude / claude -p ── stdio MCP shim ─┼─ Unix socket ── agentlayerd (engine)
agentlayer CLI ───────────────────────┘                   ├─ web: CDP over a pipe ── Chrome
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
- comboboxes whose options load late;
- virtual lists, and uploads that re-render the form;
- doubling autofill, yes/no buttons whose state lives in a class name;
- Return that submits.

Success is decided from what the page posted to the fixture server, which the agent cannot
read.

```bash
uv run python -m bench.run --smoke                 # no model: every task is passable, sizes
uv run python -m bench.run --repeat 5              # all arms; uses model quota
uv run python -m bench.report bench/results/*.jsonl
```

Reports give success rates with Wilson intervals, and token counts with bootstrap intervals.

### Results (2026-11-09)

Setup:

- Model: `claude-sonnet-5-5`, Playwright MCP 0.0.83, Chrome 157, 3 runs per cell.
- Input tokens include cached tokens, so the `$` column, which prices them, is the fairer cost.
- Raw records are in [`bench/published/2026-11-09.jsonl`](bench/published/2026-11-09.jsonl).

| task | arm | success (95% CI) | input tokens | vs B | tool calls | s | $ |
| --- | --- | --- | --- | --- | --- | --- | --- |
| application-fill | A | 1/3 (6%-79%) | 450,518 | -49% | 88.7 | 63 | 0.249 |
| application-fill | B | 2/3 (21%-94%) | 877,888 |  | 104.7 | 117 | 0.320 |
| application-fill | C | 3/3 (44%-100%) | 55,477 | -94% | 16.3 | 22 | 0.054 |
| application-fill | D | 3/3 (44%-100%) | 38,147 | -96% | 10.0 | 17 | 0.038 |
| contact-send | A | 0/3 (0%-56%) | 126,094 | +211% | 38.3 | 32 | 0.111 |
| contact-send | B | 3/3 (44%-100%) | 40,552 |  | 5.7 | 13 | 0.029 |
| contact-send | C | 3/3 (44%-100%) | 22,862 | -44% | 4.0 | 9 | 0.023 |
| contact-send | D | 3/3 (44%-100%) | 24,147 | -40% | 3.3 | 10 | 0.023 |
| orders-lookup | A | 3/3 (44%-100%) | 34,976 | +39% | 5.0 | 10 | 0.040 |
| orders-lookup | B | 3/3 (44%-100%) | 25,087 |  | 3.0 | 8 | 0.017 |
| orders-lookup | C | 3/3 (44%-100%) | 20,729 | -17% | 2.0 | 7 | 0.024 |

What this does and does not show:

- **The gap is on the hard form.** Snapshots (B) needed 105 tool calls and $0.32 per
  application. The traps include a late autofill, a virtual list and a re-render on upload.
  This layer needed 10-16 calls and $0.04-0.05, and every one of its runs left the right
  values in the page's own state.
- **Easy pages are close.** Contact form and table lookup differ by a few calls. On the lookup,
  C used fewer tokens but cost more than B.
- **Screenshots (A) lost the most.** Lacking feedback that a click had worked, it sent the
  contact form twice in two of three runs.
- **Three runs per cell is not much.** The intervals are wide, and success rates overlap
  everywhere except the application form.
- **We wrote the fixtures.** They imitate failure patterns, not a real site, and a layer can be
  tuned to its own tests. The first full run is how that showed: it exposed three bugs in this
  layer.
  - `select` was missing from the MCP tool.
  - Upload did not work through the button that opens the file picker.
  - `set` on a native dropdown did nothing.

  C failed the application all three times, and twice sent it with Enter. The C and D rows
  above are after those fixes. A and B are from the first run, whose harness and tasks were
  otherwise identical.
- **The gate policy differs by arm.** This layer's arms run with its shipped default, `ask`;
  Playwright has no gate. Only the contact task, whose job is to submit, runs with `allow`.

## Development

```bash
uv sync --extra dev --extra mac
uv run pytest                  # unit tests, no browser
uv run pytest -m chrome        # real headless Chrome against the fixtures
uv run pytest -m mac           # real Accessibility against a Cocoa fixture app
uv run mypy && uv run ruff check src tests bench
```

The mac tests need Accessibility access for the terminal that runs them.
