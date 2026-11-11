"""Audio landmark fingerprinting. CPU only: numpy, scipy and ffmpeg.

How it works:
1. ffmpeg decodes audio to mono 11.025 kHz.
2. A spectrogram is computed and local peaks are picked. Peaks that stand out from
   their surroundings survive volume changes, re-encoding, and speech over music.
3. Each peak is paired with the next few peaks. Each pair becomes a 24-bit hash.
4. To find a template in a longer recording, every matching hash votes for the
   time offset it implies. A real match piles its votes on one offset.
"""
import io
import subprocess

import numpy as np
from scipy.ndimage import maximum_filter, uniform_filter

FP_VERSION = 1
SR = 11025
N_FFT = 1024
HOP = 512
FRAME_SEC = HOP / SR
BIN_LO, BIN_HI = 6, 330          # about 65 Hz to 3.5 kHz
PEAK_T, PEAK_F = 9, 25           # local maximum neighbourhood (frames, bins)
CONTEXT_T, CONTEXT_F = 41, 61    # neighbourhood used for the local average
CONTRAST_DB = 6.0                # a peak must beat its surroundings by this much
FLOOR_DB = -75.0                 # ignore anything quieter than this (dB re full scale)
MAX_PEAKS_PER_SEC = 30
FAN = 8                          # pair each peak with the next FAN peaks
MAX_DT = 63                      # frames, must fit in 6 bits
BLOCK_FRAMES = 2048
MARGIN = 24
MIN_TEMPLATE_HASHES = 40
CONF_REF = 0.12                  # share of template hashes that must agree for confidence 1.0
MIN_VOTES = 12

_WIN = np.hanning(N_FFT).astype(np.float32)
_FRAMES_PER_SEC = int(round(1.0 / FRAME_SEC))


class FingerprintError(Exception):
    pass


class Fingerprint:
    def __init__(self, hashes, times, version=None):
        self.hashes = np.asarray(hashes, dtype=np.uint32)
        self.times = np.asarray(times, dtype=np.int32)
        self.version = FP_VERSION if version is None else version
        order = np.argsort(self.hashes, kind="stable")
        self._sh = self.hashes[order]
        self._st = self.times[order]

    def __len__(self):
        return len(self.hashes)


# ---------- decoding ----------

