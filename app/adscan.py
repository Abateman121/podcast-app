import os
import re

_model = None

SPONSOR_PATTERNS = [
    r"brought to you by",
    r"sponsored by",
    r"this episode is (sponsored|brought)",
    r"support for this (podcast|show|episode)",
    r"promo code",
    r"(use|enter) (the )?(offer )?code",
    r"free trial",
    r"\d+\s?(%|percent) off",
    r"dot com slash",
    r"\.com/",
    r"link in the (show )?notes",
    r"go to \w+ dot com",
    r"visit \w+ dot com",
    r"thanks to .{0,40} for sponsoring",
]
SPONSOR_REGEX = [re.compile(p, re.IGNORECASE) for p in SPONSOR_PATTERNS]

MERGE_GAP_SECONDS = 25.0
MIN_AD_SECONDS = 20.0
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


def score_text(text):
    return sum(1 for rx in SPONSOR_REGEX if rx.search(text))


def find_ad_ranges(segments):
    hits = [(s["start"], s["end"]) for s in segments if score_text(s["text"]) >= 1]
    ranges = []
    for start, end in hits:
        if ranges and start - ranges[-1][1] <= MERGE_GAP_SECONDS:
            ranges[-1][1] = end
        else:
            ranges.append([start, end])
    result = []
    for start, end in ranges:
        start = max(0.0, start - 2.0)
        end = end + 3.0
        length = end - start
        if MIN_AD_SECONDS <= length <= MAX_AD_SECONDS:
            result.append([round(start, 1), round(end, 1)])
    return result


def scan(file_path, on_progress=None):
    model = get_model()
    seg_iter, info = model.transcribe(file_path, beam_size=1, vad_filter=True)
    total = float(getattr(info, "duration", 0) or 0)
    segments = []
    for seg in seg_iter:
        segments.append({"start": seg.start, "end": seg.end, "text": seg.text})
        if on_progress and total > 0:
            on_progress(min(1.0, seg.end / total))
    return find_ad_ranges(segments)
