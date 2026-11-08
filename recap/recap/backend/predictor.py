"""
Live understanding layer: reference material, predictive captions, AI notes.

Three jobs, each with an instant offline path and a Gemini path:

1. ContextLibrary  - lesson/reference material (PDF, .txt, .md). Builds a
                     glossary, a small n-gram model of how the material is
                     phrased, and passages for retrieval.
2. Prediction      - when Alex stutters, stalls or trails off, guess the rest
                     of the sentence. Offline: finish a half-said word from the
                     glossary, then extend with the n-gram model (instant).
                     Gemini: refine with the lesson context (~1 s), replacing
                     the offline guess if it's confident.
3. Notes           - AI note-taker: topic, key points, key terms, links to the
                     reference material, check-for-understanding questions.

Honesty rule: a prediction is a *suggestion*. The caption shows his own words
(stutters removed) and the prediction separately, styled differently. Only
his own words ever reach the transcript and the end-of-day recap.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import config
import gemini
from speech_symptoms import _WORD, DANGLING, clean, is_incomplete, tokens

_TOKEN_RE = _WORD.pattern

STOP = DANGLING | {
    "i", "me", "it", "he", "she", "them", "him", "what", "how", "why", "all", "any", "no", "yes",
    "just", "only", "out", "up", "down", "over", "under", "again", "one", "two", "three", "such",
    "may", "might", "must", "shall", "being", "having", "did", "doing", "here", "there", "now",
    "okay", "ok", "today", "let's", "lets", "going", "get", "got", "go", "know", "think", "see",
    "look", "right", "well", "really", "thing", "things", "people", "also", "typically", "tends",
    "available", "internet", "from", "into", "include", "including", "type", "types",
}
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n{2,}|•|•")


def _content(w: str) -> bool:
    raw = w.strip("'-")
    if len(raw) >= 2 and raw.isupper() and raw.isalpha():   # acronyms and numerals: CN, III, ATP
        return True
    w = raw.lower()
    return len(w) >= 3 and w not in STOP and not w.isdigit()


def _stem(w: str) -> str:
    w = w.lower().strip("'-")
    for suf in ("ies", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)]
    return w


def _tidy(text: str) -> str:
    text = text.replace("�", " ").replace("­", "")
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)          # hyphenated line breaks
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _drop_bibliography(text: str) -> str:
    m = re.search(r"\n\s*(bibliography|references|works cited)\s*\n", text, flags=re.I)
    return text[: m.start()] if m else text


def read_document(path: Path) -> Optional[str]:
    suffix = path.suffix.lower()
    try:
        if suffix in (".txt", ".md"):
            return path.read_text(encoding="utf-8", errors="replace")
        if suffix == ".pdf":
            return pdf_text(path.read_bytes())
    except OSError as e:
        print(f"[context] can't read {path}: {e}")
    return None


def pdf_text(data: bytes) -> Optional[str]:
    try:
        import io

        from pypdf import PdfReader  # pip install pypdf
    except ImportError:
        print("[context] pypdf not installed; PDF skipped (pip install pypdf)")
        return None
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception as e:  # malformed PDFs raise many different things
        print(f"[context] PDF unreadable: {e}")
        return None


# ---------------------------------------------------------------- library
@dataclass
class Doc:
    title: str
    text: str
    source: str = "upload"


@dataclass
class ContextLibrary:
    docs: list[Doc] = field(default_factory=list)
    glossary: dict[str, str] = field(default_factory=dict)   # term -> one-line definition
    glossary_source: str = "none"

    def __post_init__(self):
        self._rebuild()

    # ---- loading
    def load_dir(self, folder: Path) -> int:
        n = 0
        if not folder.is_dir():
            return 0
        for p in sorted(folder.iterdir()):
            if p.name.startswith(".") or p.name.lower() == "readme.md":
                continue
            text = read_document(p)
            if text and text.strip():
                self.docs.append(Doc(p.stem.replace("_", " "), _tidy(text), str(p.name)))
                n += 1
        self._rebuild()
        return n

    def add(self, title: str, text: str, source: str = "upload") -> Doc:
        d = Doc(title.strip() or "Untitled", _tidy(text), source)
        self.docs.append(d)
        self._rebuild()
        return d

    def clear(self) -> None:
        self.docs.clear()
        self.glossary.clear()
        self.glossary_source = "none"
        self._rebuild()

    # ---- derived structures
    def _rebuild(self) -> None:
        self.sentences: list[str] = []
        for d in self.docs:
            body = _drop_bibliography(d.text)
            for chunk in _SENT_SPLIT.split(body):
                s = " ".join(chunk.split())
                if len(tokens(s)) >= 3:
                    self.sentences.append(s)
        # passages: 2 sentences each, overlapping, for retrieval
        self.passages = [" ".join(self.sentences[i:i + 2]) for i in range(0, len(self.sentences), 1)] or []
        df: Counter = Counter()
        for p in self.passages:
            df.update({_stem(w) for w in tokens(p) if _content(w)})
        n = max(len(self.passages), 1)
        self.idf = {w: math.log(1 + n / c) for w, c in df.items()}

        self.known: set[str] = set()
        self.bigram: dict[str, Counter] = defaultdict(Counter)
        self.trigram: dict[tuple[str, str], Counter] = defaultdict(Counter)
        for s in self.sentences:
            self.learn(s, rebuild_terms=False)
        self._auto_terms()

    def learn(self, sentence: str, rebuild_terms: bool = False) -> None:
        """Add a sentence to the phrase model (reference text, or his own finished sentences)."""
        # keep commas/semicolons/colons as a "," token so a prediction stops at a clause boundary
        toks = [t.lower().strip("'") if t not in ",;:" else "," for t in re.findall(r"[,;:]|" + _TOKEN_RE, sentence)]
        toks = [t for t in toks if t and t != "…" and not t.endswith("-")]
        if not toks:
            return
        self.known.update(t for t in toks if t != ",")
        seq = ["<s>", "<s>"] + toks + ["</s>"]
        for i in range(2, len(seq)):
            self.bigram[seq[i - 1]][seq[i]] += 1
            self.trigram[(seq[i - 2], seq[i - 1])][seq[i]] += 1
        if rebuild_terms:
            self._auto_terms()

    def _auto_terms(self) -> None:
        """Offline glossary candidates: frequent content words and repeated phrases."""
        uni: Counter = Counter()
        phr: Counter = Counter()
        for s in self.sentences:
            toks = [t for t in tokens(s) if t != "…"]
            for t in toks:
                if _content(t) and len(t) >= 5:
                    uni[t.lower()] += 1
            for clause in re.split(r"[(),;:]", s):   # phrases don't cross brackets or commas
                ctoks = [t for t in tokens(clause) if t != "…"]
                for n in (2, 3):
                    for i in range(len(ctoks) - n + 1):
                        g = ctoks[i:i + n]
                        if all(_content(x) for x in g):
                            phr[" ".join(x.lower() for x in g)] += 1
            for m in re.finditer(r"\(([A-Z]{2,}(?: [IVX]+)?)\)", s):   # acronyms like (CN III)
                phr[m.group(1)] += 2
        counts = {p: c for p, c in phr.items() if c >= 2}
        counts.update({w: c for w, c in uni.most_common(60)
                       if c >= 2 and len(w) >= 7 and not any(ch.isdigit() for ch in w)})
        # drop a phrase that only ever appears inside a longer one ("medial rectus" in "medial rectus muscle")
        out = []
        for t in sorted(counts, key=lambda t: (-len(t.split()), -counts[t], t)):
            inside = [o for o in out if re.search(rf"\b{re.escape(t.lower())}\b", o.lower())]
            if not any(counts[o] >= counts[t] for o in inside):
                out.append(t)
        self.terms = out

    # ---- queries
    @property
    def empty(self) -> bool:
        return not self.docs

    def all_terms(self) -> list[str]:
        return list(dict.fromkeys(list(self.glossary) + self.terms))

    def keyterms(self, limit: int = 40) -> list[str]:
        """Short terms worth boosting in speech recognition."""
        return [t for t in self.all_terms() if len(t) <= 20][:limit]

    def relevant(self, text: str, k: int = 3) -> list[str]:
        q = {_stem(w) for w in tokens(text) if _content(w)}
        if not q or not self.passages:
            return []
        scored = []
        for i, p in enumerate(self.passages):
            words = {_stem(w) for w in tokens(p) if _content(w)}
            s = sum(self.idf.get(w, 0) for w in q & words)
            if s > 0:
                scored.append((s, i))
        scored.sort(reverse=True)
        out, used = [], set()
        for _, i in scored:
            if i in used or i - 1 in used or i + 1 in used:
                continue
            used.add(i)
            out.append(self.passages[i])
            if len(out) >= k:
                break
        return out

    def define(self, term: str) -> Optional[str]:
        for t, d in self.glossary.items():
            if t.lower() == term.lower():
                return d
        low = term.lower()
        best, best_score = None, 0.0
        for s in self.sentences:
            sl = s.lower()
            if low not in sl:
                continue
            score = 1.0
            if re.search(re.escape(low) + r"[^.]{0,40}\b(is|are|controls?|occurs?|causes?|responsible|refers)\b", sl):
                score += 2
            if sl.startswith(low):
                score += 1
            if re.match(r"(these|this|it|they|that|those)\b", sl):
                score -= 2.5   # "These muscles control..." doesn't stand alone
            if any(len(t) > len(low) and low in t.lower() and t.lower() in sl for t in self.terms):
                score -= 1.5   # really about a longer term ("oculomotor nerve palsy")
            score -= len(s) / 400
            if score > best_score:
                best, best_score = s, score
        return best

    def display(self, term: str) -> str:
        """The term as it is written in the material ("CN III palsy", not "cn iii palsy")."""
        for s in self.sentences:
            m = re.search(rf"\b{re.escape(term)}\b", s, flags=re.I)
            if m:
                found = m.group(0)
                return found[0].upper() + found[1:] if found.islower() else found
        return term

    def hint_terms(self) -> list[str]:
        """Terms worth a definition card: the curated glossary, else multi-word phrases and acronyms."""
        if self.glossary:
            return list(self.glossary)
        return [t for t in self.terms if " " in t or t.isupper()]

    def find_terms(self, text: str) -> list[str]:
        """Hint-worthy terms mentioned in `text`, longest first."""
        low = " " + " ".join(t.lower() for t in tokens(text)) + " "
        found = []
        for t in sorted(self.hint_terms(), key=len, reverse=True):
            if f" {t.lower()} " in low and not any(t.lower() in f.lower() for f in found):
                found.append(t)
        return found

    def summary(self) -> dict:
        return {"docs": [{"title": d.title, "source": d.source, "words": len(d.text.split())} for d in self.docs],
                "terms": self.all_terms()[:40], "glossary_source": self.glossary_source}

    # ---- Gemini enrichment (one call per material change)
    def enrich_with_gemini(self) -> bool:
        if self.empty or not gemini.enabled():
            return False
        material = "\n\n".join(f"# {d.title}\n{_drop_bibliography(d.text)}" for d in self.docs)[:12000]
        out = gemini.generate_json(
            "Extract a glossary from this teaching material for a live-caption system.\n"
            "Return JSON {\"terms\": [{\"term\": str, \"definition\": str}]} with 8-20 key terms "
            "(anatomy, conditions, processes, acronyms). Definitions: one plain sentence, max 20 words, "
            "taken only from the material.\n\n" + material, timeout=40)
        terms = (out or {}).get("terms") or []
        g = {}
        for t in terms:
            if isinstance(t, dict) and t.get("term") and t.get("definition"):
                g[str(t["term"]).strip()] = str(t["definition"]).strip()
        if g:
            self.glossary = g
            self.glossary_source = "gemini"
            return True
        return False


# ------------------------------------------------------------- prediction
@dataclass
class Prediction:
    clean: str                 # his words, stutters removed (and any half-word dropped)
    predicted: str = ""        # suggested rest of the sentence
    confidence: float = 0.0
    source: str = "none"       # offline | gemini | none
    disfluency: Optional[dict] = None

    def as_dict(self) -> dict:
        return {"clean": self.clean, "predicted": self.predicted, "confidence": round(self.confidence, 2),
                "source": self.source}


def _complete_fragment(frag: str, lib: ContextLibrary) -> Optional[str]:
    f = frag.lower().strip("-'")
    cut = frag.endswith("-")       # "vi-": he was clearly cut off mid-word
    if len(f) < (2 if cut else 3) or (f in lib.known and not cut):
        return None
    candidates: Counter = Counter()
    for s in lib.sentences:
        for t in tokens(s):
            tl = t.lower()
            if tl.startswith(f) and len(tl) > len(f):
                candidates[tl] += 1
    return candidates.most_common(1)[0][0] if candidates else None


def _extend(prev2: str, prev1: str, lib: ContextLibrary, max_words: int = 5) -> tuple[list[str], float]:
    words: list[str] = []
    support = []
    a, b = prev2, prev1
    for _ in range(max_words):
        tri = lib.trigram.get((a, b))
        nxt, conf = None, 0.0
        if tri:
            nxt, c = tri.most_common(1)[0]
            conf = c / sum(tri.values())
        elif lib.bigram.get(b) and (b not in STOP or not words):
            bi = lib.bigram[b]
            nxt, c = bi.most_common(1)[0]
            conf = 0.6 * c / sum(bi.values())
        if not nxt or nxt in ("</s>", ",") or conf < 0.15:
            break
        if words.count(nxt) >= 2:
            break
        words.append(nxt)
        support.append(conf)
        a, b = b, nxt
    while words and words[-1] in DANGLING:   # don't end a suggestion on "the"
        words.pop()
        support.pop()
    if not words:
        return [], 0.0
    return words, sum(support) / len(support)


def offline_predict(text: str, lib: ContextLibrary, stalled: bool = False) -> Prediction:
    cleaned, d = clean(text)
    p = Prediction(clean=cleaned, disfluency=d.as_dict())
    toks = tokens(cleaned)
    if not toks or lib.empty and not lib.bigram:
        return p
    last = toks[-1]
    pred_words: list[str] = []
    conf_parts: list[float] = []

    word = _complete_fragment(last, lib) if (last.endswith("-") or stalled or is_incomplete(cleaned)
                                              or last.lower() not in lib.known) else None
    if word:
        # replace the half-word with the full word as the first predicted word
        p.clean = cleaned[: cleaned.rfind(last)].rstrip()
        toks = toks[:-1]
        pred_words.append(word)
        conf_parts.append(0.7)
    elif not (stalled or is_incomplete(cleaned)):
        return p

    ctx = ["<s>", "<s>"] + [t.lower() for t in toks] + [w.lower() for w in pred_words]
    ext, c = _extend(ctx[-2], ctx[-1], lib)
    pred_words += ext
    if ext:
        conf_parts.append(c)
    if not pred_words:
        return p
    p.predicted = " ".join(pred_words)
    p.confidence = min(0.85, sum(conf_parts) / len(conf_parts))
    p.source = "offline"
    return p


def _strip_overlap(spoken: str, completion: str) -> str:
    """If the model repeated the end of what he said, keep only the new words."""
    s = [w.lower().strip(".,") for w in spoken.split()]
    c = completion.split()
    cl = [w.lower().strip(".,") for w in c]
    for n in range(min(len(s), len(c)), 0, -1):
        if s[-n:] == cl[:n]:
            return " ".join(c[n:])
    return completion


def gemini_predict(raw: str, pred: Prediction, lib: ContextLibrary, recent: list[str],
                   topic: str = "") -> Optional[Prediction]:
    """Refine the guess with Gemini using the lesson context. None if unavailable/unsure."""
    passages = lib.relevant(" ".join(recent[-2:] + [pred.clean]), k=3)
    prompt = (
        "You assist live captions for a high-school teacher with ALS. His speech can stutter, slow down, "
        "stop mid-word or trail off when he is tired. Predict how he intends to finish the CURRENT utterance.\n"
        "Rules: continue from exactly where he stopped (if he stopped mid-word, start with the full word); "
        "max 12 words; stay faithful to the lesson material and what he has been saying; do not add new claims; "
        "if the utterance is already complete, return an empty completion.\n"
        "Return JSON {\"completion\": str, \"confidence\": number 0-1}.\n\n"
        + (f"Lesson topic: {topic}\n" if topic else "")
        + ("Lesson material:\n- " + "\n- ".join(passages) + "\n\n" if passages else "")
        + ("He just said:\n" + "\n".join(recent[-5:]) + "\n\n" if recent else "")
        + f"CURRENT utterance (raw): {raw}\nCURRENT utterance (stutters removed): {pred.clean}\n"
    )
    out = gemini.generate_json(prompt, model=config.GEMINI_LIVE_MODEL, fallback_model=config.GEMINI_MODEL,
                               timeout=6, temperature=0.1)
    if not out:
        return None
    completion = str(out.get("completion") or "").strip().strip('"')
    try:
        conf = float(out.get("confidence", 0))
    except (TypeError, ValueError):
        conf = 0.0
    # Gemini sees the half-word too; drop it from the spoken part if Gemini completed it
    spoken = pred.clean
    last = spoken.split()[-1] if spoken.split() else ""
    raw_last = tokens(raw)[-1] if tokens(raw) else ""
    frag = raw_last.lower().strip("-")
    if completion and frag and len(frag) >= 2 and completion.split()[0].lower().startswith(frag) \
            and completion.split()[0].lower() != frag and not spoken.lower().endswith(completion.split()[0].lower()):
        if last.lower().strip("-") == frag:
            spoken = spoken[: spoken.rfind(last)].rstrip()
    completion = _strip_overlap(spoken, completion)
    words = completion.split()
    if len(words) > 14:
        completion = " ".join(words[:14])
    return Prediction(clean=spoken, predicted=completion, confidence=conf, source="gemini",
                      disfluency=pred.disfluency)


# ------------------------------------------------------------------ notes
def _offline_notes(lines: list[str], lib: ContextLibrary) -> dict:
    text = " ".join(lines)
    counts: Counter = Counter()
    low = text.lower()
    for t in lib.all_terms():
        c = low.count(t.lower())
        if c:
            counts[t] += c * (1 + len(t.split()))
    top: list[str] = []
    for t, _ in counts.most_common():
        if not any(t.lower() in o.lower() or o.lower() in t.lower() for o in top):
            top.append(t)
        if len(top) >= 8:
            break
    topic = top[0].title() if top else (lib.docs[0].title if lib.docs else "Today's lesson")
    points = []
    for line in reversed(lines):
        c, _ = clean(line)
        if any(t.lower() in c.lower() for t in top) and len(c.split()) >= 5 and c not in points:
            points.append(c)
        if len(points) >= 5:
            break
    return {
        "topic": topic,
        "summary": list(reversed(points)),
        "key_terms": [{"term": t, "definition": (lib.define(t) or "")[:180]} for t in top[:6]],
        "related": [{"title": "From your materials", "detail": p[:240]} for p in lib.relevant(" ".join(lines[-3:]), 2)],
        "questions": [],
        "source": "offline",
    }


def build_notes(lines: list[str], lib: ContextLibrary, previous: Optional[dict] = None) -> dict:
    if not lines:
        return {"topic": "", "summary": [], "key_terms": [], "related": [], "questions": [], "source": "empty"}
    if not gemini.enabled():
        return _offline_notes(lines, lib)
    material = "\n\n".join(f"# {d.title}\n{_drop_bibliography(d.text)}" for d in lib.docs)[:8000]
    transcript = "\n".join(lines[-80:])
    prompt = (
        "You are an AI note-taker for a high-school biology class taught by a teacher with ALS. "
        "His live transcript may contain stutters, repeated words and unfinished sentences; read through them "
        "for meaning. Write notes a student could study from.\n"
        "Return JSON with keys:\n"
        " topic: short title of what is being discussed now\n"
        " summary: 3-7 bullet strings, the key points so far, in plain words\n"
        " key_terms: [{term, definition}] up to 8, definitions max 20 words, consistent with the material\n"
        " related: [{title, detail}] up to 3 facts from the reference material that connect to what he is "
        "discussing right now (e.g. a symptom, cause or structure he hasn't covered yet)\n"
        " questions: up to 3 short check-for-understanding questions\n"
        "Only use what is in the transcript or the reference material.\n\n"
        + (f"Reference material:\n{material}\n\n" if material else "")
        + (f"Previous notes topic: {previous.get('topic')}\n" if previous and previous.get("topic") else "")
        + f"Transcript so far:\n{transcript}\n"
    )
    out = gemini.generate_json(prompt, timeout=40, fallback_model=config.GEMINI_LIVE_MODEL)
    if not out:
        return _offline_notes(lines, lib)

    def lst(key, n):
        v = out.get(key) or []
        return v[:n] if isinstance(v, list) else []

    return {
        "topic": str(out.get("topic") or "").strip(),
        "summary": [str(x) for x in lst("summary", 7)],
        "key_terms": [x for x in lst("key_terms", 8) if isinstance(x, dict) and x.get("term")],
        "related": [x for x in lst("related", 3) if isinstance(x, dict)],
        "questions": [str(x) for x in lst("questions", 3)],
        "source": "gemini",
    }


def context_hint(text: str, lib: ContextLibrary, notes: Optional[dict], recently_shown: list[str]) -> Optional[dict]:
    """A one-line 'what this term means' card for a term he just used."""
    for term in lib.find_terms(text):
        if term.lower() in (t.lower() for t in recently_shown):
            continue
        definition = None
        for kt in (notes or {}).get("key_terms") or []:
            if str(kt.get("term", "")).lower() == term.lower() and kt.get("definition"):
                definition = str(kt["definition"])
        definition = definition or lib.define(term)
        if definition:
            if len(definition) > 160:
                definition = definition[:157].rsplit(" ", 1)[0] + "…"
            return {"term": lib.display(term), "text": definition}
    return None
