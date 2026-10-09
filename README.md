# Speed to Lead

A Facebook lead form submission arrives at a webhook. Within seconds it is scored 1 to 5 for fit, given a drafted first reply, posted to Discord for a rep, and logged to Airtable with timestamps that show exactly how long it took. In the growth-case experiment this is built from, first contact with a new lead took about 22 hours, long after most leads have moved on. This pipeline is the treatment arm of that experiment built for real: it cuts the time from form submission to a drafted reply in front of a rep from 22 hours to seconds, timestamps every step so the speed is measurable, and adds a fit score so reps call the right people first.

## Architecture

```
POST /webhook/facebook-lead
  |
  |-- record received_at, return 200 immediately (Facebook expects a fast ack)
  |
  '-- background task
        1. parse      field_data -> Lead (tolerant of missing, extra, and messy fields)
        2. rules      hard caps from door count and contact validity (deterministic)
        3. LLM        one Claude call: fit_score, reason, reply, pain_points
        4. score      final_score = min(llm_score, rule_cap)
        5. Discord    post the lead, scores, reason, and reply      -> notified_at
        6. Airtable   write the record with latency_seconds          -> or CSV fallback
```

Code layout:

| Path | What it does |
|---|---|
| `app/main.py` | FastAPI app, `/health`, the webhook, and the background task |
| `app/parse.py` | Facebook `field_data` to a `Lead`, including door counts like "about 200" or "50-100" |
| `app/rules.py` | Hard rules and caps, pure functions |
| `app/llm.py` | The Claude call, validation, retry, reply cleanup, fallback templates |
| `app/prompts/qualify_and_reply.md` | The prompt, loaded at runtime |
| `app/sinks/discord.py` | Discord webhook sink |
| `app/sinks/airtable.py` | Airtable sink with CSV fallback |
| `fixtures/leads/` | Seven fake leads with expected score ranges |
| `scripts/` | `send_test_lead.py` and `eval_scoring.py` |
| `tests/` | Unit tests; the LLM, Discord, and Airtable are always mocked |

### Respond first

The order is deliberate. The webhook acknowledges before doing any work. Discord goes before Airtable, so a slow or failing Airtable write can never delay the reply reaching a rep. `notified_at` is recorded when Discord confirms the message, and `latency_seconds` in Airtable is `notified_at - received_at`: true end to end, including the Discord call. The Discord message itself shows "Processed in", measured just before sending, because it cannot contain its own delivery time.

### Fallbacks: every lead gets a reply

| Failure | What happens |
|---|---|
| LLM error, timeout, refusal, or invalid output | Retry once. If that fails too, use a template reply chosen by the rule cap, with `reason = "LLM unavailable"` and no score. Worst case is two 20-second attempts, inside the 60-second budget. |
| Discord fails | Logged. The Airtable record is still written, with `notify_status = failed` and the drafted reply kept so a rep can send it. |
| Airtable fails or is not configured | The row is appended to `leads_fallback.csv` with the same columns. |
| No valid email or phone | The reply is empty, since there is nowhere to send it. Discord shows "(no reply sent: no valid contact)". |

Nothing in the background task raises past its own boundary: each sink returns success or failure, and the task logs and moves on.

## Setup

Requires Python 3.12 (built and tested on 3.12.10).

```powershell
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env          # macOS/Linux: cp .env.example .env
```

Fill in `.env`. It is in `.gitignore`; never commit it.

| Variable | What it is |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `ANTHROPIC_MODEL` | Defaults to `claude-haiku-5-5`. The call forces a tool call, so the model must accept a forced `tool_choice`. Tested with `claude-haiku-5-5`. |
| `AIRTABLE_TOKEN` | Personal access token |
| `AIRTABLE_BASE_ID` | Starts with `app`, visible in the base URL |
| `AIRTABLE_TABLE_NAME` | Defaults to `Leads` |
| `DISCORD_WEBHOOK_URL` | Discord incoming webhook URL |

