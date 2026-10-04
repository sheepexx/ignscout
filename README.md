# Minecraft Name Finder

*Made by **sheepex_***

A rate-limit-aware command-line tool that streams large word lists, checks every candidate
against the **official Mojang / Minecraft Services API**, and reports which Minecraft: Java
Edition usernames are available, which may be released soon, and which are taken. It never
claims more certainty than the data supports.

- **Official endpoints first.** Uses bulk lookups of 10 names per request. A self-test before
  every scan stops a changed endpoint from being misread as "everything is available".
- **Honest statuses.** *Confirmed* availability, *unverified* availability and *estimated*
  release dates are always labelled separately.
- **Built for big lists.** Word lists are streamed line by line and de-duplicated on disk, so
  memory use stays flat for millions of words.
- **Careful rate limiting.** A shared token bucket enforces requests per second. On HTTP 429
  every worker pauses, `Retry-After` is honoured, and the rate drops automatically. Retries
  use exponential backoff with jitter.
- **Cached and resumable.** Results are cached in SQLite. Ctrl+C stops cleanly, and running
  the same command again resumes where it stopped.
- **Polished terminal UI.** A live Rich dashboard, crash-safe output files, CSV/JSON/JSONL
  export, and a local quality ranking for sorting.

---

## The easy way: no commands to type

**Windows:** double-click **`Start.bat`** in the project folder.
**Linux / macOS:** run `./start.sh`.

The first start installs everything it needs, which requires Python 3.12 or newer. After that,
a simple menu opens:

```text
╭──────────────  Minecraft Name Finder  ──────────────╮
│                                                     │
│   Find Minecraft Java names that nobody is using.   │
│                                                     │
╰────────────────────────────────  made by sheepex_  ─╯

What do you want to do?
   1  Check if a name is free
   2  Search for free names   words, or every 3- or 4-character name · can run overnight
   3  Continue my unfinished search   1 paused
   4  Show the free names I found
   5  Save my results to a file   Excel or text
   6  Confirm my best finds with Mojang   free, coming soon or blocked? needs your login
   7  How does this work?
   0  Quit
Choose a number:
```

1. Choose **2 · Search for free names**.
2. Pick **English dictionary**. It is downloaded for you the first time (370,000 words, 4 MB,
   public domain).
3. Choose a name length, then answer two yes/no questions.
4. The tool shows how many names it will check and how long that takes, and asks before
   starting:

   ```text
   ╭──────────────────────  Minecraft Name Finder  ──────────────────────╮
   │                                                                     │
   │   Word list        english.txt                                      │
   │   Names to check   55,111                                           │
   │   Time needed      about 1h 31m  (Mojang's speed limit)             │
   │   Sleep            your PC stays awake while this runs              │
   │   Stop any time    press Ctrl+C; everything found so far is saved   │
   │   Results          output\available.txt                             │
   │                                                                     │
   ╰────────────────────────────────────────────────  made by sheepex_  ─╯
   Start now? [y/n] (y):
   ```

You can leave it running overnight. The PC is kept awake automatically while it searches
(nothing is changed in your Windows settings), so just keep it plugged in. Closing a laptop's
lid can still put it to sleep. Press Ctrl+C or close the window to stop; next time choose
**3 · Continue my unfinished search**. Your finds are in `output\available.txt`, or use menus
4 and 5. Both ask how to sort the names: best first, shortest first, A to Z, or coming soon
first.

**Checking results while it runs.** The search window shows the latest finds live. You can
also open `output\available.txt` at any time; every find is added the moment it is found, so
reopen the file to see new ones. Or double-click `Start.bat` again for a second window and
choose **4** (show) or **5** (save to Excel). Only one search can run at a time, because two
would share Mojang's speed limit, so the second window won't start another one.

**Every 3- or 4-character name.** In menu 2, choose **Every short name**, then 3 or 4
characters, then one of three groups. The groups never overlap, so running all three covers every
possible name exactly once:

| Group | Contains | 3 characters | 4 characters |
|---|---|---|---|
| Only letters | `a–z` | 17,576 · about 30 min | 456,976 · about 13 h |
| With numbers | at least one digit (`ab12`, `1234`) | 29,080 · about 50 min | 1,222,640 · about 34 h |
| With underscores | at least one `_` (`ab_c`) | 3,997 · about 7 min | 194,545 · about 5½ h |

