import time
import json
import math
import logging
import hashlib
import secrets
import asyncio
import mimetypes
import traceback
import urllib.parse
import jinja2
from aiohttp import web
from aiohttp.http_exceptions import BadStatusLine
from FileStream.bot import multi_clients, work_loads, FileStream
from FileStream.config import Telegram, Server
from FileStream.server.exceptions import FIleNotFound, InvalidHash
from FileStream import utils, StartTime, __version__
from FileStream.utils.render_template import render_page
from FileStream.utils.database import Database
from FileStream.utils.human_readable import humanbytes
from FileStream.utils.mkv_probe import probe_mkv

routes = web.RouteTableDef()

# ──────────────────────────────────────────────────────────────
# Auth — hardcoded credentials (username: Afnan / password: Afnan011)
# ──────────────────────────────────────────────────────────────
_CREDENTIALS = {
    "Afnan": hashlib.sha256("Afnan011".encode()).hexdigest()
}
_active_sessions: set = set()   # in-memory session tokens
_db = Database(Telegram.DATABASE_URL, Telegram.SESSION_NAME)


def _render_template(template_path: str, **kwargs) -> str:
    with open(template_path, encoding="utf-8") as f:
        return jinja2.Template(f.read()).render(**kwargs)


def _check_session(request: web.Request) -> bool:
    token = request.cookies.get("fs_session")
    return token is not None and token in _active_sessions


@routes.get("/", allow_head=True)
async def root_redirect(request: web.Request):
    """Root URL — redirect to /files (which handles login redirect automatically)."""
    raise web.HTTPFound("/files")


@routes.get("/status", allow_head=True)
async def root_route_handler(_):
    return web.json_response(
        {
            "server_status": "running",
            "uptime": utils.get_readable_time(time.time() - StartTime),
            "telegram_bot": "@" + FileStream.username,
            "connected_bots": len(multi_clients),
            "loads": dict(
                ("bot" + str(c + 1), l)
                for c, (_, l) in enumerate(
                    sorted(work_loads.items(), key=lambda x: x[1], reverse=True)
                )
            ),
            "version": __version__,
        }
    )

# ──────────────────────────────────────────────────────────────
# File Library — Auth routes
# ──────────────────────────────────────────────────────────────

@routes.get("/files", allow_head=True)
async def files_page(request: web.Request):
    if not _check_session(request):
        raise web.HTTPFound("/login")
    html = _render_template("FileStream/template/files.html")
    return web.Response(text=html, content_type="text/html")


@routes.get("/login", allow_head=True)
async def login_page(request: web.Request):
    if _check_session(request):
        raise web.HTTPFound("/files")
    html = _render_template("FileStream/template/login.html", error="")
    return web.Response(text=html, content_type="text/html")


@routes.post("/login")
async def login_post(request: web.Request):
    try:
        data     = await request.post()
        username = data.get("username", "").strip()
        password = data.get("password", "")
        pwd_hash = hashlib.sha256(password.encode()).hexdigest()

        if username in _CREDENTIALS and _CREDENTIALS[username] == pwd_hash:
            token = secrets.token_hex(32)
            _active_sessions.add(token)
            resp = web.HTTPFound("/files")
            resp.set_cookie(
                "fs_session", token,
                httponly=True, max_age=86400 * 7, samesite="Lax"
            )
            return resp
        else:
            html = _render_template(
                "FileStream/template/login.html",
                error="Invalid username or password. Please try again."
            )
            return web.Response(text=html, content_type="text/html", status=401)
    except Exception as e:
        logging.error(f"Login error: {e}")
        html = _render_template("FileStream/template/login.html", error="Server error. Please try again.")
        return web.Response(text=html, content_type="text/html", status=500)


@routes.get("/logout")
async def logout(request: web.Request):
    token = request.cookies.get("fs_session")
    if token:
        _active_sessions.discard(token)
    resp = web.HTTPFound("/login")
    resp.del_cookie("fs_session")
    return resp


