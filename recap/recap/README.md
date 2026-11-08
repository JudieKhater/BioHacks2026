# Recap

**A voice that lasts until 3:30.** BioHacks 2026 · Wearable Devices and Quality of Life

Recap is a teacher's lanyard for Alex, a biology teacher with ALS whose speech is clear at 9 am
(165 words per minute, 98 % intelligible) and tired by 4 pm (118 wpm, 91 %). A small throat sensor
and a lapel mic notice when his voice is tiring and quietly bring up live captions of **his own words**
on the classroom screen, so the back row stops asking him to repeat himself. It never speaks for him.

- **Captions only when he needs them.** Auto mode turns them on when his fatigue score rises; he can
  force them on or off at any time.
- **Captions over everything.** A desktop overlay puts the captions on the bottom edge of the screen,
  black text on a white highlight, on top of slides, videos or any app. No web page needs to be open.
- **Predictive captions.** When he stutters, stalls or trails off, the caption shows his words with the
  stutter removed, then a suggested ending in italics after an arrow (`So the oculomotor nerve controls
  the → medial rectus muscle`). It's grounded in the lesson material, clearly marked as a guess, never
  spoken aloud, and never added to the recap. He can turn it off.
- **AI note-taker.** Live notes while he teaches: topic, key points, key terms, facts from the lesson
  material that connect to what he's saying, and check-for-understanding questions. When he uses a key
  term, a one-line definition appears above the captions.
- **Speech check and guidance.** Live flags for the four speech subsystems (Green et al. 2013) plus
  stutters and trailing sentences, with energy-conserving tips for right now.
- **Sessions on his terms.** The mic stays off until he presses **Start session**. **End session** turns
  it off and builds a report: how his speech changed from the start to the end of the session, how it
  compares with his recent sessions, guidance for now, a breathing plan built from that session, and
  anything worth raising with his clinic. Every session is saved, so the report page charts his trend
  over weeks.
- **Speech battery.** He sees how much voice he has left today, so he can plan, not push.
- **The recap comes back.** At the end of the day he ticks the sentences he said that matter most and
  publishes them for students: his exact words, chosen by him.
- **Morning breathing trainer.** Guides expiratory muscle strength training (EMST) reps, the one
  bulbar/respiratory exercise with randomized-trial evidence in ALS, if his therapist prescribes it.
- **Clinic view.** Daily speech-rate, loudness and words-per-breath trends between his 3-monthly visits,
  and his speaking-rate slope compared with published ALS decline rates (Eshghi et al. 2022), shared only
  while he allows it.

We deliberately **do not** include tongue, lip or jaw strengthening: ALS speech guidance advises against
it. Energy conservation is the goal.

---

## Quick start (any laptop, 5 minutes)

Needs Python 3.10 or newer. API keys are optional: without them, captions, predictions and notes use
offline methods.

**Windows:** clone the repo, then double-click `setup.bat`. It creates `.venv`, installs the
requirements, creates `.env` from `.env.example` and asks for your Gemini and ElevenLabs keys (press
Enter to skip). Then double-click `start_session.bat demo` and you're done. To run each part yourself:

```bat
.venv\Scripts\activate
uvicorn backend.app:app --reload --host 0.0.0.0 --port 8000
```

**macOS / Linux:**

```bash
git clone <this repo> recap && cd recap
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # optional: add GEMINI_API_KEY / ELEVENLABS_API_KEY
uvicorn backend.app:app --reload --host 0.0.0.0 --port 8000
```

Open http://127.0.0.1:8000. Open a second terminal in the repo folder, activate the venv again
(`.venv\Scripts\activate` on Windows, `source .venv/bin/activate` on macOS/Linux), and run the demo
with no hardware:

```bash
python client/overlay.py                    # captions bar over the whole screen (keep it running)
python client/simulator.py --lesson         # CN III lesson with stutters and stalls: predictions,
                                            # term definitions, live notes (open /notes.html)
python client/simulator.py --history 21     # 3 weeks of past data for the clinic charts
python client/simulator.py --day --captions # Alex's school day in 2 minutes
python client/simulator.py --breath         # breathing-trainer reps (open /trainer.html first)
```

Put `/captions.html` on one screen and `/alex.html` on another: around "2 pm" the battery drops below
half and captions fade in. The overlay follows the same Off / Auto / On control.

Use `127.0.0.1`, not `localhost`, for `RECAP_SERVER`: on Windows `localhost` tries IPv6 first and adds
about 2 seconds to every request, which delays every caption.

