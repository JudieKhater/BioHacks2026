# Recap

**A voice that lasts until 3:30.** BioHacks 2026 · Wearable Devices and Quality of Life

Recap is a teacher's lanyard for Alex, a biology teacher with ALS whose speech is clear at 9 am
(165 words per minute, 98 % intelligible) and tired by 4 pm (118 wpm, 91 %). A small throat sensor
and a lapel mic notice when his voice is tiring and quietly bring up live captions of **his own words**
on the classroom screen, so the back row stops asking him to repeat himself. It never speaks for him.

- **Captions only when he needs them.** Auto mode turns them on when his fatigue score rises; he can
  force them on or off at any time.
- **Speech battery.** He sees how much voice he has left today, so he can plan, not push.
- **The recap comes back.** At the end of the day he ticks the sentences he said that matter most and
  publishes them for students: his exact words, chosen by him.
- **Morning breathing trainer.** Guides expiratory muscle strength training (EMST) reps, the one
  bulbar/respiratory exercise with randomized-trial evidence in ALS, if his therapist prescribes it.
- **Clinic view.** Daily speech-rate, loudness and words-per-breath trends between his 3-monthly visits,
  shared only while he allows it.

We deliberately **do not** include tongue, lip or jaw strengthening: ALS speech guidance advises against
it. Energy conservation is the goal.

---

## Quick start (any laptop, 5 minutes)

```bash
git clone <this repo> && cd recap
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                  # fill in keys later; works without them
uvicorn backend.app:app --reload --host 0.0.0.0 --port 8000
```

Open http://localhost:8000. In a second terminal, run the demo with no hardware:

```bash
python client/simulator.py --history 21     # 3 weeks of past data for the clinic charts
python client/simulator.py --day --captions # Alex's school day in 2 minutes
python client/simulator.py --breath         # breathing-trainer reps (open /trainer.html first)
```

Put `/captions.html` on one screen and `/alex.html` on another: around "2 pm" the battery drops below
half and captions fade in.

With a real mic:

```bash
python client/audio_client.py --list-devices
python client/audio_client.py --device <index>         # needs ELEVENLABS_API_KEY in .env
python client/audio_client.py --no-asr                 # no key: features only, no captions
```

With the ESP32 badge: flash `firmware/recap_badge/recap_badge.ino`, then
`python client/serial_bridge.py --port <port>`.

Run tests: `python -m pytest -q`

---

## How it works

```
 Lapel mic ──► audio_client.py ──► features every 5 s ──► POST /api/features ─┐
     │                                                                        ▼
     └──► ElevenLabs Scribe v2 Realtime ──► POST /api/captions        FatigueEngine
                                                                     (baseline, score,
 ESP32 badge ─► serial_bridge.py ─► POST /api/sensor (throat effort)   hysteresis, mode)
            └─────────────────────► POST /api/emst/sample                     │
                                                                              ▼
            Tiger Data / SQLite (numbers only) ◄──────────────────────── backend/app.py
                                                                              │
          /captions.html  /alex.html  /trainer.html  /recap.html  /clinic.html ◄┘ (WebSockets + REST)
```

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
sentences verbatim, so the model cannot change a word. With no key, an offline picker uses the
biology keyterms.

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
- **Gemini:** recap sentence selection (numbers only) and the plain-language clinic summary.
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
- The breathing trainer is a prototype, not a calibrated EMST device; resistance must be set by a clinician.
- A real product would need a clinical pilot and would likely be regulated as a medical device by Health Canada.
