import os
import re

_model = None

# Phrases that usually open an ad read.
START_PATTERNS = [
    r"brought to you by",
    r"sponsored by",
    r"this episode is (sponsored|brought)",
    r"this (podcast|show) is (sponsored|supported|brought)",
    r"support for this (podcast|show|episode)",
    r"thanks to .{0,40} for sponsoring",
    r"(today's|our|this week's) sponsor",
    r"a (quick )?word from our sponsor",
]
# Phrases that usually close an ad read (offers, promo codes, URLs).
END_PATTERNS = [
    r"promo code",
    r"(use|enter) (my |the )?(offer )?code",
    r"free trial",
    r"\d+\s?(%|percent) off",
    r"dot com slash",
    r"\b[\w-]+\.com/[\w-]+",
    r"link in the (show )?notes",
    r"go to \w+ dot com",
    r"visit \w+ dot com",
    r"terms (and conditions )?apply",
]
START_RX = [re.compile(p, re.IGNORECASE) for p in START_PATTERNS]
END_RX = [re.compile(p, re.IGNORECASE) for p in END_PATTERNS]

MAX_GAP_SECONDS = 75.0      # hits further apart than this are separate ads
SPAN_SECONDS = 45.0         # assumed ad length when only the start or only the end phrase is found
MIN_AD_SECONDS = 8.0
MAX_AD_SECONDS = 240.0


def get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        _model = WhisperModel(
            os.environ.get("WHISPER_MODEL", "small.en"),
            device=os.environ.get("WHISPER_DEVICE", "cuda"),
            compute_type=os.environ.get("WHISPER_COMPUTE", "int8"),
        )
    return _model


def classify(text):
    start_weight = sum(1 for rx in START_RX if rx.search(text))
    end_weight = sum(1 for rx in END_RX if rx.search(text))
    return start_weight, end_weight


def close_block(block):
    start, end = block["start"], block["end"]
    if block["start_w"] and not block["end_w"]:
        end = max(end, start + SPAN_SECONDS)
    elif block["end_w"] and not block["start_w"]:
        if block["end_w"] < 2:
            return None  # one weak closing phrase on its own is too risky to skip
        start = max(0.0, end - SPAN_SECONDS)
    start = max(0.0, start - 2.0)
    end = end + 3.0
    if not (MIN_AD_SECONDS <= end - start <= MAX_AD_SECONDS):
        return None
    return [round(start, 1), round(end, 1)]


def merge_ranges(ranges, gap=1.0):
    """Join ranges that overlap or sit within gap seconds of each other."""
    merged = []
    for start, end in sorted(([float(a), float(b)] for a, b in ranges)):
        if merged and start <= merged[-1][1] + gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [[round(s, 1), round(e, 1)] for s, e in merged]


def tokens(text):
    return re.findall(r"[a-z0-9']+", text.lower())


def anchor_hits(segments, anchors):
    """Segments that contain a phrase learned from a hand-tagged ad."""
    hits = []
    for seg in segments:
        spoken = " " + " ".join(tokens(seg["text"])) + " "
        for a in anchors:
            if f" {a['phrase']} " in spoken:
                hits.append({"start": round(seg["start"], 1), "phrase": a["phrase"], "lead": a["lead"], "tail": a["tail"]})
    return hits


def anchor_ranges(segments, anchors):
    """Place an ad around each learned phrase, using the lead and tail of the ad it was learned from."""
    ranges = []
    for hit in anchor_hits(segments, anchors):
        start = max(0.0, hit["start"] - hit["lead"])
        end = hit["start"] + hit["tail"]
        if MIN_AD_SECONDS <= end - start <= MAX_AD_SECONDS:
            ranges.append([start, end])
    return ranges


def find_ad_ranges(segments, anchors=None):
    ranges = []
    block = None
    for seg in sorted(segments, key=lambda s: s["start"]):
        start_w, end_w = classify(seg["text"])
        if block is not None and seg["start"] - block["last_end"] > MAX_GAP_SECONDS:
            result = close_block(block)
            if result:
                ranges.append(result)
            block = None
        if not (start_w or end_w):
            continue
        if block is None:
            block = {"start": seg["start"], "end": seg["end"], "last_end": seg["end"], "start_w": start_w, "end_w": end_w}
        else:
            block["end"] = seg["end"]
            block["last_end"] = seg["end"]
            block["start_w"] += start_w
            block["end_w"] += end_w
    if block is not None:
        result = close_block(block)
        if result:
            ranges.append(result)
    if anchors:
        ranges = merge_ranges(ranges + anchor_ranges(segments, anchors))
    return ranges


def debug_hits(segments):
    hits = []
    for seg in segments:
        start_w, end_w = classify(seg["text"])
        if start_w or end_w:
            hits.append({"start": round(seg["start"], 1), "start_hits": start_w, "end_hits": end_w, "text": seg["text"].strip()})
    return hits


def scan(file_path, on_progress=None, anchors=None):
    """Returns (ad_ranges, transcript_segments). anchors are phrases learned from hand-tagged ads."""
    model = get_model()
    seg_iter, info = model.transcribe(file_path, beam_size=1, vad_filter=True)
    total = float(getattr(info, "duration", 0) or 0)
    segments = []
    for seg in seg_iter:
        segments.append({"start": round(seg.start, 2), "end": round(seg.end, 2), "text": seg.text})
        if on_progress and total > 0:
            on_progress(min(1.0, seg.end / total))
    return find_ad_ranges(segments, anchors), segments