### Caption overlay (`client/overlay.py`)
Uses Python's built-in tkinter; nothing extra to install. Click-through on Windows, so it never blocks
the mouse. Stop it with Ctrl+C in its terminal.

```bash
python client/overlay.py --demo                 # preview the look without a server
python client/overlay.py --scale 1.3 --lines 2  # bigger text, two lines
python client/overlay.py --no-hints             # no term definitions above the captions
python client/overlay.py --movable              # drag to move, double-click to close
python client/overlay.py --server https://yourdomain.co
```

### Lesson material (`context/`)
Drop PDFs, `.txt` or `.md` files in `context/`, or add them on the Notes page. They boost speech
recognition for subject terms, ground the predictions, give the term definitions, and feed the notes.
The demo file is the Group 5 A&P infographic on oculomotor nerve palsy.

With a real mic (Windows: just double-click `start_session.bat`; it opens the server, the overlay and
the mic client, each in its own window; `start_session.bat demo` plays the lesson instead). The mic
stays off until you press **Start session** on the Alex or Notes page:

```bash
python client/audio_client.py --captions on            # switch captions on and start listening
python client/audio_client.py --list-devices
python client/audio_client.py --device <index>
python client/audio_client.py --no-asr                 # features only, no captions
```

Speech-to-text uses ElevenLabs Scribe (word by word) if `ELEVENLABS_API_KEY` is set, otherwise Gemini
(`GEMINI_STT_MODEL`, default `gemini-3.5-transcribe`): the mic stream is cut into phrases at his pauses
and each phrase is captioned about 1-2 s after he pauses. A phrase he breaks off mid-sentence becomes an
unfinished caption, which is what triggers the predicted ending. Without `--captions on`, Auto mode keeps
captions hidden until his fatigue score rises.

With the ESP32 badge: flash `firmware/recap_badge/recap_badge.ino`, then
`python client/serial_bridge.py --port <port>`.

Run tests: `python -m pytest -q`

---

## How it works

```
 Lapel mic ──► audio_client.py ──► features every 5 s ──► POST /api/features ─┐
     │                                                                        ▼
     └──► ElevenLabs Scribe v2 Realtime ──► POST /api/captions        FatigueEngine
                                                │                    (baseline, score,
                                                ▼                     hysteresis, mode)
                       stutter cleanup ─► stall? ─► offline guess (instant)   │
                       (speech_symptoms)            └─► Gemini guess (~1 s) │
                                                │                             │
 context/ lesson PDFs ─► ContextLibrary ────────┤ glossary, n-grams, passages │
                                                ▼                             ▼
 ESP32 badge ─► serial_bridge.py ─► /api/sensor, /api/emst/sample ─► backend/app.py ─► Tiger Data / SQLite
                                                                              │        (numbers only)
   client/overlay.py (desktop captions bar) ◄── /ws/captions ◄────────────────┤
   /captions.html /notes.html /alex.html /trainer.html /recap.html /clinic.html ◄┘ (WebSockets + REST)
```

### Predictive captions (`backend/predictor.py`)
1. **Clean, don't rewrite.** `speech_symptoms.clean()` removes fillers (um, uh), cut-off starts
   (`oculo- oculomotor`, `n-nerve`) and repeats (`the the`), keeping his words and punctuation.
2. **Detect a stall.** A partial caption that hasn't changed for 1.2 s, or a sentence that ends on a
   word that leaves it hanging (`the`, `from`, `are`) or a half-word (`vi-`).
3. **Instant offline guess.** Finish a half-said word from the lesson glossary, then extend with an
   n-gram model of the lesson material and his own finished sentences. Stops at a clause boundary.
4. **Gemini refinement.** `GEMINI_LIVE_MODEL` (default `gemini-3.1-flash-lite`, about 1 s) gets the
   relevant lesson passages and his last few sentences and returns `{completion, confidence}`.
   Shown only above `PREDICT_MIN_CONFIDENCE`. If that model is overloaded it falls back to
   `GEMINI_MODEL`; calls are throttled (`GEMINI_LIVE_MIN_INTERVAL`) and paused after a rate limit,
   with the offline guess filling in.
5. **Honesty.** Predictions are separate fields, drawn in italics after an arrow, and never stored in
   the recap. "Sentences finished by prediction" is tracked as a symptom.

### Live notes
Every `NOTES_EVERY_SECONDS` while he talks, `GEMINI_MODEL` (default `gemini-3.5-flash`) turns the
transcript plus lesson material into topic, key points, key terms, related facts from the material and
check-for-understanding questions. At startup Gemini also builds a glossary from the material for the
term-definition cards. Offline, the notes and definitions come from the material directly.

