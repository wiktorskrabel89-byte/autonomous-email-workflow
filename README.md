# Autonomous Email Workflow Lab (`email-workflow-lab`)

A Python CLI that reads your unread email, works out what each message needs, and
either files it, drafts a reply, or escalates it to you. It has a deterministic
safety layer, thread supersession, idempotency tracking, an append-only audit log
and multi-channel reports (Terminal, Discord, Email, WhatsApp).

---

## Get it

Works the same on **Windows, macOS and Linux**. You need **Python 3.10 or newer**
and a Gmail account.

```bash
git clone https://github.com/wiktorskrabel89-byte/autonomous-email-workflow.git
```
```bash
cd autonomous-email-workflow
```
```bash
pip install -e .
```
```bash
email-workflow setup
```

The setup wizard asks for your AI provider, your key, your mailbox and your
notification channel, and writes them to a `.env` file for you. Then:

```bash
email-workflow
```

Want to see it work before connecting anything to your real mailbox? This needs
no API key and makes no network calls at all:

```bash
email-workflow demo
```

**What you will need**

| Thing | Where to get it | Free? |
|---|---|---|
| An AI API key | [Google AI Studio](https://aistudio.google.com/apikey) for Gemini, or OpenAI / Groq / OpenRouter | yes, on the free tier |
| A Gmail app password | [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) - **not** your normal password | yes |
| Discord webhook (optional) | your server's channel settings | yes |

Everything is yours: your keys stay in your `.env`, which is gitignored and
never leaves your machine unless you deliberately schedule a cloud run.

---

## Getting started on a new machine

Nothing is tied to one computer, and nothing is tied to one operating
system: it runs the same on **Windows, macOS and Linux**, needs Python 3.10
or newer, and every dependency is pure Python. Copy the folder, then:

```bash
pip install -e .
```

```bash
email-workflow setup
```

The setup wizard asks for your AI provider, model, API key and mailbox, and writes
them to a `.env` file for you. Then:

```bash
email-workflow
```

Want to see it work before connecting anything? This needs no API key and makes no
network calls at all:

```bash
email-workflow demo
```

### Before you share this folder with anyone

`.gitignore` protects a `git clone`, but a zip or a copied folder carries your
personal files with it. Clear them first:

```bash
email-workflow reset
```

That removes your login, knowledge base, run history and usage records. It does
**not** touch `config.yaml` (your email address) or `.env` (your keys) — check
those yourself.

### Where your settings live

| File | What it holds | In git? |
|---|---|---|
| `config.yaml` | provider, model, mailbox, thresholds | yes |
| `.env` | API keys and passwords | **no** |
| `auth.json` | your login (salted hash only) | **no** |
| `known_facts.txt` | your personal knowledge base | **no** |
| `usage.jsonl` | how much of each API key you used | **no** |
| `state.json`, `audit.jsonl`, `idempotency.json` | run bookkeeping | **no** |

The `.env` file is looked for in the project folder, then the folder you ran the
command from, then your home folder. Everything else is always read from the
project folder, so running the tool from a different directory cannot silently
pick up a different config.

---

## Logging in

The app can read your mailbox and send email, so it asks who you are before it
opens. On first run it asks you to choose a username and password.

Your password is never written anywhere. What is stored in `auth.json` is a
PBKDF2-SHA256 hash (600,000 iterations) with a random salt, which cannot be
turned back into the password.

```bash
email-workflow passwd
```

changes the password, or removes the login entirely.

Forgot it? Delete `auth.json` and the app will ask you to set a new one.
To turn the login off permanently, set `security.require_login: false` in
`config.yaml`.

---

## Choosing a model

Providers retire models, and when that happens every request fails with a 404.
To see what your key can actually use right now:

```bash
email-workflow models
```

This only lists models — it does not spend any generation quota, so it is safe on
a free tier.

**A listed model is not always a usable one.** Google still lists
`gemini-2.5-flash`, but a newer key gets back *"no longer available to new
users"*. The error message now quotes Google's own words, including the
replacement it suggests.

The defaults are set for a free tier:

| Role | Model |
|---|---|
| Main | `gemini-3.1-flash-lite` — the cheapest that works |
| Falls back to | `gemini-3-flash-preview` — the oldest that still works |

---

## Staying inside the free tier

Free tiers cap **requests per minute** — Gemini's is commonly 15. One email is not
one request: classifying it, checking it for risk and writing a reply are up to
three separate calls, so twenty unread emails can mean sixty requests.

The app spaces its own requests out, so it does not get refused:

```yaml
ai:
  api:
    requests_per_minute: 15   # 0 turns throttling off
```

It uses a sliding window rather than a fixed delay: an idle allowance can be spent
at once, so a single email runs with no waiting at all, and a request only waits
when it would genuinely be the 16th within the last minute. A plain "sleep 4
seconds between calls" would make one email take 8 seconds of staring at nothing;
a token bucket that starts full would allow 29 requests in the first minute and
earn a 429. Neither does what you want; this does both.

---

## Several API keys at once (and several emails at a time)

A free tier belongs to an **account**, not to you. Three Gemini keys from three
different Google accounts are three separate allowances - so the app uses all
three, and works on three emails at the same time, one key each.

Number them in `.env`. There is nothing else to set up:

```
GEMINI_API_KEY=key-from-your-main-account
GEMINI_API_KEY_2=key-from-your-second-account
GEMINI_API_KEY_3=key-from-your-third-account
```

Any name that starts with the provider's own key name is picked up, so
`GEMINI_API_KEY_WORK` works as well as `GEMINI_API_KEY_2`. The same goes for
`GROQ_API_KEY_2`, `OPENAI_API_KEY_2` and the rest.

The same key written twice under two names counts **once**. That is one
account, and pretending it is two only earns you a rate-limit error.

At the start of a run it says what it found:

```
Keys in use (3):
  - Google Gemini [GEMINI_API_KEY] / gemini-3.1-flash-lite
  - Google Gemini [GEMINI_API_KEY_2] / gemini-3.1-flash-lite
  - Google Gemini [GEMINI_API_KEY_3] / gemini-3.1-flash-lite

3 API keys - 3 emails at a time.
```

### The two ways they work together

**They split the inbox.** Each key takes a different email, at the same time,
and each result appears the moment it lands. Every key keeps its own
per-minute allowance, so three keys are three times the headroom - not three
workers queueing behind one limit. A key that runs out of quota is stepped
over and the others carry on.

Within a single email the work already spreads too: reading it, weighing it and
writing the reply are three separate requests, and the pool hands each one to
whichever key has the most left.

**They team up on one email.** When a reply is about to be sent, a *different*
key reads it first and can object. It looks for facts the writer invented,
commitments nobody asked for, and questions left unanswered. If it objects the
reply is **not sent** - it is kept as a draft, and the objection goes in the
audit log. A model marking its own homework is worth nothing, so this only
happens when there is a second key to ask.

Both are on by default and can be turned off separately:

```yaml
ai:
  keys:
    auto_detect: true   # use every key you have, not just the first
    parallel: true      # several emails at once, one key each
    max_workers: 0      # 0 = one worker per key
    review: true        # a second key checks a reply before it is sent
```

`email-workflow usage` breaks the totals down per key, so you can see how much
of each account you have spent.

---

## Seeing how much of your key you have used

```bash
email-workflow usage
```

Shows calls, tokens, failures and quota refusals — today and over the last week.

**Why this is counted locally:** Gemini sends no rate-limit headers and offers no
usage endpoint for an API key, so nothing can ask Google how much of a free tier
is left. These totals are exact for this app, and do not include anything else
sharing the same key. Where a provider *does* report limits (Groq, OpenAI send
`x-ratelimit-*` headers), those numbers are shown too.

To get a percentage, tell the app your allowance — copy it from your provider's
dashboard:

```yaml
ai:
  usage:
    warn_at_percent: 80
    limits:
      gemini:
        requests_per_day: 250
```

---

## Falling back to another provider automatically

If one provider hits its quota mid-run, the run does not die — it continues on the
next one that has a key.

Put more keys in `.env` and they are picked up automatically:

```text
GEMINI_API_KEY=...
GROQ_API_KEY=...
OPENAI_API_KEY=...
```

The chain can also hold several models from the same provider — try the cheap one
first, then a bigger one:

```yaml
ai:
  fallback:
    chain:
      - provider: gemini
        model: gemini-3-flash-preview
```

You will see a line like:

```text
Google Gemini / gemini-3.1-flash-lite stopped working (quota). Switching to Google Gemini / gemini-3-flash-preview and carrying on.
```

Only failures another provider could survive cause a switch — a quota wall, a dead
key, a retired model, a network drop. A malformed request fails everywhere, so it
is reported immediately instead of being retried four times. The audit log records
the provider that **actually** answered, not the one named in `config.yaml`.

---

## Can it run offline?

Partly, and it depends what you mean.

| | Works offline? |
|---|---|
| `email-workflow demo` | **Yes, fully.** No key, no network, at all. |
| The AI, in local mode (`ai.mode: local`) | **Yes.** Ollama runs on your machine; nothing leaves localhost. |
| The AI, with a cloud provider | **No.** Gemini, Groq, OpenAI and OpenRouter are remote services — there is no way to call one without a connection. |
| Reading your mailbox | **No.** Fetching Gmail needs the internet by definition. |

So "completely offline with a cloud provider" is not possible. What *is* possible
is never being stopped by an outage or an exhausted quota: your local model sits
at the end of the fallback chain.

```yaml
ai:
  fallback:
    use_local_last: true
```

Once every cloud provider has failed — connection down, all free quotas spent —
the work continues on Ollama. It costs nothing when unused, because it is only
ever reached after everything above it has failed.

---

## Running it every day

```bash
email-workflow schedule
```

It asks two questions - where, and at what time - and does the rest.

**"On this computer"** registers a daily task with whatever your system uses:
Task Scheduler on Windows, a launchd agent on macOS, cron on Linux. Free and
private, but it only fires while the machine is awake, so a laptop that is shut
at that hour simply misses the run. It says so before setting anything up.

**"On GitHub"** is the one that works with your computer off. It signs you in to
GitHub (GitHub's own login, in your browser - this app never sees a password),
creates a **private** repository, pushes this folder, uploads your keys as
repository secrets, writes your chosen time into the workflow and pushes that
too. Then GitHub runs it every day whether you are there or not.

Before anything leaves your computer it shows exactly what will happen - the
repository name, every secret by name, and the time it will run - and waits for
a yes. It refuses outright if `.gitignore` is not protecting your `.env`, your
login and your knowledge base: a private repository is still a copy of those
files on someone else's computer, and one click can make a repository public
later.

You need `git` and the GitHub CLI (`gh`). If either is missing it tells you the
one command that installs it on your system.

Times are local. GitHub schedules in UTC only, so the command converts for you,
and warns that a fixed schedule drifts by an hour when the clocks change.

### Setting up the GitHub part by hand

`.github/workflows/email-workflow.yml` is ready to use if you would rather do
it yourself:

1. Push this folder to a **private** GitHub repo (`config.yaml` has your address).
2. Settings → Secrets and variables → Actions, and add `GEMINI_API_KEY`,
   `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `DISCORD_WEBHOOK_URL`.
   Keys from your other accounts go in the same way - `GEMINI_API_KEY_2`,
   `GEMINI_API_KEY_3` - and are used at the same time, exactly as they are
   locally. A secret you do not set arrives empty and is ignored.
3. Actions tab → enable workflows.

It then runs daily at 06:00 UTC and the Discord report arrives whether your
computer is on or not. Use "Run workflow" on the Actions tab to try it at once
instead of waiting.

Notes worth knowing:

- GitHub cron is always **UTC** — 06:00 UTC is 07:00 or 08:00 in Poland,
  depending on the season, and a busy scheduler can start a few minutes late.
- What was already handled is cached between runs. Without that, every run would
  see the same unread emails as new and draft a second reply to each.
- Unattended runs set `EMAIL_WORKFLOW_DISABLE_LOGIN=1`, because nobody is there
  to type a password. Your local copy stays locked.
- The local-model fallback cannot help here: GitHub's machines have no Ollama.
- **GitHub switches scheduled workflows off in a repo that has had no activity
  for 60 days.** If the reports stop arriving, that is the first thing to check:
  push anything, or press "Run workflow", and the schedule resumes.
- A private repo gets 2000 free Actions minutes a month. A daily run of a few
  minutes is nowhere near that, and several keys make each run shorter, not
  longer.

Other options: a Raspberry Pi or any always-on machine (`--loop`), or a cloud
VM. Oracle's always-free tier looks tempting but reclaims an instance that
stays under 20% CPU, network and memory for seven days, which is exactly what
a job running two minutes a day looks like.

---

## Which emails get processed

Only **unread** mail, and only from the **last 7 days**:

```yaml
email:
  max_age_days: 7        # how far back to look
  max_emails_per_run: 0  # 0 = no limit on how many
```

The age window stops a first run on an old mailbox from processing years of
backlog. There is no cap on how many emails a single run handles.

Mail is fetched with `BODY.PEEK[]`, so reading it does **not** mark it as read on
the server. If a run fails halfway through, nothing is lost — those emails are
still unread and get picked up next time.

---

## Sending is off by default

```yaml
email:
  allow_send: false
```

Sending email on someone's behalf cannot be undone, so it has to be switched on
deliberately. While it is off, a reply that passed every safety check is saved as
a **draft** instead, and the audit log says exactly that.

This used to be worse than off: the line that actually sends was commented out,
so `send_email` returned a success id while nothing left the machine — the audit
log, the digest and the user were all told a reply had gone out. A reply now
either really sends or says clearly that it did not. The same applied to drafts,
where a failed save still reported "Draft created".

---

## What it does to your inbox

| Decision | What happens to the email itself |
|---|---|
| Archived / ignored | marked read, and taken out of the inbox |
| Draft written | starred and labelled, left unread in the inbox |
| Escalated to you | starred and labelled, left unread in the inbox |

Archiving is the Gmail kind: the email leaves the inbox but stays in **All Mail**
and in search. It is not deleted and it does not land in Trash.

**On a non-Gmail mailbox nothing is archived** - Outlook and the rest only get the
email marked as read, and it stays in the inbox. Gmail is the only server where an
archive can be done safely over IMAP; on the others the same commands would really
delete your mail, so the app deliberately refuses to try.

A draft is starred on purpose. It is mail still waiting on you, so it should be as
easy to find as anything escalated - otherwise it disappears among everything the
app has already dealt with.

```yaml
email:
  archive_unimportant: true      # false = unimportant mail is only marked read
  star_important: true           # false = nothing is starred
  important_label: AI/Important  # the label put next to the star
```

If the mailbox refuses an archive, the run tells you instead of quietly claiming it
worked: the email is untouched and will be picked up again on the next run.

---

## When something goes wrong

Expected failures are explained, not dumped as a Python traceback:

```text
+- Could not finish ----------------------------------------------------------+
| Google Gemini will not serve the model 'gemini-2.5-flash'.                   |
|                                                                             |
| Google Gemini says: This model models/gemini-2.5-flash is no longer          |
| available to new users. Please update your code to use                      |
| models/gemini-3.6-flash for the latest features and improvements.           |
|                                                                             |
| What to do:                                                                 |
| Run 'email-workflow models' to see the models your key can use...           |
+-----------------------------------------------------------------------------+
```

This covers a retired model, a rejected key, a quota wall, a timeout, a reply cut
off by the token limit, and a provider that cannot return valid JSON.

---

## Commands

| Command | What it does |
|---|---|
| `email-workflow` | interactive main menu |
| `email-workflow run` | process the inbox once |
| `email-workflow run --loop --interval 60` | keep polling |
| `email-workflow run --schedule 18:00` | run daily at a set time |
| `email-workflow demo` | offline demo, no API key, no network |
| `email-workflow models` | list models your key can use |
| `email-workflow usage` | how much of your keys you have used |
| `email-workflow providers` | which API keys are set |
| `email-workflow dashboard` | current settings and status |
| `email-workflow setup` | interactive setup wizard |
| `email-workflow facts` | edit your personal knowledge base |
| `email-workflow passwd` | change or remove the login |
| `email-workflow reset` | erase your personal data before sharing |
| `email-workflow log` | read the audit log |
| `email-workflow replay --thread-id X` | full history of one thread |
| `email-workflow test-report` | test Discord/Email/WhatsApp delivery |

---

## Reports to Discord, Email or WhatsApp

Configure any of these through `email-workflow setup`:

- **Discord** — paste a webhook URL, saved as `DISCORD_WEBHOOK_URL`.
- **Email (SMTP)** — saved as `NOTIFICATION_SENDER_EMAIL`,
  `NOTIFICATION_SENDER_PASSWORD`, `NOTIFICATION_RECIPIENT_EMAIL`.
- **WhatsApp (Twilio)** — saved as `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
  `TWILIO_WHATSAPP_FROM`, `TWILIO_WHATSAPP_TO`.

Check delivery any time with `email-workflow test-report`.

---

## How the AI is prompted

The prompts live in `src/email_workflow/providers/base_ai.py`. They follow four
rules:

1. **The model judges; it does not echo.** Message ids, sender and subject are
   merged in by the code afterwards, so the model cannot corrupt them and no
   output tokens are spent repeating data we already have. This matters on a free
   tier.
2. **Everything the model is told to use, it actually receives.** Your
   `known_facts.txt` is passed into classification, decision support and reply
   writing.
3. **Rules are ordered,** so the model never has to guess which one wins.
   Anything touching passwords, payments or bank details is escalated and can
   never be auto-replied to.
4. **Missing information is named, never invented.** A reply that needs a fact
   nobody supplied gets `[NEEDS INPUT: ...]` rather than a plausible guess.

`tests/test_prompts.py` checks the prompts and the code still agree — every
placeholder is filled, and the JSON block in each prompt matches the model it is
parsed into.

---

## Tests

```bash
python -m pytest -q
```

285 tests covering the decision engine, thread supersession, provider failover,
rate limiting, the login gate, prompt/schema agreement, IMAP fetch rules, real
sending and drafting, what the app does to the mailbox (archiving, starring),
pools of keys from different accounts and several emails at once,
offline and local mode, CLI dispatch and full pipeline runs. None of them make a
network call.