@routes.get("/api/files", allow_head=True)
async def api_files(request: web.Request):
    if not _check_session(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    try:
        page    = max(1, int(request.rel_url.query.get("page", 1)))
        limit   = 20
        skip    = (page - 1) * limit
        search  = request.rel_url.query.get("search", "").strip()
        ftype   = request.rel_url.query.get("type", "").strip()

        cursor, total = await _db.get_all_files(
            skip=skip, limit=limit, search=search, file_type=ftype
        )
        files = []
        async for f in cursor:
            files.append({
                "id":        str(f["_id"]),
                "file_name": f.get("file_name", "Unknown"),
                "file_size": humanbytes(int(f.get("file_size") or 0)),
                "mime_type": f.get("mime_type", ""),
                "time":      f.get("time", 0),
            })
        pages = max(1, (total + limit - 1) // limit)
        return web.json_response(
            {"files": files, "total": total, "page": page, "pages": pages},
            headers={"Cache-Control": "no-cache"}
        )
    except Exception as e:
        logging.error(f"API files error: {e}")
        return web.json_response({"error": str(e)}, status=500)


# ──────────────────────────────────────────────────────────────
# FFmpeg — MKV Track Detection & Streaming
# ──────────────────────────────────────────────────────────────

@routes.get("/api/tracks/{path}", allow_head=True)
async def get_tracks(request: web.Request):
    """
    Detect audio/subtitle tracks in an MKV file.
    Strategy 1: ffprobe (fast, accurate — needs FFmpeg installed).
    Strategy 2: pure-Python EBML parser (no binaries, works everywhere).
    """
    path = request.match_info["path"]
    file_url = urllib.parse.urljoin(Server.URL, f'dl/{path}')

    # ── Strategy 1: ffprobe ──────────────────────────────────
    try:
        proc = await asyncio.create_subprocess_exec(
            'ffprobe', '-v', 'quiet',
            '-print_format', 'json',
            '-show_streams',
            '-analyzeduration', '1000000',
            '-probesize', '3000000',
            file_url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        data = json.loads(stdout.decode())

        audio_tracks, subtitle_tracks = [], []
        ai = si = 0
        for stream in data.get('streams', []):
            ct   = stream.get('codec_type', '')
            tags = stream.get('tags', {})
            lang  = tags.get('language', tags.get('LANGUAGE', ''))
            title = tags.get('title',    tags.get('TITLE', ''))
            label = title or lang or None
            if ct == 'audio':
                audio_tracks.append({'index': ai, 'label': label or f'Audio {ai+1}',
                    'language': lang, 'title': title, 'codec': stream.get('codec_name', '')})
                ai += 1
            elif ct == 'subtitle':
                subtitle_tracks.append({'index': si, 'label': label or f'Subtitle {si+1}',
                    'language': lang, 'title': title, 'codec': stream.get('codec_name', '')})
                si += 1

        return web.json_response(
            {'audio': audio_tracks, 'subtitles': subtitle_tracks, 'method': 'ffprobe'},
            headers={'Cache-Control': 'max-age=300'}
        )

    except FileNotFoundError:
        pass   # ffprobe not installed — fall through to Python parser
    except Exception as e:
        logging.warning(f"ffprobe failed ({e}), trying EBML fallback")

    # ── Strategy 2: pure-Python EBML MKV parser ──────────────
    try:
        audio_tracks, subtitle_tracks = await asyncio.wait_for(
            probe_mkv(file_url), timeout=40
        )
        return web.json_response(
            {'audio': audio_tracks, 'subtitles': subtitle_tracks, 'method': 'ebml'},
            headers={'Cache-Control': 'max-age=300'}
        )
    except asyncio.TimeoutError:
        return web.json_response({'audio': [], 'subtitles': [], 'error': 'probe_timeout'})
    except Exception as e:
        logging.error(f"EBML probe failed: {e}")
        return web.json_response({'audio': [], 'subtitles': [], 'error': str(e)})


@routes.get("/remux/{path}", allow_head=True)
async def remux_handler(request: web.Request):
    """
    Stream MKV with a chosen audio track as browser-compatible fragmented MP4.
    - Video : copied (no re-encode).
    - Audio : ALWAYS transcoded to AAC stereo - EAC3/AC3/DTS are not
              decodable by browsers without transcoding.
    - ?t=N  : fast-seek to N seconds (FFmpeg uses HTTP Range on /dl/ stream).
    """
    path        = request.match_info["path"]
    audio_track = int(request.rel_url.query.get("audio", 0))
    seek_time   = float(request.rel_url.query.get("t", 0))
    try:
        file_url  = urllib.parse.urljoin(Server.URL, f'dl/{path}')
        file_info = await _db.get_file(path)
        raw_name  = (file_info.get('file_name', 'video') if file_info else 'video')
        file_name = raw_name.rsplit('.', 1)[0] + '.mp4' if '.' in raw_name else raw_name + '.mp4'

        cmd = ['ffmpeg', '-v', 'quiet']
        # Fast-seek BEFORE -i: FFmpeg translates to HTTP Range on the /dl/ stream.
        if seek_time > 10:
            cmd += ['-ss', str(int(seek_time))]
        cmd += [
            '-i', file_url,
            '-map', '0:v:0',
            '-map', f'0:a:{audio_track}',
            '-c:v', 'copy',                # video: copy (no re-encode)
            '-c:a', 'aac',                 # audio: transcode to AAC (EAC3/AC3 -> AAC)
            '-b:a', '192k',
            '-ac', '2',                    # downmix 5.1 -> stereo
            '-movflags', 'frag_keyframe+empty_moov+default_base_moof',
            '-f', 'mp4',
            'pipe:1',
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL
        )

        async def body_generator():
            try:
                while True:
                    chunk = await proc.stdout.read(65536)
                    if not chunk:
                        break
                    yield chunk
            finally:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass

        return web.Response(
            body=body_generator(),
            headers={
                'Content-Type':        'video/mp4',
                'Content-Disposition': f'inline; filename="{file_name}"',
                'X-Accel-Buffering':   'no',
                'Cache-Control':       'no-cache',
            }
        )
    except FIleNotFound:
        raise web.HTTPNotFound(text="File not found")
    except FileNotFoundError:
        raise web.HTTPServiceUnavailable(text="ffmpeg not installed on this server")
    except Exception as e:
        logging.error(f"Remux error: {e}")
        raise web.HTTPInternalServerError(text=str(e))


@routes.get("/sub/{path}/{track_index}", allow_head=True)
async def subtitle_handler(request: web.Request):
    """
    Extract a subtitle track from MKV and stream it as WebVTT.
    Streams chunks so the client starts receiving cues immediately.
    """
    path        = request.match_info["path"]
    track_str   = request.match_info["track_index"]   # e.g. '0.vtt'
    track_index = int(track_str.split('.')[0])
    try:
        file_url = urllib.parse.urljoin(Server.URL, f'dl/{path}')
        proc = await asyncio.create_subprocess_exec(
            'ffmpeg',
            '-v', 'quiet',
            '-i', file_url,
            '-map', f'0:s:{track_index}',
            '-f', 'webvtt',
            'pipe:1',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL
        )

        async def vtt_generator():
            try:
                while True:
                    chunk = await asyncio.wait_for(proc.stdout.read(8192), timeout=60)
                    if not chunk:
                        break
                    yield chunk
            except asyncio.TimeoutError:
                logging.warning("Subtitle extraction timed out (60s idle)")
            finally:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass

        return web.Response(
            body=vtt_generator(),
            headers={
                'Content-Type':                'text/vtt; charset=utf-8',
                'Access-Control-Allow-Origin': '*',
                'Cache-Control':               'max-age=300',
            }
        )
    except FileNotFoundError:
        raise web.HTTPServiceUnavailable(text="ffmpeg not installed on this server")
    except Exception as e:
        logging.error(f"Subtitle extraction error: {e}")
        raise web.HTTPInternalServerError(text=str(e))


# ──────────────────────────────────────────────────────────────

@routes.get("/watch/{path}", allow_head=True)
async def stream_handler(request: web.Request):
    try:
        path = request.match_info["path"]
        return web.Response(text=await render_page(path), content_type='text/html')
    except InvalidHash as e:
        raise web.HTTPForbidden(text=e.message)
    except FIleNotFound as e:
        raise web.HTTPNotFound(text=e.message)
    except (AttributeError, BadStatusLine, ConnectionResetError):
        pass


@routes.get("/dl/{path}", allow_head=True)
async def stream_handler(request: web.Request):
    try:
        path = request.match_info["path"]
        return await media_streamer(request, path)
    except InvalidHash as e:
        raise web.HTTPForbidden(text=e.message)
    except FIleNotFound as e:
        raise web.HTTPNotFound(text=e.message)
    except (AttributeError, BadStatusLine, ConnectionResetError):
        pass
    except Exception as e:
        traceback.print_exc()
        logging.critical(e.with_traceback(None))
        logging.debug(traceback.format_exc())
        raise web.HTTPInternalServerError(text=str(e))

class_cache = {}

async def media_streamer(request: web.Request, db_id: str):
    range_header = request.headers.get("Range", 0)
    
    index = min(work_loads, key=work_loads.get)
    faster_client = multi_clients[index]
    
    if Telegram.MULTI_CLIENT:
        logging.info(f"Client {index} is now serving {request.headers.get('X-FORWARDED-FOR',request.remote)}")

    if faster_client in class_cache:
        tg_connect = class_cache[faster_client]
        logging.debug(f"Using cached ByteStreamer object for client {index}")
    else:
        logging.debug(f"Creating new ByteStreamer object for client {index}")
        tg_connect = utils.ByteStreamer(faster_client)
        class_cache[faster_client] = tg_connect
    logging.debug("before calling get_file_properties")
    file_id = await tg_connect.get_file_properties(db_id, multi_clients)
    logging.debug("after calling get_file_properties")
    
    file_size = file_id.file_size

    if range_header:
        from_bytes, until_bytes = range_header.replace("bytes=", "").split("-")
        from_bytes = int(from_bytes)
        until_bytes = int(until_bytes) if until_bytes else file_size - 1
    else:
        from_bytes = request.http_range.start or 0
        until_bytes = (request.http_range.stop or file_size) - 1

    if (until_bytes > file_size) or (from_bytes < 0) or (until_bytes < from_bytes):
        return web.Response(
            status=416,
            body="416: Range not satisfiable",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    chunk_size = 1024 * 1024
    until_bytes = min(until_bytes, file_size - 1)

    offset = from_bytes - (from_bytes % chunk_size)
    first_part_cut = from_bytes - offset
    last_part_cut = until_bytes % chunk_size + 1

    req_length = until_bytes - from_bytes + 1
    part_count = math.ceil(until_bytes / chunk_size) - math.floor(offset / chunk_size)
    body = tg_connect.yield_file(
        file_id, index, offset, first_part_cut, last_part_cut, part_count, chunk_size
    )

    mime_type = file_id.mime_type
    file_name = utils.get_name(file_id)
    disposition = "attachment"

    if not mime_type:
        mime_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"

    # if "video/" in mime_type or "audio/" in mime_type:
    #     disposition = "inline"

    return web.Response(
        status=206 if range_header else 200,
        body=body,
        headers={
            "Content-Type": f"{mime_type}",
            "Content-Range": f"bytes {from_bytes}-{until_bytes}/{file_size}",
            "Content-Length": str(req_length),
            "Content-Disposition": f'{disposition}; filename="{file_name}"',
            "Accept-Ranges": "bytes",
        },
    )