The list is written to `wordlists\` once, and the search runs like any other: it keeps the PC awake,
can be stopped with Ctrl+C, and continues with menu 3. Almost every short name is taken, and many
that look free are blocked or locked, so when the search ends (or is paused) the menu offers to check
the free-looking ones with Mojang straight away (see below).

**Confirming your best finds (menu 6).** Without a login, Mojang only says whether an account owns
a name. Really free names, names Mojang blocks, names on hold for 37 days after a rename, and
locked names all come back as "likely available", and short names among them are usually blocked,
on hold or locked.
Menu 6 asks Mojang's own name check (the one minecraft.net uses when you change your name) about
your best-scored finds. Each comes back as **AVAILABLE NOW** (listed in `output\confirmed.txt`),
**coming soon** (on hold or locked; Mojang doesn't say which, or until when), **not allowed** or
**taken**. If you have both short and longer finds, it asks whether to check only the 3- and
4-character ones. It needs your
Minecraft access token, which the menu explains how to copy from minecraft.net. Mojang allows only
about 3–4 checks per minute, so 100 names take about half an hour. The token is never saved or
logged. Exports and menu 4 list confirmed names first.

Running `minecraft-finder` without a command opens the same menu. The rest of this README is
for people who prefer commands.

---

## Requirements

- Python **3.12 or newer** (developed and tested on CPython 3.14.3, Windows 10)
- Network access to `api.minecraftservices.com`
- Dependencies (installed automatically): `httpx`, `rich`, `typer`

## Installation

```bash
git clone <repository-url> minecraft-name-finder
cd minecraft-name-finder
python -m venv .venv
```

Activate the virtual environment:

```bash
# Windows
.venv\Scripts\activate