### Speech check and guidance (`backend/speech_symptoms.py`)
Grounded in the two papers in the repo root:
- **Green et al. 2013** (*ALS & FTD* 14:494): speech has four subsystems. We map live measures onto them:
  respiratory = words per breath and percent pause; phonatory = loudness drop and throat effort;
  articulatory = speaking rate, stutters per 100 words, trailing sentences; resonatory = not measurable
  without a nasal/oral two-mic headset, so it says so. The weekly check lists the paper's tasks (Bamboo
  passage, sustained "ah", "Buy Bobby a puppy", "I owe you a yoyo today").
- **Eshghi et al. 2022** (*Sci Rep* 12:15713): speaking rate under **120 wpm** and intelligibility under
  **85 %** mark functional speech loss; mean decline 1.74 wpm/month (spinal onset) vs 4.16 (bulbar).
  The clinic view fits a line to his morning speaking rate, compares the slope with these, and estimates
  months to 120 wpm at that slope. Morning values are used to keep daily fatigue out of the trend.

Guidance is rule-based (not generated) so it's predictable and reviewable: voice rest, shorter
phrases, let the mic do the work, slow the start, relax the throat. No strengthening drills.

### Sessions, report and breathing plan (`backend/sessions.py`, `backend/breathing.py`)
Start session → the mic client (idle until now) opens the mic. End session → mic off, and the server
builds a report from that session's feature windows and stutter counts:
- **Start vs end** of the session (first third vs last third) for speaking rate, words per breath,
  pauses and loudness; battery; stutters; sentences finished by prediction; time under 120 wpm.
