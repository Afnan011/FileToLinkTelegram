"""
Pure-Python MKV (EBML) track detector.
No FFmpeg or external binaries required.
Downloads only the first MAX_PROBE bytes of the MKV to read the Tracks element.
"""
import logging
import aiohttp

log = logging.getLogger(__name__)

MAX_PROBE = 7_000_000   # 7 MB — covers Tracks element in almost all MKV files

# ── EBML Element IDs ────────────────────────────────────────
E_SEGMENT     = 0x18538067
E_SEEK_HEAD   = 0x114D9B74
E_TRACKS      = 0x1654AE6B
E_TRACK_ENTRY = 0xAE
E_TRACK_TYPE  = 0x83
E_LANGUAGE    = 0x22B59C
E_TRACK_NAME  = 0x536E
E_CODEC_ID    = 0x86

TYPE_AUDIO    = 2
TYPE_SUB      = 17

LANG_MAP = {
    'hin': 'Hindi',   'eng': 'English',   'tam': 'Tamil',   'tel': 'Telugu',
    'kan': 'Kannada', 'mal': 'Malayalam', 'mar': 'Marathi', 'ben': 'Bengali',
    'pun': 'Punjabi', 'ara': 'Arabic',    'fra': 'French',  'deu': 'German',
    'spa': 'Spanish', 'zho': 'Chinese',   'jpn': 'Japanese','kor': 'Korean',
    'rus': 'Russian', 'por': 'Portuguese','ita': 'Italian', 'und': 'Unknown',
    'mul': 'Multiple','mis': 'Misc',
}


# ── EBML primitives ─────────────────────────────────────────

def _read_id(buf: bytes, p: int):
    """Read 1-4-byte EBML element ID (leading marker bit preserved)."""
    if p >= len(buf):
        return None, 0
    b = buf[p]
    if b >= 0x80: return b, 1
    if b >= 0x40: return (b << 8) | buf[p + 1], 2
    if b >= 0x20: return (b << 16) | (buf[p + 1] << 8) | buf[p + 2], 3
    if b >= 0x10 and p + 3 < len(buf):
        return (b << 24) | (buf[p + 1] << 16) | (buf[p + 2] << 8) | buf[p + 3], 4
    return None, 0


def _read_vint(buf: bytes, p: int):
    """
    Read EBML variable-length integer (data size).
    Returns (value, bytes_consumed).
    Returns (-1, n) for "unknown size" sentinel.
    """
    if p >= len(buf):
        return None, 0
    b = buf[p]
    for size in range(1, 9):
        mask = 0x80 >> (size - 1)
        if b & mask:
            val = b & (mask - 1)
            for i in range(1, size):
                if p + i >= len(buf):
                    return None, 0
                val = (val << 8) | buf[p + i]
            unknown = (1 << (7 * size)) - 1
            return (-1, size) if val == unknown else (val, size)
    return None, 0


# ── EBML element accessors ───────────────────────────────────

def _scan(buf: bytes, target_id: int):
    """Return raw bytes content of the first element with target_id in buf."""
    p = 0
    while p < len(buf):
        eid, il = _read_id(buf, p)
        if eid is None:
            break
        sz, sl = _read_vint(buf, p + il)
        if sz is None:
            break
        hd = il + sl
        end = None if sz < 0 else p + hd + sz
        if eid == target_id:
            return buf[p + hd : end] if end else buf[p + hd :]
        p = end if end else p + hd
        if end is None:
            break
    return None


def _uint(buf: bytes, target_id: int):
    raw = _scan(buf, target_id)
    if raw is None:
        return None
    v = 0
    for byte in raw:
        v = (v << 8) | byte
    return v


def _str(buf: bytes, target_id: int, enc: str = 'ascii'):
    raw = _scan(buf, target_id)
    if raw is None:
        return ''
    return raw.decode(enc, errors='ignore').strip('\x00').strip()


# ── Tracks element locator ───────────────────────────────────