def decode_chunks(path, start=0.0, duration=None, chunk_sec=60):
    """Yield int16 mono samples from ffmpeg in chunks, so long episodes never sit in memory at once."""
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    if start and start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", path]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size = chunk_sec * SR * 2
    got = 0
    try:
        while True:
            data = proc.stdout.read(size)
            if not data:
                break
            got += len(data)
            yield np.frombuffer(data[: len(data) // 2 * 2], dtype="<i2")
    finally:
        proc.stdout.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    if got == 0:
        raise FingerprintError("ffmpeg could not decode any audio from " + path)


# ---------- peaks ----------

def _spectrogram_db(samples):
    n = 1 + (len(samples) - N_FFT) // HOP
    if n <= 0:
        return np.zeros((0, BIN_HI - BIN_LO), dtype=np.float32)
    view = np.lib.stride_tricks.sliding_window_view(samples, N_FFT)[::HOP][:n]
    spec = np.fft.rfft(view * _WIN, axis=1)[:, BIN_LO:BIN_HI]
    power = spec.real.astype(np.float32) ** 2 + spec.imag.astype(np.float32) ** 2
    return 10.0 * np.log10(power + 1e-10)


def _peaks(db):
    """Return (frame, bin, contrast) arrays of spectral peaks."""
    if db.shape[0] == 0:
        return (np.zeros(0, np.int64),) * 2 + (np.zeros(0, np.float32),)
    local_max = maximum_filter(db, size=(PEAK_T, PEAK_F), mode="nearest")
    local_avg = uniform_filter(db, size=(CONTEXT_T, CONTEXT_F), mode="nearest")
    contrast = db - local_avg
    ok = (db >= local_max) & (contrast >= CONTRAST_DB) & (db > FLOOR_DB)
    t, f = np.nonzero(ok)
    return t.astype(np.int64), f.astype(np.int64), contrast[t, f]


def _cap_density(t, f, c):
    """Keep at most MAX_PEAKS_PER_SEC of the strongest peaks in each second."""
    if len(t) == 0:
        return t, f
    group = t // _FRAMES_PER_SEC
    order = np.lexsort((-c, group))
    group_sorted = group[order]
    starts = np.r_[0, np.nonzero(np.diff(group_sorted))[0] + 1]
    rank = np.arange(len(order)) - np.repeat(starts, np.diff(np.r_[starts, len(order)]))
    keep = order[rank < MAX_PEAKS_PER_SEC]
    return t[keep], f[keep]


def peaks_from_chunks(chunks):
    """Stream samples in, return all peaks as (frames, bins) with frame numbers from the first sample."""
    need = (BLOCK_FRAMES + 2 * MARGIN - 1) * HOP + N_FFT
    carry = np.zeros(0, dtype=np.float32)
    start_frame = 0
    first = True
    all_t, all_f, all_c = [], [], []

    def take(db, lo, hi, base):
        t, f, c = _peaks(db)
        keep = (t >= lo) & (t < hi)
        all_t.append(t[keep] + base)
        all_f.append(f[keep])
        all_c.append(c[keep])

    for chunk in chunks:
        carry = np.concatenate([carry, chunk.astype(np.float32) / 32768.0])
        while len(carry) >= need:
            db = _spectrogram_db(carry[:need])
            take(db, 0 if first else MARGIN, BLOCK_FRAMES + MARGIN, start_frame)
            carry = carry[BLOCK_FRAMES * HOP:]
            start_frame += BLOCK_FRAMES
            first = False
    if len(carry) >= N_FFT:
        db = _spectrogram_db(carry)
        take(db, 0 if first else MARGIN, db.shape[0], start_frame)
    if not all_t:
        return np.zeros(0, np.int64), np.zeros(0, np.int64)
    t = np.concatenate(all_t)
    f = np.concatenate(all_f)
    c = np.concatenate(all_c)
    return _cap_density(t, f, c)


# ---------- hashing ----------

def hashes_from_peaks(t, f):
    if len(t) < 2:
        return np.zeros(0, np.uint32), np.zeros(0, np.int32)
    order = np.lexsort((f, t))
    t, f = t[order], f[order]
    hs, ts = [], []
    for k in range(1, FAN + 1):
        if len(t) <= k:
            break
        dt = t[k:] - t[:-k]
        ok = dt <= MAX_DT
        if not ok.any():
            continue
        h = (f[:-k][ok].astype(np.uint32) << 15) | (f[k:][ok].astype(np.uint32) << 6) | dt[ok].astype(np.uint32)
        hs.append(h)
        ts.append(t[:-k][ok])
    if not hs:
        return np.zeros(0, np.uint32), np.zeros(0, np.int32)
    return np.concatenate(hs), np.concatenate(ts).astype(np.int32)


def fingerprint_chunks(chunks):
    t, f = peaks_from_chunks(chunks)
    h, ht = hashes_from_peaks(t, f)
    return Fingerprint(h, ht)


def fingerprint_file(path, start=0.0, duration=None):
    return fingerprint_chunks(decode_chunks(path, start, duration))


def fingerprint_samples(samples_int16):
    return fingerprint_chunks([np.asarray(samples_int16, dtype=np.int16)])


# ---------- storage ----------

def pack(fp):
    buf = io.BytesIO()
    np.savez_compressed(buf, h=fp.hashes, t=fp.times, v=np.array([fp.version]))
    return buf.getvalue()


def unpack(blob):
    """Return a Fingerprint, or None when the blob is missing or was made by another algorithm version."""
    if not blob:
        return None
    try:
        data = np.load(io.BytesIO(blob))
        if int(data["v"][0]) != FP_VERSION:
            return None
        return Fingerprint(data["h"], data["t"])
    except Exception:
        return None


# ---------- matching ----------

def _votes(template, query):
    """Return the time offset (query frame minus template frame) of every hash that appears in both."""
    if len(template) == 0 or len(query) == 0:
        return np.zeros(0, np.int64)
    left = np.searchsorted(template._sh, query.hashes, side="left")
    right = np.searchsorted(template._sh, query.hashes, side="right")
    counts = right - left
    total = int(counts.sum())
    if total == 0:
        return np.zeros(0, np.int64)
    q_idx = np.repeat(np.arange(len(query)), counts)
    base = np.repeat(np.cumsum(counts) - counts, counts)
    t_idx = np.repeat(left, counts) + (np.arange(total) - base)
    return query.times[q_idx].astype(np.int64) - template._st[t_idx].astype(np.int64)


def _confidence(votes, n_template):
    if n_template <= 0:
        return 0.0
    return float(min(1.0, votes / (CONF_REF * n_template)))


def match(template, query, max_hits=1, min_confidence=0.0, min_gap_frames=None):
    """Find where the template occurs in the query.

    Returns a list of hits, best first: {offset (seconds into the query where the
    template starts, may be slightly negative), votes, confidence}.
    """
    offsets = _votes(template, query)
    if len(offsets) == 0:
        return []
    lo = int(offsets.min())
    hist = np.bincount(offsets - lo).astype(np.int64)
    smooth = np.convolve(hist, np.ones(3, dtype=np.int64), mode="same")
    hits = []
    gap = min_gap_frames if min_gap_frames is not None else int(template.times.max() if len(template) else 0)
    for _ in range(max_hits):
        pos = int(smooth.argmax())
        votes = int(smooth[pos])
        conf = _confidence(votes, len(template))
        if votes < MIN_VOTES or conf < min_confidence:
            break
        # centre the offset on the votes inside the winning bins
        window = slice(max(0, pos - 1), pos + 2)
        weights = hist[window]
        centre = (np.arange(len(hist))[window] * weights).sum() / max(1, weights.sum())
        hits.append({"offset": float((centre + lo) * FRAME_SEC), "votes": votes, "confidence": conf})
        smooth[max(0, pos - gap): pos + gap + 1] = 0
    return hits


def best_match(template, query):
    hits = match(template, query, max_hits=1)
    if hits:
        return hits[0]
    return {"offset": 0.0, "votes": 0, "confidence": 0.0}