- **Compared with his last 5 sessions** (better / same / worse).
- **Guidance for now** (the rules above, applied to the end of the session).
- **Breathing plan.** The papers don't test exercises; they define what to measure and when to worry,
  and the plan is built on exactly that. Each card cites where its idea comes from:
  - *Readiness*: a tiring session (battery < 35 %, breathing alert, high fatigue score) means rest
    first and practise tomorrow morning. No training on a tired system.
  - *Recovery breaths* now.
  - *Phrase breathing*: lesson text marked with a breath every N words, N = his own words per breath
    late in the session. It is Green et al.'s passage-reading task (percent pause), so the next report
    shows whether it helps.
  - *Easy sustained "ah"* (Green et al.'s voice task), comfortable not maximal, skipped when tired.
  - *Slow starts* when his rate fell (Eshghi et al.: clarity drops quickly under 120 wpm).
  - *EMST* only if his therapist prescribed it, mornings, never after a tiring session.
- **Clinic flags**: > 25 % of the session under 120 wpm (Eshghi), and words per breath, pauses or rate
  worsening across sessions (Green: weaker breathing shows as more, longer pauses; ask about a vital
  capacity check).

Reports (numbers and guidance only, no transcript) go in the `sessions` table, and `/report.html`
charts every session with a 6-session rolling average, so mornings and afternoons balance out. The
per-day trends on the Clinic page are unchanged. `python client/simulator.py --history 21` backfills
three weeks of sessions; `--lesson` runs as a full session and ends with a report.

### The fatigue score (`backend/fatigue.py`)
1. **Baseline.** Alex presses "Set baseline now" while fresh; the next 6 speaking windows are averaged.
2. **Four deficits**, each scaled 0 to 1 against his own baseline:
   - speech rate drop (30 % slower = 1; his clinic drop of 165 → 118 is 28 %)
   - loudness drop (6 dB quieter = 1)
   - words-per-breath drop (40 % fewer = 1); an indirect daily breathing measure
   - effort rise: throat-sensor energy divided by sound output (50 % more effort for the same sound = 1)
3. Weighted average × 100, smoothed with an exponential moving average.
4. **Hysteresis:** captions turn on at 55 and only off below 40, after at least 2 minutes, so they
   never flicker mid-sentence. Mode `on`/`off` overrides everything; that's Alex's control.

All thresholds are in `.env` because a clinician would tune them.

### Audio features (`backend/features.py`)
- **Loudness:** RMS per 30 ms frame in dBFS, averaged over speech frames only.
- **Voice activity:** a frame is speech if it's 12 dB above the window's noise floor.
- **Breath groups:** speech runs split by pauses ≥ 250 ms.
- **Speech rate:** from Scribe word timestamps when available; otherwise estimated from syllable
  peaks (labelled `estimate`).
- **Confidence:** mean word probability from Scribe's `logprob`, a rough intelligibility stand-in.

### Privacy
- No audio is ever saved. Live captions are shown, not stored.
- Today's transcript sits in server memory only so Alex can build the recap; it's cleared at midnight.
- The database holds numbers, breathing reps, consent events, and recap sentences Alex approved.
- The clinic gets data only while Alex's sharing toggle is on (enforced on the server).

### Recap: his words, never rewritten (`backend/recap_ai.py`)
Gemini receives numbered sentences and returns only the **numbers** to keep. The server copies those
sentences verbatim, so the model cannot change a word. Stutters are removed first; predicted words and
unfinished sentences are never offered. With no key, an offline picker uses the subject keyterms.

---

## Team split

- **Hardware:** ESP32, MyoWare/accelerometer, pressure sensor, 3D prints, wearing safety.
  Files: `firmware/`, `client/serial_bridge.py`.
- **Signals:** mic features, fatigue tuning, Scribe captions.
  Files: `backend/features.py`, `backend/fatigue.py`, `client/audio_client.py`.
- **Backend/cloud:** server, Tiger Data, Vultr, domain, Entire, Solana.
  Files: `backend/app.py`, `backend/db.py`, `deploy/`.
- **Frontend/story:** pages, recap flow, pitch, vlog, Devpost.
  Files: `frontend/`, `backend/recap_ai.py`.

## Build order (protect the main-track demo)
1. Simulator + pages working end to end (already done: run the quick start).
2. Real mic: `audio_client.py` with Scribe captions on the projector.
3. Throat sensor through the serial bridge; check the effort ratio rises when you strain.
4. Tiger Data + Vultr + domain.
5. Breathing trainer hardware.
6. Feature freeze a few hours before submission: rehearse, record the Devpost video.
7. Solana consent anchoring only if time is left.

---

## Sponsor prizes

- **ElevenLabs:** Scribe v2 Realtime powers captions, with biology keyterms and word timestamps for
  speech rate. Used to *understand* his voice, never to clone it.
- **Tiger Data:** `speech_features` and `emst_sessions` are hypertables; clinic trends use
  `time_bucket()`; `speech_hourly` is a continuous aggregate with a refresh policy
  (`backend/schema_timescale.sql`). Set `DATABASE_URL` to your Tiger Cloud service.
- **Vultr:** hosts the backend and pages (see below).
- **Gemini:** predictive captions, live notes, lesson glossary, recap sentence selection (numbers only)
  and the plain-language clinic summary.
- **Bambu Lab:** print the lanyard badge, throat-sensor holder and breathing-trainer body.
- **Entire:** install it in this repo and code with your AI agents through it so checkpoints build up
  all weekend. Confirm judging details at their booth.
- **GoDaddy Registry:** register a domain on one of their extensions (e.g. .co, .us, .biz, .club),
  point it at the Vultr server.
- **Solana (optional):** `SOLANA_ANCHOR=1` writes a SHA-256 consent receipt (no health data) as a
  devnet memo via the `solana` CLI. Experimental: test `solana transfer --help` shows `--with-memo`
  before relying on it.

## Deploy on Vultr

```bash
# on a fresh Ubuntu instance
sudo apt update && sudo apt install -y python3-venv git caddy
sudo git clone <this repo> /opt/recap && cd /opt/recap
sudo python3 -m venv .venv && sudo .venv/bin/pip install -r requirements.txt
sudo cp .env.example .env && sudo nano .env          # DATABASE_URL, GEMINI_API_KEY, RECAP_TOKEN
sudo cp deploy/recap.service /etc/systemd/system/ && sudo systemctl enable --now recap
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile && sudo nano /etc/caddy/Caddyfile   # your domain
sudo systemctl reload caddy
```

Point your domain's A record at the server IP. On the laptops set `RECAP_SERVER=https://yourdomain.co`
and the same `RECAP_TOKEN`; open pages once as `https://yourdomain.co/alex.html?token=...`.

## Honest limitations
- Tested on teammates simulating fatigue, not on ALS speech; thresholds need clinical tuning.
- Speech recognition can be less accurate on dysarthric speech; keyterms help but don't solve it.
- Predictions can be wrong. They are marked as guesses and he can switch them off, but a confident wrong
  guess on a projector could still mislead a student; tune `PREDICT_MIN_CONFIDENCE` with real use.
- The decline projection is a straight line through noisy daily data, not a prognosis.
- Free-tier Gemini quotas are small and models get overloaded at peak times; live calls are throttled
  and everything falls back to offline methods, which are more limited.
- The breathing trainer is a prototype, not a calibrated EMST device; resistance must be set by a clinician.
- A real product would need a clinical pilot and would likely be regulated as a medical device by Health Canada.