If the Airtable settings are missing, records go to the CSV fallback instead, so you can run everything without Airtable.

### Airtable

1. Create a base called `Speed to Lead` with a table called `Leads`.
2. Add these fields:

   | Field | Type |
   |---|---|
   | `leadgen_id` | Single line text (primary field) |
   | `received_at` | Date, include time |
   | `name`, `company` | Single line text |
   | `email` | Email |
   | `phone` | Phone number |
   | `doors` | Number |
   | `message`, `reason`, `reply` | Long text |
   | `llm_score`, `rule_cap`, `final_score` | Number |
   | `notify_status` | Single select: `sent`, `failed` |
   | `notified_at` | Date, include time |
   | `latency_seconds` | Number, 2 decimals |
   | `flags` | Single line text |

3. Timestamps are sent as UTC (`2026-10-09T21:38:05.000Z`). To see them in local time, make sure both date fields are Date fields (not text) and set their display time zone.
4. Create a personal access token at airtable.com/create/tokens with the `data.records:write` scope, limited to this base.
5. Add a grid view sorted by `final_score` descending so the best leads are on top.

### Discord

In the channel that should receive leads: Edit Channel, Integrations, Webhooks, New Webhook, Copy Webhook URL. Put the URL in `DISCORD_WEBHOOK_URL`. The URL contains the webhook's secret token, so treat it like a password.

## How to run

Start the server:

```powershell
.venv\Scripts\python -m uvicorn app.main:app
```

In a second terminal, send fake leads. These go through the real pipeline, so they call Anthropic, post to Discord, and write to Airtable.

```powershell
.venv\Scripts\python scripts\send_test_lead.py --list
.venv\Scripts\python scripts\send_test_lead.py ideal_late_rent
.venv\Scripts\python scripts\send_test_lead.py --all          # every fixture, 2 seconds apart
```

Run the scoring eval. This calls only the Anthropic API (no server, Discord, or Airtable): 7 fixtures x 3 runs = 21 calls by default.

```powershell
.venv\Scripts\python scripts\eval_scoring.py
.venv\Scripts\python scripts\eval_scoring.py --runs 5 --only prompt_injection
```

Run the unit tests. They never touch a real service.

```powershell
.venv\Scripts\python -m pytest
```

## Sample output

**Discord**

![Discord messages for two high-fit leads](docs/discord.png)

Edge cases: a junk submission with no valid contact gets no reply, and a one-unit landlord is capped by the rules and gets a polite no-fit.

![Discord messages for a junk lead and a one-unit landlord](docs/discord_edge_cases.png)

**Airtable**

![Airtable Leads table sorted by final score](docs/sample_output.png)


[docs/sample_output.csv](docs/sample_output.csv) is the full Airtable export from a clean run of all 7 fixtures.

A reply drafted for the ideal-fit fixture (180 doors, late rent, a buried bookkeeper), from the final eval run:

```
Hi Dana, chasing late rent every month and having your bookkeeper buried in
reconciliations by the 10th are exactly the problems Blaze is built for.
Automated payments and bookkeeping on autopilot keep rent and books current
across all 180 doors. Book a call here: [booking link]

The Blaze team
```

`[booking link]` is a literal placeholder; the model is told never to write a real or invented URL.

## Eval results

Seven fixtures, each run three times through parse, rules, and the LLM. A fixture passes only if all three runs pass. Each run is checked for:

- final score inside the expected range
- reply at most 320 characters (em dashes are stripped and reported as a warning, not a failure)
- `[booking link]` present for final 4-5 and absent for 1-3
- reply ends with "The Blaze team" on its own line
- reply empty when the lead has no valid contact, and not empty otherwise