# Linux / macOS
source .venv/bin/activate
```

Install the package (this provides the `minecraft-finder` command):

```bash
pip install -e .            # users
pip install -e ".[dev]"     # contributors (adds pytest)
```

Optionally, copy the example configuration and adjust it:

```bash
cp config.example.toml config.toml      # Windows: copy config.example.toml config.toml
```

## Quick start

```bash
minecraft-finder check glacier                              # check a single name now
minecraft-finder scan words.txt                             # scan a word list
minecraft-finder scan examples/words-sample.txt --provider demo   # offline demo, no network
minecraft-finder generate 4 letters                         # list every 4-letter name, then scan it:
minecraft-finder scan wordlists/all-4-letters.txt
minecraft-finder verify --max-length 4                      # confirm only names of up to 4 characters
minecraft-finder export --sort quality                      # best available/soon names first
```

More examples:

```bash
minecraft-finder scan words.txt --min-length 3 --max-length 12
minecraft-finder scan words.txt --prefix x
minecraft-finder scan words.txt --suffix s
minecraft-finder scan words.txt --min-length 4 --max-length 8 --exclude-regex "[0-9_]"
minecraft-finder scan words.txt --transform compact          # "O'Brien" -> obrien, "ice-cream" -> icecream
minecraft-finder scan words.txt --starts-with sun --contains a
minecraft-finder scan words.txt --cache-ttl 7d               # re-use results for a week
minecraft-finder scan words.txt --force                      # ignore the cache
minecraft-finder scan words.txt --restart                    # start this scan over
minecraft-finder scan words.txt --dry-run                    # read and stage only; no requests
minecraft-finder check notch jeb_ glacier --json             # machine-readable output
minecraft-finder stats
minecraft-finder export --status all --format jsonl -o results.jsonl
minecraft-finder providers --test                            # one self-test request
minecraft-finder database clean --older-than 30d
```

## Screenshots

All screenshots are real output captured during development. Runs labelled `--provider demo`
use the offline simulator, whose results are made up. Everything else came from the live
Mojang API.

**Startup panel** (`scan words.txt --transform compact --suffix mc --max-length 12`):

```text
╭──────────────────────────  Minecraft Name Finder  ──────────────────────────╮
│                                                                             │
│   Word list        words.txt                                                │
│   Words loaded     19  (30 lines · 1 invalid · 8 filtered · 1 duplicate)    │
│   Progress         new scan                                                 │
│   Estimate         19 names to look up ≈ 2 requests · ~2s                   │
│   Provider         Minecraft  bulk lookup, 10 names/request  · unverified   │
│   Workers          4                                                        │
│   Rate             1.00 req/s  ≈10 names/s max                              │
│   Cache            TTL 24h                                                  │
│   Database         data\results.db                                          │
│   Output           output\                                                  │
│                                                                             │
╰────────────────────────────────────────────────────────  made by sheepex_  ─╯
```

**Live dashboard** (`scan words.txt --provider demo`, redrawn in place 4× per second):

```text
╭──────────────────────────────  Minecraft Name Finder  ───────────────────────────────╮
│                                                                                      │
│  Scanning   █████████████████████████████████████████████████░░░░░░░░░░░░░░░░   75%  │
│                                                                     240 / 320 names  │
│                                                                                      │
│  Checked           240      Rate        1.52 req/s  (limit 1.50)                     │
│  Available    15 (1 ✓)      Throughput  15.2 names/s                                 │
│  Soon                4      ETA         5s                                           │
│  Taken             220      Cache hits  0                                            │
│  Other               0      Requests    24                                           │
│  Errors              1      Elapsed     16s                                          │
│                                                                                      │
│  Latest discoveries                                                                  │
│  ✓  lumen17           likely available           score 60                            │
│  ◷  fable17           release unknown            score 60                            │
│  ✓  ivory15           likely available           score 60                            │
│  ◷  aurora15          estimated 6d · 2026-10-09  score 54                            │
│  ✓  jasper11          likely available           score 56                            │
│  ✓  dusk11            likely available           score 62                            │
│                                                                                      │
╰─  4 workers · Ctrl+C stops safely, progress is saved  ───────────────────────────────╯
```

While a 429 cooldown is active, the footer changes to
`⏸ rate limited (HTTP 429) — all workers paused for 23s`.

**Summary** (a faster run of the same demo list; two simulated errors are left for the next
run to retry):

```text
╭───────────────  Scan paused — some checks failed  ───────────────╮
│                                                                  │
│  Checked this run    320  (0 from cache)                         │
│  Available           23  (2 confirmed)                           │
│  Soon                5                                           │
│  Taken               289                                         │
│  Blocked / unknown   1 / 0                                       │
│  Errors              2  — retried automatically on the next run  │
│  Requests            32  (0 retries, 0 × HTTP 429)               │
│  Elapsed             2s                                          │
│  Job progress        318 / 320 (99%)                             │
│                                                                  │
╰──────────────────────────────────────────────────────────────────╯
Discoveries this run (best first)

      Username   Status                      Score
 ──────────────────────────────────────────────────
  ✓   lantern    likely available               86
  ✓   thistle    likely available               84
  ◷   echo1      estimated 5d · 2026-10-08      74
  ✓   echo3      available now (confirmed)      74
```

**`minecraft-finder check Notch qzx7vbn_k2lw ab`** (live API, one bulk request):

```text
╭─  Notch  ────────────────────────────────────────────────────────────────╮
│ Status         TAKEN                                                     │
│ Profile        Notch  069a79f4-44e9-4726-a5be-fca90e38aaf5               │
│ Provider       Minecraft                                                 │
│ Quality        88 / 100                                                  │
│ Checked        just now                                                  │
╰──────────────────────────────────────────────────────────────────────────╯
╭─  qzx7vbn_k2lw  ─────────────────────────────────────────────────────────╮
│ Status         LIKELY AVAILABLE                                          │
│ Confidence     unverified — no profile found, claimability not checked   │
│ Provider       Minecraft                                                 │
│ Details        No Minecraft profile uses this name. Claimability is not  │
│                verified — it may be reserved (37-day hold after a        │
│                rename) or blocked; use --verify with an access token to  │
│                confirm.                                                  │
│ Quality        15 / 100                                                  │
│ Checked        just now                                                  │
╰──────────────────────────────────────────────────────────────────────────╯
╭─  ab  ───────────────────────────────────────────────────────────────────╮
│ Status         INVALID                                                   │
│ Reason         too short (2 < 3 characters)                              │
│ Network        no request sent                                           │
╰──────────────────────────────────────────────────────────────────────────╯
```

**`minecraft-finder stats`:**

```text
╭────────────────────  Results database  ─────────────────────╮
│                                                             │
│        Status      Names   Share                            │
│   ──────────────────────────────────────────────────────    │
│    ✓   AVAILABLE       1    3.7%   █░░░░░░░░░░░░░░░░░░░     │
│    ◷   SOON            0    0.0%   ░░░░░░░░░░░░░░░░░░░░     │
│    ✗   TAKEN          26   96.3%   ███████████████████░     │
│    ⊘   BLOCKED         0    0.0%   ░░░░░░░░░░░░░░░░░░░░     │
│    ?   UNKNOWN         0    0.0%   ░░░░░░░░░░░░░░░░░░░░     │
│    !   ERROR           0    0.0%   ░░░░░░░░░░░░░░░░░░░░     │
│        Total          27                                    │
│                                                             │
│  data\results.db  ·  last check just now  ·  oldest 6m ago  │
╰─────────────────────────────────────────────────────────────╯
Scan jobs

  Job        Word list   Options                   Progress   State      Updated
 ────────────────────────────────────────────────────────────────────────────────
  61dea0e6   words.txt   transform=compact   26 / 26 (100%)   finished   4m ago
