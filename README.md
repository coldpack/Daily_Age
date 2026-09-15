# Daily Aging Research Briefing        

#py aging_briefing.py --lookback 1

Every morning, this collects newly published papers from 16 aging / gerontology /
geroscience journals, sorts **all** titles by journal, renders
them as a PDF, and emails that PDF to you.

---

## Why it queries Crossref instead of scraping the journal websites

The original ask was to scrape the sixteen journal sites. That approach breaks
badly in practice:

- Wiley, Oxford University Press, Elsevier, Springer and Karger all sit behind
  bot protection (Cloudflare / PerimeterX). A daily scraper gets challenged,
  throttled, then IP-blocked, usually within a few weeks.
- Their terms of use generally prohibit automated harvesting of article listings.
- Sixteen sites means sixteen different HTML layouts, each of which silently
  breaks on redesign. You would find out via a briefing that quietly stopped
  including *Aging Cell*.

Every journal on your list deposits its article metadata with **Crossref**, the
DOI registration agency, which publishes it through a free, documented, rate-
limit-friendly API intended for exactly this. Same titles, same DOIs, same day —
one interface that publishers *want* you to use. So that's the backend here.

The one trade-off: Crossref indexing typically lags publication by 0–3 days, and
varies by publisher. The program handles this by querying a rolling window rather
than a single day and remembering every DOI it has already sent, so nothing is
duplicated and nothing is dropped in the gap.

---

## Install

Requires Python 3.10 or newer.

**Windows** (PowerShell or Command Prompt):

```
cd aging-briefing
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

If `py` is not recognised, install Python from python.org and tick
**"Add python.exe to PATH"** during setup.

**macOS / Linux:**

```bash
cd aging-briefing
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

---

## First-run checklist

**1. Verify the ISSNs.** Everything depends on these being right, and a wrong
ISSN fails silently — you'd just never see that journal again. This takes 30
seconds:

```bash
python aging_briefing.py --check-issns
```

It prints each configured journal beside the title Crossref returns for that
ISSN and flags mismatches with `!`. Fix any flagged entry in `journals.json`.
(Some flags are harmless — a journal that renamed itself, or a title Crossref
records slightly differently. Read the line before changing anything.)

**2. Preview the layout** without touching the network or your inbox:

```bash
python aging_briefing.py --sample
```

**3. Configure email:**

```
copy .env.example .env        # Windows
cp .env.example .env          # macOS / Linux
```

Then open `.env` in a text editor and fill it in. Notepad is fine — the program
strips the byte-order mark Notepad adds.

For Gmail you need an **App Password** (Google Account → Security → 2-Step
Verification → App passwords), not your account password.

**4. Do a real fetch without sending anything:**

```bash
python aging_briefing.py --dry-run --lookback 3
```

Open the PDF in `briefings/`. If it looks right, run it for real:

```bash
python aging_briefing.py
```

---

## Scheduling

### Windows

Edit the `$Folder` path at the top of `scheduling/register_task.ps1`, then run
it from PowerShell:

```
powershell -ExecutionPolicy Bypass -File scheduling\register_task.ps1
```

That registers a Task Scheduler job for 07:15 daily with three settings that
matter on a laptop:

- **StartWhenAvailable** — runs as soon as the PC wakes if it was off at 07:15.
- **RunOnlyIfNetworkAvailable** — skips cleanly instead of failing on no Wi-Fi.
- **ExecutionTimeLimit** — 30 minutes, so nothing can wedge indefinitely.

Test it right away, then check the outcome:

```
Start-ScheduledTask -TaskName "Aging Research Briefing"
Get-ScheduledTaskInfo -TaskName "Aging Research Briefing"
```

`LastTaskResult` of `0` means success. The task runs as you and only when you're
logged in, so no password is stored. To remove it:
`Unregister-ScheduledTask -TaskName "Aging Research Briefing"`.

You can also just double-click `run_briefing.bat` to run it by hand.

### Other platforms

| Platform | Method | Notes |
|---|---|---|
| macOS | `scheduling/com.user.agingbriefing.plist` → `~/Library/LaunchAgents/` | Catches up after sleep. Setup in the file's header comment. |
| Linux | `scheduling/aging-briefing.{service,timer}` → `~/.config/systemd/user/` | `Persistent=true` catches up after downtime. |
| Any | `crontab -e` → `15 7 * * * /full/path/to/run_briefing.sh` | Simplest, but skips runs if the machine is off. |
| Cloud | `.github/workflows/daily-briefing.yml` | Runs whether or not your PC is on. Setup in the file's header comment. |

Both launchers write to `logs/briefing.log`. Overlap protection is built into
the program itself (an atomic lock file), so two runs can never collide no
matter how it's scheduled.

---

## What the PDF contains

**One page, always.** The briefing is a single sheet you can scan in a minute or
print and carry.

- **Masthead** — a widely letter-spaced serif wordmark under an accent bleed
  rule, with the date and index window opposite, closed by a heavy-over-light
  double rule on a softly tinted band.
- **Stat strip** — how many new papers, from how many journals.
- **The list** — every new title in one A–Z sequence, numbered, grouped under
  letter dividers, with the journal (in small caps), date and lead authors
  beneath each. Columns are separated by hairline rules.
- **Contributing journals** — a compact tally at the foot, shown on days when
  the list leaves room for it.

**Every title is a clickable link** that opens the paper at the publisher via
its DOI. The whole title is the click target, not just a DOI string, so it's an
easy hit on a phone as well as a desktop.

### How one page is guaranteed

Publication volume is uneven — some days bring a dozen papers, some ninety. The
renderer walks down a ladder of thirteen layout densities, from a generous
single column at 11pt to three tight columns at 5.4pt, rendering each into
memory and measuring the result. The first layout that fits on one page wins, so
a quiet day gets airy typography and a heavy day gets a newspaper-style
three-column index. Nothing is hard-coded to a guessed article count.