def _find_tracks(buf: bytes) -> bytes | None:
    """
    Locate the Tracks element in the buffer.
    Strategy 1: structured EBML walk.
    Strategy 2: brute-force byte scan (fallback).
    """
    # Strategy 1 — walk top-level elements
    p = 0
    while p < len(buf):
        eid, il = _read_id(buf, p)
        if eid is None:
            break
        sz, sl = _read_vint(buf, p + il)
        if sz is None:
            break
        hd = il + sl
        content_start = p + hd
        content_end   = len(buf) if sz < 0 else min(p + hd + sz, len(buf))

        if eid == E_TRACKS:
            return buf[content_start : content_end]

        if eid in (E_SEGMENT, 0x1A45DFA3):   # Segment or EBML header — recurse
            inner = _find_tracks(buf[content_start : content_end])
            if inner is not None:
                return inner

        if sz < 0:
            break
        p = p + hd + sz

    # Strategy 2 — brute-force scan for Tracks ID bytes
    marker = bytes([0x16, 0x54, 0xAE, 0x6B])
    idx = 0
    while True:
        idx = buf.find(marker, idx)
        if idx == -1:
            break
        sz, sl = _read_vint(buf, idx + 4)
        if sz is None or sz == 0:
            idx += 1
            continue
        start = idx + 4 + sl
        end   = len(buf) if sz < 0 else min(start + sz, len(buf))
        candidate = buf[start : end]
        # Sanity: first byte should be the TrackEntry ID (0xAE)
        if candidate and candidate[0] == 0xAE:
            return candidate
        idx += 1

    return None


# ── TrackEntry parser ────────────────────────────────────────

def _parse_entries(tracks_buf: bytes):
    audio_tracks, sub_tracks = [], []
    ai = si = 0
    p  = 0

    while p < len(tracks_buf):
        eid, il = _read_id(tracks_buf, p)
        if eid is None:
            break
        sz, sl = _read_vint(tracks_buf, p + il)
        if sz is None:
            break
        hd  = il + sl
        end = None if sz < 0 else p + hd + sz
        entry_buf = tracks_buf[p + hd : end] if end else tracks_buf[p + hd :]

        if eid == E_TRACK_ENTRY:
            ttype = _uint(entry_buf, E_TRACK_TYPE)
            lang  = _str(entry_buf, E_LANGUAGE)
            name  = _str(entry_buf, E_TRACK_NAME, 'utf-8')
            codec = _str(entry_buf, E_CODEC_ID)
            label = name or LANG_MAP.get(lang.lower(), lang) or None

            if ttype == TYPE_AUDIO:
                audio_tracks.append({
                    'index':    ai,
                    'label':    label or f'Audio {ai + 1}',
                    'language': lang,
                    'title':    name,
                    'codec':    codec,
                })
                ai += 1

            elif ttype == TYPE_SUB:
                # Only text-based subs can be extracted to WebVTT
                is_text = not codec or any(
                    k in codec for k in ('TEXT', 'SRT', 'ASS', 'SSA', 'WEBVTT')
                )
                if is_text:
                    sub_tracks.append({
                        'index':    si,
                        'label':    label or f'Subtitle {si + 1}',
                        'language': lang,
                        'title':    name,
                        'codec':    codec,
                    })
                    si += 1

        if end is None:
            break
        p = end

    return audio_tracks, sub_tracks


# ── Public API ───────────────────────────────────────────────

async def probe_mkv(dl_url: str, max_bytes: int = MAX_PROBE):
    """
    Download the first max_bytes of an MKV file at dl_url,
    parse the EBML Tracks element, and return (audio_tracks, subtitle_tracks).
    Raises ValueError if the Tracks element cannot be found.
    """
    range_header = {'Range': f'bytes=0-{max_bytes - 1}'}
    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(timeout=timeout) as sess:
        async with sess.get(dl_url, headers=range_header) as resp:
            if resp.status not in (200, 206):
                raise ValueError(f'Bad HTTP status: {resp.status}')
            buf = await resp.read()

    log.debug('probe_mkv: fetched %d bytes from %s', len(buf), dl_url)

    tracks_buf = _find_tracks(buf)
    if tracks_buf is None:
        raise ValueError(
            f'Tracks element not found in first {max_bytes} bytes. '
            'File may not be an MKV or tracks are stored later.'
        )

    audio, subs = _parse_entries(tracks_buf)
    log.debug('probe_mkv: found %d audio, %d subtitle tracks', len(audio), len(subs))
    return audio, subs