```

## Commands

| Command | Purpose |
|---|---|
| `scan WORDLIST` | Stream a word list, check every valid candidate, show the live dashboard |
| `check NAME [NAME…]` | Check names right now (uses the cache unless `--force`) |
| `stats` | Totals per status plus every scan job and its progress |
| `export` | Write results as CSV, JSON, JSONL or plain text (`-o -` for stdout) |
| `providers` | List the providers, their endpoints and limits (`--test` sends one request) |
| `database clean` | Delete ERROR/UNKNOWN rows and finished jobs' progress; optionally `--older-than 30d`; then VACUUM |
| `database info` | Database path, size, schema version and row counts |
| `verify` | Confirm your best "likely available" names with Mojang's own check (`--top 100`, `--max-length 4`; needs your access token) |
| `generate LENGTH [letters\|numbers\|underscores]` | Write every possible 3- or 4-character name of that group to `wordlists/`, ready for `scan` |
| `menu` | The simple numbered menu (also shown when no command is given) |

Global options: `--config PATH`, `--verbose`, `--debug`, `--version`. Run any command with
`--help` for the complete option list.

### Scan options

| Group | Options |
|---|---|
| Filters | `--min-length`, `--max-length`, `--starts-with`, `--ends-with`, `--contains`, `--regex`, `--exclude-regex` |
| Transform | `--transform none\|lowercase\|compact` (default `lowercase`), `--prefix`, `--suffix` |
| Network | `--workers`, `--requests-per-second`/`--rps`, `--timeout`, `--max-retries`, `--provider minecraft\|demo`, `--verify`, `--namemc` |
| Cache & progress | `--cache-ttl 24h`, `--force`, `--restart` |
| Other | `--db`, `--output-dir`, `--plain` (line output for CI and pipes), `--dry-run`, `--confirm` (show the plan, ask before starting), `--allow-sleep` (scans keep the PC awake by default), `--skip-self-test` |

Filters apply to the **final** username, after transform, prefix and suffix. Text filters
ignore case. `--regex` and `--exclude-regex` use Python regular expressions and match anywhere
in the name. Add `^…$` to match the whole name.

Each word goes through these steps: strip whitespace, transform, add the prefix and suffix,
**validate** against Minecraft's rules (3–16 characters; `A-Z a-z 0-9 _` only), filter, then
de-duplicate (case-insensitive). Invalid names never reach the network.

### Exit codes

`0` success · `1` runtime problem (for example a failed self-test) · `2` usage or configuration error · `130` interrupted with Ctrl+C

## What the statuses mean

| Status | Shown as | How it is determined |
|---|---|---|
| `AVAILABLE` (confirmed) | **AVAILABLE NOW** | Mojang's token-backed name-availability check returned `AVAILABLE` (needs `--verify`) |
| `AVAILABLE` (unverified) | **LIKELY AVAILABLE** | No profile uses the name. Without `--verify`, nobody has confirmed it can be claimed |
| `SOON` with estimate | **ESTIMATED RELEASE 2026-10-05** | A release time reported by an enrichment source that is consistent with the 37-day rule (see below). Always an estimate |
| `SOON` without estimate | **RELEASE UNKNOWN** | The name has no profile but is not claimable (Mojang answers `DUPLICATE`), which is typically the 37-day hold. No reliable date exists |
| `TAKEN` | **TAKEN** | A profile currently owns the name (UUID and canonical spelling are stored) |
| `BLOCKED` | **NOT ALLOWED** | Mojang refuses the name (`NOT_ALLOWED`; requires `--verify`) |
| `BLOCKED` (unverified) | **PROBABLY NOT ALLOWED** | Contains an offensive word that Mojang's name filter refuses (local word list, see below). Not sent to Mojang at all |
| `UNKNOWN` | **UNKNOWN** | The data doesn't allow a reliable answer (for example, sources disagree). Never cached |
| `ERROR` | **ERROR** | Network error, persistent 429 or an unexpected response. The name stays pending and is retried on the next run |

Without a token, "likely available" is the most the public API can tell you. A name with no
profile may still be inside the post-rename hold, or filtered by Mojang. Use `--verify` to
confirm, or try to claim the name; neither this tool nor any other can guarantee it.

### Offensive names

Mojang's name filter refuses profanity, sexual terms, slurs and hate symbols, but without a token the
public API simply reports "no profile", so names like `anus` or `boner` would look available. They
would even top the list, because they are short real words. A local word list
(`src/minecraft_finder/namefilter.py`) marks them as **PROBABLY NOT ALLOWED** instead. Scans skip them
without sending a request, and older results are reclassified the next time you run `scan`, `stats`,
`export` or open the menu. Inflections and compounds are caught (`boners`, `bitchy`, `xxbitchxx`),
while innocent look-alikes stay allowed (`bass`, `therapist`, `scrape`, `cocktail`, `spicy`). The list
can't be complete, because Mojang's real filter isn't public. `--verify` gives Mojang's actual answer.
Turn the filter off with `filter_offensive = false` under `[scanner]` in `config.toml`.

### "Soon" names and the 37-day rule

According to the Minecraft Help Center, a Java profile name can be changed **once every 30
days**. The old name is reserved for **37 days**: nobody can claim it during the first 30 days,
only the previous owner can take it back during days 30–37, and anyone can claim it after
that. Mojang **removed the public name-history endpoint on 2022-09-13**, so the official API
cannot say *when* a name was dropped. As a result:

- The tool never presents a release time as exact. Dates are always labelled *estimated*.
- An estimate is only kept if it falls inside the 37-day window. Times in the past, or further
  out than 37 days plus 1 day of tolerance, are downgraded to **RELEASE UNKNOWN**.
- With the default (official-only) setup, held names appear as **RELEASE UNKNOWN** when
  `--verify` is used. Estimated dates only come from the optional NameMC enrichment.
- A SOON result whose estimated time has passed is treated as stale and checked again.

## Rate limiting

Documented Mojang limits ([minecraft.wiki — Mojang API](https://minecraft.wiki/w/Mojang_API)):
about **200 requests per 2 minutes per IP** for most endpoints, and **20 requests per 5 minutes
per account** for the name-availability check. Mojang warns that large volumes of erroneous
requests, including many 429s, can lead to suspensions. The defaults are therefore slow:

| Behaviour | Default |
|---|---|
| Request budget | **1.0 req/s**, shared by all workers (token bucket, FIFO). Values above the documented 1.67 req/s are **capped** |
| Throughput | 10 names per bulk request, so about 10 names/s, or about 36,000 names/hour |
| Workers | 4. More workers overlap latency; they never raise the request rate |
| HTTP 429 | **All** workers pause for `Retry-After` (seconds or HTTP date). Without that header the pause is 30 s, doubling per retry up to 5 min, with upward-only jitter. The rate is then **halved** |
| Recovery | +10% of the configured rate after every 50 consecutive successes (slow on purpose) |
| 5xx / 408 / timeouts / connection errors | Exponential backoff with jitter (≈1, 2, 4, 8 … s, capped at 60 s). A 503 also slows the shared rate |
| Retries | `--max-retries 4`. After that the name is recorded as ERROR and retried on the next run |
| Server hints | An `x-minecraft-rate-limit-result` header other than `UNDER_LIMIT` triggers a proactive slowdown |
| Verification (`--verify`) | Separate limiter, 3 checks/min by default (Mojang allows 4/min) |
| NameMC | 1 request per 10 s at most (5 s minimum), and longer if robots.txt sets a `Crawl-delay` |

This is the 429 handling exercised end to end against a local mock server that rate-limits on
purpose (from `logs/minecraft-finder.log`):

```text
INFO    minecraft_finder.rate_limit: minecraft: rate 1.500 -> 0.750 req/s, cooldown 2.2s
WARNING minecraft_finder.http_client: minecraft: HTTP 429 from 127.0.0.1:51177/bulk — pausing all requests for 2.2s (Retry-After: 2s)
INFO    minecraft_finder.rate_limit: minecraft: rate 0.750 -> 0.375 req/s, cooldown 2.4s
WARNING minecraft_finder.http_client: minecraft: HTTP 429 from 127.0.0.1:51177/bulk — pausing all requests for 2.4s (Retry-After: 2s)
```

That scan still finished with 0 errors (`Requests 10 (3 retries, 3 × HTTP 429)`).

Rate limits must never be evaded. Don't run several copies in parallel, rotate IP addresses
or share tokens to go faster.

## Cache, progress and resume

Everything lives in `data/results.db` (SQLite in WAL mode):

- **`results`** has one row per username: `username`, `status`, `confidence`, `uuid`,
  `provider`, `checked_at`, `available_at`, `detail`, `last_error`, `quality_score`,
  `source_word`.
- **`scan_jobs`** has one row per distinct scan, keyed by a fingerprint of the word-list path,
  its size, and the transform/filter options.
- **`scan_items`** holds the de-duplicated candidates of each job and whether each one is done.

**Cache rules.** Results younger than `--cache-ttl` (default 24 h) are reused, not requested
again, across scans *and* `check` calls. `--force` bypasses the cache. ERROR and UNKNOWN
results are never cached. A failed check never overwrites an earlier real answer. The demo
provider uses a separate database (`data/demo-results.db`) and output folder, so simulated
data can't pollute real results.

**Resume.** Results and their *done* flags are committed together in one transaction, at least
once per second. Running the same command again continues where it stopped, and the startup
panel shows how many names still need a request. The first **Ctrl+C** stops the workers,
saves everything already received, prints a summary and exits with code 130; requests that
were in flight are simply repeated next time. A second Ctrl+C quits immediately, losing at
most about one second of results. On Windows, **Ctrl+Break** also stops gracefully. A finished
scan says so; use `--restart` to run it again, which still reuses fresh cache entries.

Verified during development: a scan interrupted with SIGINT at 90/400 names, then with
Ctrl+Break at 220/400, finished on the third run with exactly 400 results and no name
requested twice. A repeated scan of 26 cached names sent **0 requests**.

**One scan at a time.** A file lock (`data/results.db.scan.lock`) stops a second scan from starting
while one is running, because they would share the per-IP rate limit. The lock disappears on its
own if the process dies. Reading (`stats`, `export`, the menu) works while a scan runs, since SQLite
WAL mode lets readers and the writer work at the same time.

**Huge word lists.** Files are streamed line by line (UTF-8, BOM tolerated, undecodable bytes
replaced) and staged into SQLite, where duplicates are dropped on disk. Memory use is
independent of list size, and the total count is known before scanning starts, which makes
the ETA accurate.

## Output files

| File | Content |
|---|---|
| `output/available.txt` | One AVAILABLE name per line, de-duplicated across runs |
| `output/confirmed.txt` | Names Mojang confirmed as claimable (`verify` / menu 6) |
| `output/soon.txt` | `name<TAB>estimated 2026-10-08T12:49:06Z` or `name<TAB>release unknown` |
| `output/results.jsonl` | One JSON object per freshly checked name (append-only log) |
| `output/export.*` | Written by `minecraft-finder export` (atomically, via a temporary file) |

A real record from `results.jsonl`:

```json
{"username":"qzx7vbn_k2lw","status":"available","confidence":"unverified","checked_at":"2026-10-02T21:46:56.535493Z","provider":"minecraft","uuid":null,"available_at":null,"detail":"No Minecraft profile uses this name. Claimability is not verified — it may be reserved (37-day hold after a rename) or blocked; use --verify with an access token to confirm.","error":null,"quality_score":14.8,"source_word":null}
```

**Crash safety.** Every record is one complete line written with a single `write` call and
flushed immediately. Discoveries are `fsync`-ed at once; the JSONL log is synced every 200
records or 2 seconds. If a crash ever leaves a half-written last line, it is terminated before
anything new is appended, so it can't corrupt later records. JSONL readers should skip any
line that doesn't parse.

## Interesting-name ranking

Each result gets a local `quality_score` from 0 to 100. No external APIs are involved.

| Component | Points |
|---|---|
| Length | 40 for 3 letters, falling to 2 for 16 |
| Characters | 20, minus 6 per digit or underscore |
| Dictionary word | 15 if the name is exactly a letters-only word from your list (no prefix or suffix) |
| Pronounceability | Up to 25: vowel balance, consonant clusters, vowel runs, and no triple letters |

`export --sort quality` (the default) puts the best names first. Sorting never hides anything;
use `--min-score` if you want a cut-off.

## Configuration

Settings are resolved in this order: built-in defaults, then `./config.toml` (or `--config
PATH`), then command-line flags. See [`config.example.toml`](config.example.toml) for every
option:

```toml
[scanner]
workers = 4
requests_per_second = 1.0
timeout = 10
max_retries = 4