| Fixture | Expected | Rule cap | Baseline | v1 | v2 |
|---|---|---|---|---|---|
| ideal_late_rent | 5 | none | 5 5 5 pass | 5 5 5 fail | 5 5 5 pass |
| in_range_no_message | 4 | none | 4 4 4 pass | 4 4 4 fail | 4 4 4 pass |
| landlord_one_unit | 1-2 | 2 | 1 1 1 pass | 2 2 2 fail | 2 2 2 pass |
| tenant_complaint | 1 | none | 1 1 1 pass | 1 1 1 fail | 1 1 1 pass |
| junk_gibberish | 1 | 1 | 1 1 1 pass | 1 1 1 pass | 1 1 1 pass |
| enterprise_2000 | 1-3 | 3 | 3 3 3 fail | 3 3 3 fail | 3 3 3 pass |
| prompt_injection | 1-2 | none | 3 3 3 fail | 1 1 1 fail | 1 1 1 pass |
| **Fixtures passed** | | | **5/7** | **1/7** | **7/7** |

Full output for each run is in `eval_baseline.txt`, `eval_after.txt`, and `eval_after_v2.txt`.

**Baseline: 5/7.** Two real misses.
- `prompt_injection` scored 3 on every run. The lead claimed 200 doors and a valid email, so no rule capped it, and the prompt only said to "score lower" for manipulation. The model did lower it, from 5 to 3, which is still a qualified lead and gets a qualifying question.
- `enterprise_2000` was capped correctly at 3, but the model scored it 4 and wrote a tier 4 reply with a booking link. The prompt never said tier 3 replies leave the link out.
- The run also logged one connection error and one invalid response on first attempts; both succeeded on the retry, which is the retry path working as intended.

**v1: every score correct, 1/7 on the reply format.** Prompt changes drafted with the AI assistant:
- Any attempt to instruct the scorer means `fit_score` 1, regardless of the other facts. Injection went from 3 to 1.
- Tier 3 replies have no booking link. Enterprise replies became a single qualifying question with no link.
- Tier 4-5 replies must name one specific Blaze capability that matches the lead's stated pain, or one that matters at their size, and never generic phrases.
- Leads with no valid contact get an empty reply.
- Every reply ends with "The Blaze team" on its own line, plus eval checks for that.

Two more edits were made by hand after reviewing the AI's diff:
- The rubric's tier 3 now covers "more than 500 doors" instead of only 501-1000, so a 2,000-door company is borderline by the model's own rubric, not just by the rule cap. This is why the enterprise model score dropped from 4 to 3; before, the model scored it 4 and only the rule cap brought the final score down.
- Tier 3 asks about something the lead hasn't already said, so a lead who gave a door count is not asked for it again.

All 21 scores landed in range, but six fixtures failed the new sign-off check: the model wrote "The Blaze team" inline at the end of the last sentence, and once typed a literal `\n\n` instead of line breaks. Only `junk_gibberish` passed, because its reply was correctly empty.

**v2: 7/7.** The sign-off moved out of the prompt and into code. The prompt now says to write no sign-off and caps the model's text at 300 characters. `llm.py` strips any sign-off the model adds anyway (inline, on its own line, after a closer like "Best,"), converts literal `\n` sequences, and appends a blank line plus "The Blaze team" to every non-empty reply, including the fallback templates. 300 characters plus the 16-character sign-off stays within the 320-character SMS limit.

## Design decisions

**Rules and the LLM each do what they are good at.** Deterministic rules run first and set a cap: no valid email or phone caps at 1, fewer than 20 doors at 2, 20-49 or over 500 doors at 3. The LLM scores on its own judgment, and the final score is the lower of the two. Both are stored, so the Airtable view shows where rules overrode the model (`enterprise_2000` in the baseline: model 4, cap 3, final 3). Rules handle what is checkable and must never be wrong; the model handles what needs reading, such as telling a tenant from a property manager. The reply is written for the capped tier, so a capped lead never gets a sales push the score does not support.

**Door counts are parsed with a confidence check.** "about 200", "200+", and "1.5k" parse as numbers, and "50-100" as the midpoint. Text with several numbers that is not a range, like "2 buildings, 40 units", is flagged `doors_ambiguous` instead of guessing, and the raw text goes to the LLM to read.