Roughly 90 titles fit at the tightest setting. Beyond that, the tail of the
alphabet is trimmed, the page says exactly how many were held back, and the
footer reads *"88 of 112 titles"*. **Nothing is lost** — the email body always
carries the complete list. If you would rather have every title on the page than
keep to one sheet, set `max_pages: 2` in `journals.json`.

Filing follows normal index convention: punctuation and Greek prefixes are
ignored for sorting, so *"Successful" ageing* files under **S** and *β-amyloid
clearance…* files under **A** for "amyloid". Set `ignore_leading_articles: true`
if you'd rather *The role of…* file under **R**.

## Configuration (`journals.json`)

| Setting | Default | What it does |
|---|---|---|
| `lookback_days` | `2` | Rolling window, in days, for a first run or after a failure. Widen for backfill. |
| `ignore_leading_articles` | `false` | File "The/A/An" titles under their next word. |
| `max_pages` | `1` | The one-page target. Raise to `2` if you'd rather never have the tail trimmed on heavy days. |
| `send_when_empty` | `true` | Send a "nothing new" briefing on quiet days. Set `false` to stay silent instead. |
| `include_titles_in_email_body` | `true` | Also list titles as plain text in the email, so you can skim on a phone without opening the PDF. |
| `seen_retention_days` | `180` | How long DOIs stay in the dedupe memory. |
| `contact_email` | — | Sent to Crossref in the User-Agent. Polite, and gets you their faster request pool. Worth setting. |

To mute a journal, set `"enabled": false` on it. To add one, add a `name` and its
`issns`, then re-run `--check-issns`.

`noise_title_patterns` drops errata, corrigenda, editorial-board pages, tables of
contents and conference-abstract dumps. Add your own regexes if something
unwanted keeps appearing.

---

## How duplicates are prevented

`state.json` records every DOI ever included, with the date it was sent. It is
written **only after the email is successfully sent**, so a failed send doesn't
silently eat a day's papers — the next run picks them up again. It's written
atomically, so an interrupted run can't corrupt the history.

`last_success` is only advanced when *every* journal responded. If one publisher
was unreachable, the next run re-covers that window rather than skipping it, and
the PDF notes which journals were missing.

---

## Command reference

```
python aging_briefing.py                  # normal daily run
python aging_briefing.py --dry-run        # fetch + build PDF, no email, no state write
python aging_briefing.py --no-email       # build PDF, still record state
python aging_briefing.py --sample         # offline layout preview
python aging_briefing.py --check-issns    # verify config against Crossref
python aging_briefing.py --lookback 14    # widen the window (backfill)
python aging_briefing.py --verbose        # debug logging
python aging_briefing.py --log-file logs\briefing.log
python aging_briefing.py --no-lock        # bypass the single-instance lock
python aging_briefing.py --out-dir "C:\Users\You\Documents\briefings"
```

Exit code `0` means success, `1` means every journal failed to respond — which
Task Scheduler surfaces as `LastTaskResult`, so a silently broken job is
visible without reading logs.

---

## Troubleshooting

**Zero papers every day.** Run `--check-issns`. If ISSNs are fine, try
`--lookback 7`; some journals publish in weekly batches and a quiet Tuesday is
normal.

**Gmail rejects the login.** You're using your account password. It needs an App
Password, which requires 2-Step Verification to be enabled first.

**The scheduled task reports `0x1` / `LastTaskResult` 1.** Every journal failed
to respond — usually no network at the time it ran. Nothing was sent and no
state was recorded, so the next run covers the same window. Open
`logs\briefing.log` for the detail.

**The task runs but no email arrives, and the log is empty.** Task Scheduler is
probably starting in the wrong directory. Confirm the action's "Start in" field
is set to the project folder; `register_task.ps1` sets this for you.

**"py is not recognized".** Python isn't on PATH. Reinstall from python.org with
"Add python.exe to PATH" ticked, or point `run_briefing.bat` at your
`python.exe` directly.

**A journal is always missing.** Its Crossref deposits may lag more than the
window. Raise `lookback_days` to 4–5; the DOI memory means a wider window costs
nothing but a slightly longer run.

**Greek letters show as boxes or vanish.** The program looks for a Unicode font
(Segoe UI, Arial, DejaVu, Liberation) and transliterates only if it finds none —
β becomes "beta". Windows always ships Segoe UI and Arial, so this should not
occur there.

**Changing the masthead face.** The wordmark and date use the first serif found
in `_serif_candidates()`: Palatino Linotype, then Garamond, Constantia, Georgia,
Times New Roman. Reorder that list to prefer a different one. If none is
present, reportlab's built-in Times is used, which needs no font file and so can
never be missing. The list body deliberately stays sans — it holds legibility at
6pt in a way serif type does not.

**Too much noise from one journal.** Add a regex to `noise_title_patterns`, or
disable the journal.

**The footer says "88 of 112 titles".** More papers arrived than fit on one
sheet, so the alphabetical tail was held back — the email body has all 112. Set
`max_pages: 2` if this happens often enough to bother you.

**The type is smaller some days than others.** That is the auto-fit working: a
heavier day gets a denser layout so it still lands on one page.

---

## Possible extensions

- **Keyword filtering** — flag or restrict to titles matching senescence,
  epigenetic clock, geroprotector, healthspan, and so on.
- **More sources** — bioRxiv/medRxiv preprints, or a PubMed query, alongside
  Crossref.
- **Weekly digest** — the same code with a `--lookback 7` cron on Mondays.

Metadata via Crossref. Please keep `contact_email` set — it's how they contact
you instead of blocking you if something misbehaves.