[cache]
ttl_hours = 24

[output]
directory = "output"
```

Relative paths (`data/`, `output/`, `logs/`) are resolved against the directory you run the command
from; set absolute paths in `config.toml` to keep one shared database. Unknown keys produce a warning. Credential-like keys (`token`, `access_token`, `cookie`, …)
are refused: the optional access token is read **only** from the environment variable named by
`providers.minecraft.token_env` (default `MINECRAFT_ACCESS_TOKEN`).

## Terminal symbols

Windows' classic console window (what opens when you double-click `Start.bat`) uses a font
that has no `✓ ◷ ✗` glyphs. The tool detects that window and uses `√ → ×` and an ASCII spinner
instead. Windows Terminal, VS Code, macOS and Linux terminals get the full symbols. Set
`MCF_SYMBOLS=simple` or `MCF_SYMBOLS=fancy` to choose yourself.

## Logging

Detailed logs go to `logs/minecraft-finder.log` (rotating, 5 MB × 3), while the terminal stays
clean. Warnings such as a 429 still appear in the terminal. Use `--verbose` for informational
messages, or `--debug` to also see per-request lines and tracebacks. A redaction filter on
every log handler scrubs bearer tokens, JWTs, cookies and `key=value` secrets. The token is
never written to logs, output files or the database.

## Providers and their limitations

Endpoints were researched and verified against the live API on 2026-10-02:

| Endpoint | Used for | Notes |
|---|---|---|
| `POST api.minecraftservices.com/minecraft/profile/lookup/bulk/byname` | Main scan path | ≤10 names per request. Returns only existing profiles (case-insensitive, canonical spelling) |
| `GET api.minecraftservices.com/minecraft/profile/lookup/name/{name}` | Single checks, self-test | `200 {id,name}` or `404 {path,errorMessage}` |
| `GET api.minecraftservices.com/minecraft/profile/name/{name}/available` | `--verify` | Bearer token; `AVAILABLE`, `DUPLICATE` or `NOT_ALLOWED`; 20 requests / 5 min per account |
| `api.mojang.com/users/profiles/minecraft/{name}` | Not used | Legacy host, documented to return sporadic 403s |
| `api.mojang.com/user/profiles/{uuid}/names` | Not used | **Removed** on 2022-09-13 (no official name history) |

All URLs are configurable in case Mojang moves them. A 404 is only treated as "no such
profile" when it carries Mojang's JSON error body. Anything else is an ERROR, never a false
"available".

- **Minecraft** (primary). Without a token, results are *unverified*. `--verify` uses a
  Minecraft Services access token **for your own account**, supplied through
  `MINECRAFT_ACCESS_TOKEN`. The tool doesn't log in for you. Tokens expire (typically after
  about a day), and an expired one simply disables verification for the run. Verification is
  slow by design (20 checks / 5 min).
- **NameMC** (optional enrichment, **off by default**). It is only asked about names Mojang
  reports as unclaimed, to find "Available Later" estimates. It checks `robots.txt` first
  (currently `/search` is allowed, `/minecraft-names?` is not), sends at most one request per
  10 seconds, and **never tries to get past Cloudflare, CAPTCHAs or other anti-bot measures**.
  A challenge, or 3 failures in a row, disables it for the session. NameMC actively blocks
  automated clients (our research requests received HTTP 403), so expect it to disable itself.
  NameMC is a third party and not authoritative: review its terms of service before enabling
  it with `--namemc` or `providers.namemc.enabled = true`. When it disagrees with Mojang, the
  result is UNKNOWN; the tool never guesses.
- **Demo** (offline). Deterministic fake results for trying the UI, caching and resume
  without sending any traffic.

To add a provider, subclass `minecraft_finder.providers.base.Provider`. Implement `info()` and
`check()`, and optionally `check_many()`, `start()`, `self_test()` and `close()`. Then register
it in `providers/__init__.py`.

## Project layout

```text
minecraft-name-finder/
├── pyproject.toml
├── README.md
├── config.example.toml
├── Start.bat / start.sh  # double-click launchers (set themselves up on first start)
├── examples/words-sample.txt
├── src/minecraft_finder/
│   ├── cli.py            # Typer commands
│   ├── menu.py           # simple numbered menu for non-technical users
│   ├── keepawake.py      # stops the PC from sleeping during a scan
│   ├── scanlock.py       # one scan per database at a time
│   ├── namefilter.py     # flags names Mojang's word filter would refuse
│   ├── wordsource.py     # downloads the English word list
│   ├── scanner.py        # async pipeline: producer → queue → workers → result handler
│   ├── validator.py      # Minecraft username rules (single source of truth)
│   ├── wordlist.py       # streaming reader, transforms, filters, fingerprints
│   ├── rate_limit.py     # adaptive async token bucket
│   ├── retry.py          # backoff, jitter, Retry-After parsing
│   ├── http_client.py    # rate-limited, retrying request executor
│   ├── database.py       # SQLite cache, jobs, resumable progress
│   ├── availability.py   # 37-day model, estimates, status wording
│   ├── ranking.py        # local quality score
│   ├── output.py         # crash-safe output files
│   ├── ui.py             # Rich dashboard, panels, summaries
│   ├── config.py         # TOML config with validation
│   ├── logging_setup.py  # rotating log file + redaction
│   ├── models.py         # CheckResult, Status, Confidence
│   └── providers/        # base.py, minecraft.py, namemc.py, demo.py
├── tests/                # 286 offline tests (mocked HTTP, fake clocks, scripted menu input)
└── data/                 # results.db is created here
```

```text
WordList ─► Validator ─► SQLite staging (dedupe) ─► Producer ─(cache hit)─────────────┐
                                                       │                              ▼
                                                  asyncio.Queue ─► Workers ─► Result handler ─► SQLite + output files + dashboard
                                                                     │
                                                    Rate limiter ─► Provider (bulk lookup, retries, 429 handling)
```

## Development

```bash
pip install -e ".[dev]"
pytest
```

The test suite runs in under 2 seconds and never contacts live services. HTTP is mocked with
`httpx.MockTransport`, and the rate limiter and retries run on a fake clock. It covers
validation, transforms, the rate limiter, retry and 429 handling, the cache, providers, output
writers, availability estimates, the scanner (including cancellation and resume) and the CLI.

## Responsible use

- This project is **not affiliated with or endorsed by Mojang Studios or Microsoft**.
  "Minecraft" is a trademark of Mojang Synergies AB.
- Use it to find a name **for yourself**, and follow the Minecraft EULA, the Minecraft Usage
  Guidelines and the Microsoft Services Agreement. Mojang's terms don't allow selling or
  transferring accounts, so don't use this tool to hoard or resell names.
- Keep the conservative defaults. Never evade rate limits: no parallel instances, rotating
  IPs or proxies to go faster. Heavy automated traffic can get your IP or account restricted.
- Only use an access token for your own account, and never share it.
- Results are a snapshot. A name can be claimed by someone else between the check and your
  attempt, and "estimated" release times can be wrong. There is no warranty.

## Credits

Made by **sheepex_**.