**Prompt injection is handled in layers.** The lead's fields are sent as JSON inside a `<lead_data>` block, with `<` and `>` escaped so a message cannot close the block early (the injection fixture tries exactly that). The prompt says everything in the block is untrusted data, never instructions, and that an explicit attempt to instruct the scorer means a score of 1. Ordinary urgency ("we need this ASAP") is called out as normal, so real leads are not penalized. The reply is validated against a schema with length limits, and email and phone are never sent to the model since no rule needs them there.

**Structured output is enforced, not requested.** The model must call a `record_qualification` tool with a strict schema, and the result is validated with Pydantic. An out-of-range score or an overlong field counts as invalid and triggers the retry. The SDK's own retries are turned off so the worst case stays at two 20-second attempts.

**The sign-off moved into code.** The model reliably followed the scoring rules but not the sign-off rule: in v1 it put "The Blaze team" inline and once typed a literal `\n\n`. That was the observed failure, so the sign-off is now added in code. Em dash cleanup is a defensive safeguard rather than a fix for an observed problem: every eval run so far shows 0 em dashes. The eval still reports the count as a warning column, so if the model starts using them the cleanup will not hide it.

**Log hygiene.** The Discord webhook URL contains its token, and HTTP client libraries log full request URLs. The `httpx`, `httpx2`, `httpcore`, and `httpcore2` loggers are set to WARNING wherever the Discord sink is imported, and a test confirms the URL never appears in any log record. Failure logs record only the error type and HTTP status, never error text that could carry request details. The Airtable token is only ever sent in a header and is never logged.

**Timestamps are UTC instants.** Both datetimes go to Airtable as ISO 8601 UTC (`...Z`), so Airtable stores the correct moment and displays it in whatever time zone the field is set to.

## Limitations and with more time

- **Real Facebook integration.** Real Lead Ads webhooks send only a `leadgen_id`; production would fetch `field_data` from the Graph API and verify the webhook signature. The fixtures already use the Graph API response shape, so only the fetch step changes.
- **SMS instead of Discord.** Sending the drafted reply by SMS through Twilio needs A2P 10DLC registration, the same blocker noted in the experiment spec. Discord stands in as the rep notification for now.
- **CRM handoff.** Push leads into GoHighLevel and create the call task for reps automatically, and add booking-source tagging from the experiment spec.
- **Durability.** Background tasks run in the web process, so a crash mid-lead loses that lead. A queue (or at least a write of the raw payload on receipt) would make every lead recoverable. CSV fallback rows are not replayed into Airtable automatically.
- **Idempotency.** Sending the same `leadgen_id` twice creates two records and two notifications. Facebook retries webhooks, so production needs deduplication.
- **Deployment.** This runs on localhost. A public URL (Render or Railway) is needed for real webhooks.
- **Eval size.** Seven fixtures and three runs per fixture catch regressions but are not a statistical measure. More real-world leads, especially borderline ones like an eager but legitimate lead or a vague door count, would make the eval more representative.
- **Sign-off cleanup edge cases.** A sign-off that follows a plain word with no punctuation ("...today The Blaze team") is not detected, and a sign-off after a comma drops the comma without adding a full stop.
- **CSV opened in Excel.** Lead text starting with `=` could be interpreted as a formula. Escaping is not applied because it would also mangle phone numbers starting with `+`.
- **Model choice.** The forced tool call means the model must accept a forced `tool_choice`, and the pipeline has only been tested with `claude-haiku-5-5`. Supporting a model that does not would mean switching to `auto` tool choice or structured outputs.
- **The injection reply promises a follow-up.** The injection fixture is correctly scored 1, but its reply reads "We appreciate your interest and will be in touch if there is a fit." That tells a likely spammer to expect a response. Replies to leads flagged as injection attempts should make no promise, or not be sent at all.
