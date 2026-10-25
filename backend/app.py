"""
Bui Records -- backend API (FastAPI)

The shop's own products/prices/orders live in SQLite; Discogs only supplies release metadata (cover, tracklist...).
Signed session cookie = cart storage (no cart tables) and the logged-in customer's id.
"""

import csv
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
import warnings
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Optional

from dotenv import load_dotenv

# Load environment variables from .env BEFORE anything reads them
load_dotenv()

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

import auth
import cases
import database
import discogs_client

HERE = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.abspath(os.path.join(HERE, "../frontend/"))

SHIPPING_EXPRESS_FEE = 45000
SHIPPING_STANDARD_FEE = 35000
FREE_SHIPPING_THRESHOLD = 1500000
PAYMENT_LABELS = {
    "cod": "Thanh toán khi nhận hàng",
    "bank": "Chuyển khoản",
    "momo": "Momo",
    "zalopay": "ZaloPay",
    "card": "Thẻ tín dụng / ghi nợ",
}


# Non-persistent demo testimonials. Product/community data still comes from Discogs.
SITE_REVIEWS = [
    {"stars": 5, "quote": "Kệ đĩa gọn gàng, đặt hàng nhanh, theo dõi đơn dễ dàng.", "who": "Khách hàng Bụi Records"},
    {"stars": 5, "quote": "Thông tin từng bản ép rất chi tiết, giúp mình yên tâm trước khi mua.", "who": "Người sưu tầm đĩa than"},
]


# ------------------------------------------------------------------------
# App setup
# ------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    yield


log = logging.getLogger("bui")

# API docs are not needed in production.
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

# No hardcoded fallback secret: generate a throwaway one for local/dev runs
# (sessions won't survive a restart) and warn loudly instead of silently
# using a value that could end up committed to source control.
_secret_key = os.environ.get("SECRET_KEY") or os.environ.get("FLASK_SECRET_KEY")
if not _secret_key and database.IS_VERCEL:
    # Each serverless instance would invent its own key, so carts and logins would
    # randomly disappear between requests. Fail with a clear message instead.
    raise RuntimeError("SECRET_KEY must be set in the Vercel project's Environment Variables.")
if not _secret_key:
    warnings.warn(
        "SECRET_KEY is not set. Using a random throwaway key for this "
        "process only -- all sessions (carts) will be invalidated on restart. "
        "Set SECRET_KEY in your .env for real use.",
        RuntimeWarning,
    )
    _secret_key = secrets.token_hex(32)

# Vercel always serves over HTTPS.
_cookie_secure = os.environ.get("COOKIE_SECURE", "1" if database.IS_VERCEL else "0") == "1"
# Behind Vercel's proxy request.client is the proxy; the real client is in x-forwarded-for,
# which Vercel overwrites (clients can't spoof it). Elsewhere only trust it when told to.
_trust_proxy = os.environ.get("TRUST_PROXY", "1" if database.IS_VERCEL else "0") == "1"

# Session cookie storage for the cart, signed the same way Flask's was.
# max_age=None makes it a real browser-session cookie (no Max-Age/Expires),
# matching the previous behaviour of expiring when the browser closes.
app.add_middleware(
    SessionMiddleware,
    secret_key=_secret_key,
    session_cookie="session",
    max_age=None,
    same_site="lax",
    https_only=_cookie_secure,
)

# The frontend is served by this same app (same origin), so CORS is off by default.
# Set ALLOWED_ORIGIN_REGEX only if the frontend is hosted on another origin.
_allowed_origin_regex = os.environ.get("ALLOWED_ORIGIN_REGEX", "").strip()
if _allowed_origin_regex:
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=_allowed_origin_regex,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "X-Admin-Token"],
    )


@app.middleware("http")
async def revalidate_frontend(request: Request, call_next):
    """
    index.html / script.js / style.css: the browser must check with the server
    before reusing its copy ("no-cache" = revalidate, a cheap 304 when nothing
    changed), so a new version of the shop shows up on the next page load.
    """
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Keep the {"error": "..."} JSON shape the frontend expects (incl. unknown routes).

    bad_request(..., code="out_of_stock") adds {"code": "..."} so the frontend can
    react to the *reason* instead of guessing from the status code or the text.
    """
    code = None
    if isinstance(exc.detail, dict):
        detail = exc.detail.get("message") or "Yêu cầu không hợp lệ."
        code = exc.detail.get("code")
    else:
        detail = exc.detail if isinstance(exc.detail, str) else "Yêu cầu không hợp lệ."
    if exc.status_code == 404 and detail == "Not Found":
        detail = "Không tìm thấy"
    if exc.status_code == 405:
        detail = "Phương thức không được hỗ trợ."
    content = {"error": detail}
    if code:
        content["code"] = code
    return JSONResponse(status_code=exc.status_code, content=content)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"error": "Dữ liệu gửi lên không hợp lệ."})


# ------------------------------------------------------------------------
# Small helpers
# ------------------------------------------------------------------------

def bad_request(message, status_code=400, code=None):
    raise HTTPException(
        status_code=status_code,
        detail={"message": message, "code": code} if code else message,
    )


def not_found(message="Không tìm thấy"):
    raise HTTPException(status_code=404, detail=message)


def discogs_error_response(exc):
    """Map any Discogs failure to a generic Vietnamese message; details go to the log only."""
    log.warning("Discogs error (%s): %s", getattr(exc, "status_code", None), exc)
    code = getattr(exc, "status_code", None)
    if code == 404:
        raise HTTPException(status_code=404, detail="Không tìm thấy đĩa này trên Discogs.")
    if code == 429:
        raise HTTPException(status_code=503, detail="Discogs đang giới hạn truy cập, vui lòng thử lại sau ít phút.")
    raise HTTPException(status_code=502, detail="Không kết nối được tới Discogs, vui lòng thử lại sau.")


MAX_QTY = 20          # per cart line
MAX_CART_LINES = 12   # the cart lives in a cookie (~4 KB browser limit)
MAX_ID = 2 ** 31 - 1


def check_id(value: int, what: str = "mục"):
    """Path ids must fit SQLite/Discogs; huge values would overflow and 500."""
    if not (0 < int(value) <= MAX_ID):
        raise HTTPException(status_code=404, detail=f"Không tìm thấy {what}.")
    return int(value)


def parse_release_id(value):
    """Accept int or ASCII-digit string only ('²' or Arabic digits are rejected)."""
    if isinstance(value, bool):
        bad_request("Sản phẩm không hợp lệ.")
    text = str(value)
    if not (text.isascii() and text.isdigit() and 0 < int(text) <= MAX_ID):
        bad_request("Sản phẩm không hợp lệ.")
    return int(text)


def parse_qty(value):
    if type(value) is not int or not (1 <= value <= MAX_QTY):
        bad_request(f"Số lượng phải là số nguyên từ 1 đến {MAX_QTY}.")
    return value


def text_field(payload, key, max_len, required=False, label=""):
    value = payload.get(key)
    if value is None:
        value = ""
    if not isinstance(value, str):
        bad_request("Dữ liệu gửi lên không hợp lệ.")
    value = value.strip()
    if required and not value:
        bad_request(f"Vui lòng nhập {label or key}.")
    if len(value) > max_len:
        bad_request(f"{(label or key).capitalize()} quá dài (tối đa {max_len} ký tự).")
    return value


# Tiny in-memory rate limiter for endpoints that can be probed (lookups, admin).
_rl_hits: dict = {}
_rl_lock = threading.Lock()


def client_ip(request: Request):
    if _trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client else "?"


def rate_limit(request: Request, bucket: str, limit: int = 20, window: int = 60, key: Optional[str] = None):
    """Allow `limit` hits per `window` seconds, counted per client IP (or per `key`, e.g. an email)."""
    ident = key if key is not None else client_ip(request)
    slot = (bucket, ident)
    now = time.time()
    with _rl_lock:
        if len(_rl_hits) > 10000:  # keys can be chosen by the caller (emails): drop idle ones
            for stale in [k for k, ts in _rl_hits.items() if not ts or now - ts[-1] > 3600]:
                del _rl_hits[stale]
        hits = [t for t in _rl_hits.get(slot, []) if now - t < window]
        if len(hits) >= limit:
            _rl_hits[slot] = hits
            raise HTTPException(status_code=429, detail="Bạn thao tác quá nhanh, vui lòng thử lại sau ít phút.")
        hits.append(now)
        _rl_hits[slot] = hits


def fold(text):
    """Lower-case and strip diacritics, so 'trinh cong son' finds 'Trịnh Công Sơn' (đ -> d too)."""
    text = unicodedata.normalize("NFD", str(text or "").lower().replace("đ", "d"))
    return "".join(c for c in text if not unicodedata.combining(c))


def search_text(item):
    """Everything a shopper might type to find a release, folded for matching."""
    parts = [item.get("title"), item.get("artist"), item.get("year"), item.get("country"), item.get("catno")]
    for key in ("labels", "genres", "styles"):
        parts.extend(item.get(key) or [])
    return fold(" ".join(str(p) for p in parts if p))


@lru_cache(maxsize=256)
def cached_release(release_id):
    return discogs_client.get_release(release_id)


def local_price(release_id):
    """The shop's own price row (table release_prices) or None."""
    conn = database.get_db()
    try:
        return database.get_price(conn, release_id)
    finally:
        conn.close()


def resolve_release(release_id):
    try:
        return cached_release(int(release_id))
    except (TypeError, ValueError):
        raise discogs_client.DiscogsError("Discogs release id must be an integer", status_code=400)


# ------------------------------------------------------------------------
# SQLite connection -- orders/preorders only (per-request dependency)
# ------------------------------------------------------------------------

def get_conn():
    conn = database.get_db()
    try:
        yield conn
    finally:
        conn.close()


# ------------------------------------------------------------------------
# Frontend index -- static assets are mounted at the bottom of this file
# ------------------------------------------------------------------------

@app.get("/")
def index(request: Request):
    index_path = os.path.join(FRONTEND_DIR, "index.html")
    if not os.path.exists(index_path):
        return PlainTextResponse(
            "index.html not found next to the backend folder. "
            "The API is still available under /api/.",
            status_code=404,
        )
    count_visit(request)
    return FileResponse(index_path)


def count_visit(request: Request):
    """
    "Khách truy cập" on the admin dashboard: one per browser per day. The id is a
    random value in the signed session cookie, so no personal data is stored.
    A failing write must never stop the shop page from loading.
    """
    vid = request.session.get("vid")
    if not isinstance(vid, str) or len(vid) != 16:
        vid = secrets.token_hex(8)
        request.session["vid"] = vid
    try:
        conn = database.get_db()
        try:
            database.record_visit(conn, vid)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning("visit not recorded: %s", exc)


# ------------------------------------------------------------------------
# Health / Discogs catalogue
# ------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "catalogue": "discogs",
        "database": ["orders", "preorders", "release_prices", "users", "site_visits"],
        "cart": "signed-session-cookie",
    }


def shop_records(filters, page, per_page):
    """
    The shop catalogue: releases that have a row in release_prices.
    Metadata (cover, tracklist, genre...) comes from Discogs (cached);
    price and stock come from the shop's own DB.
    """
    conn = database.get_db()
    try:
        # Sold-out discs stay in the catalogue (shown as "Hết hàng", not buyable)
        # but are listed after everything that can still be bought.
        rows = conn.execute(
            "SELECT discogs_release_id FROM release_prices "
            "ORDER BY CASE WHEN stock_status = 'out_of_stock' "
            "OR (stock IS NOT NULL AND stock <= 0) THEN 1 ELSE 0 END, "
            "updated_at DESC, discogs_release_id"
        ).fetchall()
    finally:
        conn.close()
    ids = [r["discogs_release_id"] for r in rows]

    def load(rid):
        try:
            return discogs_client.normalize_release(cached_release(rid))
        except Exception as exc:
            # Discogs down / rate-limited: keep the product on sale using the DB
            # row alone (price and stock are the shop's own data).
            log.warning("metadata for release %s unavailable: %s", rid, exc)
            return discogs_client.normalize_release({"id": rid, "title": f"Đĩa #{rid}"})

    with ThreadPoolExecutor(max_workers=6) as pool:
        items = [i for i in pool.map(load, ids) if i]

    def has(value, needle):
        return needle.lower() in str(value or "").lower()

    def keep(i):
        f = filters
        if f.get("search"):
            hay = search_text(i)
            if not all(w in hay for w in fold(f["search"]).split()[:8]):
                return False
        if f.get("label") and not any(has(x, f["label"]) for x in i.get("labels") or []):
            return False
        if f.get("artist") and not has(i.get("artist"), f["artist"]):
            return False
        if f.get("genre") and f["genre"] not in (i.get("genres") or []):
            return False
        if f.get("style") and f["style"] not in (i.get("styles") or []):
            return False
        if f.get("country") and not has(i.get("country"), f["country"]):
            return False
        # No format at all = Discogs metadata unavailable right now (see load() above):
        # keep the product visible instead of silently dropping it from the catalogue.
        if f.get("format") and i.get("format") and not has(i.get("format"), f["format"]):
            return False
        if f.get("year") and str(i.get("year")) != str(f["year"]):
            return False
        return True

    items = [i for i in items if keep(i)]
    total = len(items)
    start = (page - 1) * per_page
    pages = max(1, -(-total // per_page))
    return items[start:start + per_page], {
        "page": page, "pages": pages, "per_page": per_page, "items": total,
    }



@app.get("/api/records")
def list_records(
    search: str = "",
    label: Optional[str] = None,
    artist: Optional[str] = None,
    genre: Optional[str] = None,
    style: Optional[str] = None,
    country: Optional[str] = None,
    format: Optional[str] = None,
    year: Optional[int] = None,
    # Kept for compatibility with old frontend calls; become page/per_page.
    limit: Optional[int] = None,
    offset: int = 0,
    per_page: Optional[int] = None,
    page: Optional[int] = None,
):
    """The shop catalogue: releases that have a row in release_prices.

    Supported query params: search, label, artist, genre, style, country,
    year, format, page, per_page. limit/offset are accepted for compatibility.
    """
    search = (search or "").strip()[:100]
    resolved_per_page = per_page or (limit or 24)
    resolved_per_page = max(1, min(100, resolved_per_page))
    resolved_page = page
    if not resolved_page:
        resolved_page = (max(0, offset) // resolved_per_page) + 1
    resolved_page = max(1, min(resolved_page, 10000))

    records, pagination = shop_records(
        {"search": search, "label": label, "artist": artist, "genre": genre,
         "style": style, "country": country, "format": format, "year": year},
        resolved_page, resolved_per_page,
    )
    return {
        "records": records,
        "total": pagination["items"],
        "pagination": pagination,
        "source": "shop",
    }


@app.get("/api/records/{record_id}")
def get_record(record_id: int):
    check_id(record_id, "đĩa")
    if not local_price(record_id):
        return not_found("Đĩa này không có trong cửa hàng.")
    try:
        return discogs_client.normalize_release(resolve_release(record_id))
    except discogs_client.DiscogsError as exc:
        return discogs_error_response(exc)


# ------------------------------------------------------------------------
# Preorders -- the only local catalogue-like data that remains in SQLite
# ------------------------------------------------------------------------

@app.get("/api/preorders")
def list_preorders(conn: sqlite3.Connection = Depends(get_conn)):
    rows = conn.execute("SELECT * FROM preorders ORDER BY id").fetchall()
    result = []
    for row in rows:
        held = min(row["total_slots"], row["held"] + row["reserved_count"])
        pct = round(100 * held / row["total_slots"]) if row["total_slots"] else 0
        cover = None
        if row["discogs_release_id"]:
            try:
                cover = discogs_client.normalize_release(
                    cached_release(int(row["discogs_release_id"]))
                ).get("cover_image")
            except Exception:
                cover = None
        result.append({
            "id": row["id"],
            "discogs_release_id": row["discogs_release_id"],
            "cover_image": cover,
            "title": row["title"],
            "artist": row["artist"],
            "ships_label": row["ships_label"],
            "held": held,
            "total_slots": row["total_slots"],
            "price": row["price"],
            "pct": pct,
            "sold_out": held >= row["total_slots"],
        })
    return result


STATUS_LABELS = {
    "pending": "Chờ xác nhận",
    "confirmed": "Đã xác nhận",
    "arrived": "Đĩa đã về",
    "cancelled": "Đã hủy",
}
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")


def require_admin(request: Request):
    """
    Gate for every /api/admin/* endpoint: the X-Admin-Token header must equal
    ADMIN_TOKEN from .env. Wrong guesses are rate-limited much harder than normal use.
    """
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=503, detail="Máy chủ chưa đặt ADMIN_TOKEN trong file .env.")
    rate_limit(request, "admin", limit=240, window=60)
    token = request.headers.get("x-admin-token", "")
    if not secrets.compare_digest(token.encode("utf-8"), ADMIN_TOKEN.encode("utf-8")):
        rate_limit(request, "admin-fail", limit=10, window=300)
        raise HTTPException(status_code=403, detail="Mã quản trị không đúng.")


def reservation_payload(res):
    return {
        "code": res["code"],
        "status": res["status"],
        "status_label": STATUS_LABELS.get(res["status"], res["status"]),
        "preorder_id": res["preorder_id"],
        "title": res["title"],
        "artist": res["artist"],
        "ships_label": res["ships_label"],
        "price": res["price"],
        "name": res["name"],
        "created_at": res["created_at"],
        "updated_at": res["updated_at"],
    }


@app.post("/api/preorders/{preorder_id}/reserve", status_code=201)
def reserve_preorder(
    preorder_id: int,
    request: Request,
    payload: dict = Body(default={}),
    conn: sqlite3.Connection = Depends(get_conn),
):
    check_id(preorder_id, "đợt đặt trước")
    rate_limit(request, "reserve", limit=10, window=60)
    name = text_field(payload, "name", 120, required=True, label="họ tên")
    phone = database.normalize_phone(payload.get("phone") if isinstance(payload.get("phone"), (str, int)) else "")
    if not phone:
        return bad_request("Số điện thoại chưa đúng (ví dụ 0912345678).")
    try:
        res = database.create_reservation(conn, preorder_id, name, phone)
    except ValueError as exc:
        return bad_request(str(exc), status_code=409)
    data = reservation_payload(res)
    data["message"] = f"Đã ghi nhận giữ chỗ — mã {res['code']}. Bụi sẽ liên hệ để xác nhận."
    return data


@app.post("/api/reservations/lookup")
def lookup_reservation(
    request: Request,
    payload: dict = Body(default={}),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """POST (not GET) so the phone number never appears in URLs / access logs."""
    rate_limit(request, "lookup", limit=20, window=60)
    code = text_field(payload, "code", 20)
    phone = payload.get("phone") if isinstance(payload.get("phone"), (str, int)) else ""
    res = database.get_reservation(conn, code)
    # Same answer for "no such code" and "wrong phone" so codes can't be probed.
    if not res or database.normalize_phone(phone) != res["phone"]:
        return not_found("Không tìm thấy giữ chỗ với mã và số điện thoại này.")
    return reservation_payload(res)


@app.put("/api/admin/reservations/{code}")
def admin_set_reservation(
    code: str,
    request: Request,
    payload: dict = Body(default={}),
    conn: sqlite3.Connection = Depends(get_conn),
):
    require_admin(request)
    try:
        res = database.set_reservation_status(conn, code, payload.get("status"))
    except ValueError as exc:
        return bad_request(str(exc))
    return reservation_payload(res)


# ------------------------------------------------------------------------
# Non-persistent informational endpoints
# ------------------------------------------------------------------------

@app.get("/api/reviews")
def list_reviews():
    return SITE_REVIEWS


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


@app.post("/api/newsletter")
def subscribe_newsletter(payload: dict = Body(default={})):
    email = text_field(payload, "email", 254).lower()
    if not _EMAIL_RE.match(email):
        return bad_request("Vui lòng nhập email hợp lệ.")
    # Deliberately not stored in SQLite.
    return {"message": "Đã đăng ký — thư đầu tiên sẽ tới vào sáng thứ Năm"}


# ------------------------------------------------------------------------
# Customer accounts -- the signed session cookie only stores the user id
# ------------------------------------------------------------------------

MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 128  # also bounds the work a single login attempt can cost


def optional_phone(payload):
    """Phone is optional on an account: blank -> None, malformed -> 400."""
    raw = payload.get("phone")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    phone = database.normalize_phone(raw if isinstance(raw, (str, int)) and not isinstance(raw, bool) else "")
    if not phone:
        bad_request("Số điện thoại chưa đúng (ví dụ 0912345678).")
    return phone


def public_user(conn, user):
    return {
        "id": user["id"],
        "name": user["name"],
        "email": user["email"],
        "phone": user["phone"] or "",
        "last_shipping": database.last_shipping(conn, user["id"]),
    }


def current_user(request: Request, conn):
    """The logged-in account, or None. The id comes from the signed cookie; the row is re-read every time."""
    uid = request.session.get("uid")
    if type(uid) is not int:
        return None
    user = database.get_user(conn, uid)
    if not user:
        request.session.pop("uid", None)  # account no longer exists
    return user


def require_user(request: Request, conn):
    user = current_user(request, conn)
    if not user:
        raise HTTPException(status_code=401, detail="Vui lòng đăng nhập.")
    return user


@app.post("/api/auth/register", status_code=201)
def register(request: Request, payload: dict = Body(default={}), conn: sqlite3.Connection = Depends(get_conn)):
    rate_limit(request, "register", limit=10, window=600)
    name = text_field(payload, "name", 120, required=True, label="họ tên")
    email = text_field(payload, "email", 254, required=True, label="email").lower()
    if not _EMAIL_RE.match(email):
        return bad_request("Vui lòng nhập email hợp lệ.")
    password = payload.get("password")
    if not isinstance(password, str) or not password:
        return bad_request("Vui lòng nhập mật khẩu.")
    if len(password) < MIN_PASSWORD_LEN:
        return bad_request(f"Mật khẩu cần ít nhất {MIN_PASSWORD_LEN} ký tự.")
    if len(password) > MAX_PASSWORD_LEN:
        return bad_request(f"Mật khẩu quá dài (tối đa {MAX_PASSWORD_LEN} ký tự).")
    phone = optional_phone(payload)
    try:
        user = database.create_user(conn, email, name, phone, auth.hash_password(password))
    except ValueError as exc:
        return bad_request(str(exc), status_code=409)
    request.session["uid"] = user["id"]
    return {"user": public_user(conn, user)}


@app.post("/api/auth/login")
def login(request: Request, payload: dict = Body(default={}), conn: sqlite3.Connection = Depends(get_conn)):
    rate_limit(request, "login", limit=15, window=300)
    email = payload.get("email")
    password = payload.get("password")
    if not isinstance(email, str) or not isinstance(password, str) or not email.strip() or not password:
        return bad_request("Vui lòng nhập email và mật khẩu.")
    email = email.strip().lower()[:254]
    # Slow down guessing against one account even when it comes from many IPs.
    rate_limit(request, "login-email", limit=10, window=600, key=email)
    user = database.get_user_by_email(conn, email)
    # Same message and same work for "no such email" and "wrong password".
    valid = len(password) <= MAX_PASSWORD_LEN and auth.verify_password(
        password, user["password_hash"] if user else None
    )
    if not user or not valid:
        return bad_request("Email hoặc mật khẩu chưa đúng.", status_code=401)
    request.session["uid"] = user["id"]
    return {"user": public_user(conn, user)}


@app.post("/api/auth/logout")
def logout(request: Request):
    request.session.pop("uid", None)  # the cart stays: it belongs to the browser, not the account
    return {"user": None}


@app.get("/api/auth/me")
def whoami(request: Request, response: Response, conn: sqlite3.Connection = Depends(get_conn)):
    response.headers["Cache-Control"] = "no-store"
    user = current_user(request, conn)
    return {"user": public_user(conn, user) if user else None}


@app.put("/api/auth/me")
def update_profile(request: Request, payload: dict = Body(default={}), conn: sqlite3.Connection = Depends(get_conn)):
    user = require_user(request, conn)
    name = text_field(payload, "name", 120, required=True, label="họ tên")
    phone = optional_phone(payload)
    return {"user": public_user(conn, database.update_user(conn, user["id"], name, phone))}


@app.get("/api/orders")
def my_orders(request: Request, response: Response, conn: sqlite3.Connection = Depends(get_conn)):
    """Orders placed while logged in, newest first."""
    response.headers["Cache-Control"] = "no-store"
    user = require_user(request, conn)
    rows = conn.execute(
        "SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT 50", (user["id"],)
    ).fetchall()
    return {"orders": [customer_order(conn, r) for r in rows], "reasons": cases.reasons_payload()}


def customer_order(conn, row):
    """An order as its owner sees it, with what they can still ask for."""
    data = order_payload(conn, row["order_no"])
    data["cases"] = cases.customer_view(conn, row["id"])
    data["refunded"] = row["refunded"] or 0
    data["carrier"] = row["carrier"] or ""
    data["tracking_no"] = row["tracking_no"] or ""
    data["is_reship"] = row["parent_order_id"] is not None
    busy = cases.open_case_for(conn, row["id"], "customer") is not None
    data["has_open_request"] = busy
    status = row["status"]
    data["cancel_mode"] = None if busy else ("instant" if status == "pending" else "request" if status in ("confirmed", "shipping") else None)
    report_until = None
    if status == "completed":
        since = datetime.strptime(row["updated_at"] or row["created_at"], "%Y-%m-%d %H:%M:%S")
        until = since + timedelta(days=cases.RETURN_WINDOW_DAYS)
        if datetime.now(timezone.utc).replace(tzinfo=None) <= until:
            report_until = until.strftime("%Y-%m-%d %H:%M:%S")
    data["report_until"] = report_until
    data["can_report"] = not busy and not data["is_reship"] and (status == "shipping" or report_until is not None)
    return data


def _owner_request(request, conn, order_no, fn):
    user = require_user(request, conn)
    rate_limit(request, "order-request", limit=10, window=600)
    order_no = str(order_no or "").strip().upper()
    if not _ORDER_NO_RE.match(order_no):
        return not_found("Không tìm thấy đơn hàng.")
    payload = getattr(request.state, "payload", {})
    try:
        code = fn(conn, order_no, user["id"], payload.get("reason"), payload.get("detail"))
    except LookupError as exc:
        return not_found(str(exc))
    except ValueError as exc:
        return bad_request(str(exc), status_code=409)
    row = conn.execute("SELECT * FROM orders WHERE order_no = ?", (order_no,)).fetchone()
    return {"code": code, "order": customer_order(conn, row)}


@app.post("/api/orders/{order_no}/cancel")
def customer_cancel_order(order_no: str, request: Request, payload: dict = Body(default={}),
                          conn: sqlite3.Connection = Depends(get_conn)):
    """Owner cancels (pending: at once) or asks to cancel (confirmed / shipping). Body: {reason, detail}."""
    request.state.payload = payload
    return _owner_request(request, conn, order_no, cases.customer_cancel)


@app.post("/api/orders/{order_no}/report")
def customer_report_order(order_no: str, request: Request, payload: dict = Body(default={}),
                          conn: sqlite3.Connection = Depends(get_conn)):
    """Owner reports a delivery problem or asks to return. Body: {reason, detail}."""
    request.state.payload = payload
    return _owner_request(request, conn, order_no, cases.customer_report)


# ------------------------------------------------------------------------
# Cart -- stored in the signed session cookie, never in SQLite
# ------------------------------------------------------------------------

def cart_items(request: Request):
    return request.session.setdefault("cart", [])


def set_cart(request: Request, items):
    # Keep the cookie small (browsers drop cookies over ~4 KB, losing the cart and the login):
    # only id, qty and last known price are stored; title/artist are read from Discogs (cached).
    request.session["cart"] = [
        {"item_id": x["item_id"], "qty": x["qty"], "unit_price": x.get("unit_price")} for x in items
    ]


def _cart_metas(items):
    """Discogs metadata for each cart line, loaded in parallel; {} for a line Discogs can't give now."""
    return _load_meta([int(x["item_id"]) for x in items]) if items else {}


def _line_names(raw, metas):
    """(title, artist) of a cart line. Old cookies may still carry their own copy."""
    rid = int(raw["item_id"])
    meta = metas.get(rid) or {}
    title = meta.get("title") or raw.get("title") or f"Đĩa #{rid}"
    artist = meta.get("artist") or raw.get("artist") or ""
    return title, artist


def _line_check(item, qty, conn=None):
    """
    Current price + availability of one cart line, read live from the shop DB so
    price/stock edits apply immediately. Pass `conn` to read inside an open
    transaction (checkout does, so the check and the stock decrement agree).

    Returns a dict:
        unit, currency   price to charge (None when the line can't be bought)
        available        tracked stock, or None when the shop doesn't count it
        problem, code    Vietnamese message + machine code, both None when OK.
                         code: out_of_stock | insufficient_stock | no_price | not_in_shop
    """
    rid = int(item["item_id"])
    shop = database.get_price(conn, rid) if conn is not None else local_price(rid)

    def fail(problem, code, available=None):
        return {"unit": None, "currency": None, "available": available, "problem": problem, "code": code}

    if not shop:
        return fail("Đĩa này không có trong cửa hàng.", "not_in_shop")
    stock = shop.get("stock")
    if shop["stock_status"] == "out_of_stock" or (stock is not None and stock <= 0):
        return fail("Đĩa này đã hết hàng.", "out_of_stock", 0)
    if shop.get("price") is None:
        return fail("Đĩa này chưa có giá, vui lòng liên hệ cửa hàng.", "no_price", stock)
    if stock is not None and qty > stock:
        return fail(f"Chỉ còn {stock} bản.", "insufficient_stock", stock)
    return {
        "unit": int(shop["price"]),
        "currency": shop.get("currency") or "VND",
        "available": stock,
        "problem": None,
        "code": None,
    }


def cart_payload(request: Request):
    items = []
    raw_items = cart_items(request)
    metas = _cart_metas(raw_items)
    for raw in raw_items:
        check = _line_check(raw, raw["qty"])
        unit, currency = check["unit"], check["currency"]
        if unit is None:
            unit, currency = raw.get("unit_price") or 0, raw.get("price_currency") or "VND"  # show last known price
        title, artist = _line_names(raw, metas)
        line = {
            "item_id": raw["item_id"],
            "item_type": "record",
            "title": title,
            "artist": artist,
            "unit_price": unit,
            "price_currency": currency,
            "qty": raw["qty"],
        }
        line["line_total"] = unit * raw["qty"]
        if check["available"] is not None:
            line["available"] = check["available"]
        if check["problem"]:
            line["problem"] = check["problem"]
            line["problem_code"] = check["code"]
        items.append(line)
    # Lines that can't be bought (sold out, not enough stock...) are shown with
    # their problem but never counted in the amount the customer would pay.
    payable = [item for item in items if "problem" not in item]
    return {
        "items": items,
        "subtotal": sum(item["line_total"] for item in payable),
        "count": sum(item["qty"] for item in items),
        "unavailable": len(items) - len(payable),
        "max_qty": MAX_QTY,  # lets the quantity inputs use the same limit as the server
    }


def compute_shipping_fee(shipping_method, subtotal):
    if shipping_method == "express":
        return SHIPPING_EXPRESS_FEE
    return 0 if subtotal >= FREE_SHIPPING_THRESHOLD else SHIPPING_STANDARD_FEE


@app.get("/api/cart")
def view_cart(request: Request):
    return cart_payload(request)


@app.post("/api/cart/items")
def add_cart_item(request: Request, payload: dict = Body(default={})):
    item_type = payload.get("item_type", "record")
    if item_type != "record":
        return bad_request("Sản phẩm không hợp lệ.")
    release_id = parse_release_id(payload.get("item_id"))
    qty = parse_qty(payload.get("qty", 1))

    items = cart_items(request)
    existing = next((x for x in items if x["item_id"] == str(release_id)), None)
    new_qty = qty + (existing["qty"] if existing else 0)
    if new_qty > MAX_QTY:
        return bad_request(f"Mỗi đĩa chỉ đặt tối đa {MAX_QTY} bản.", status_code=409)
    if not existing and len(items) >= MAX_CART_LINES:
        return bad_request("Giỏ hàng đã đầy, vui lòng thanh toán trước khi thêm đĩa mới.", status_code=409)

    # Price/stock is a cheap local read: check it first so a sold-out disc says so
    # even when Discogs is slow or down.
    check = _line_check({"item_id": str(release_id)}, new_qty)
    if check["problem"]:
        message = check["problem"]
        if check["code"] == "insufficient_stock" and existing:
            message = f"Bạn đã có {existing['qty']} bản trong giỏ, đĩa này chỉ còn {check['available']} bản."
        return bad_request(message, status_code=409, code=check["code"])
    # The shop DB row is all a cart line needs (title/artist are looked up when the cart is
    # shown), so a disc stays buyable while Discogs is down, just like in the catalogue.
    unit_price = check["unit"]

    # Identifies this cart in its order, so a double-submitted checkout can't create two orders.
    request.session.setdefault("cart_id", secrets.token_hex(12))
    if existing:
        existing["qty"] = new_qty
        existing["unit_price"] = unit_price
    else:
        items.append({"item_id": str(release_id), "qty": qty, "unit_price": unit_price})
    set_cart(request, items)
    return cart_payload(request)


@app.put("/api/cart/items/{item_id}")
def set_cart_item_qty(item_id: str, request: Request, payload: dict = Body(default={})):
    qty = parse_qty(payload.get("qty"))
    items = cart_items(request)
    for item in items:
        if item["item_id"] == str(item_id):
            check = _line_check(item, qty)
            # Lowering a line is how the customer fixes it, so it is always allowed;
            # only raising the quantity of a line that can't be bought is refused.
            if check["problem"] and qty > item["qty"]:
                return bad_request(check["problem"], status_code=409, code=check["code"])
            item["qty"] = qty
            set_cart(request, items)
            return cart_payload(request)
    return not_found("Sản phẩm không có trong giỏ hàng")


@app.delete("/api/cart/items/{item_id}")
def remove_cart_item(item_id: str, request: Request):
    items = [item for item in cart_items(request) if item["item_id"] != str(item_id)]
    set_cart(request, items)
    return cart_payload(request)


@app.delete("/api/cart")
def clear_cart(request: Request):
    request.session.pop("cart", None)
    request.session.pop("cart_id", None)
    return cart_payload(request)


# ------------------------------------------------------------------------
# Checkout -- the order is the only cart snapshot persisted in SQLite
# ------------------------------------------------------------------------

_ORDER_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

# What the customer sees in "Tài khoản".
ORDER_STATUS_LABELS = {
    "pending": "Chờ xác nhận",
    "confirmed": "Đã xác nhận",
    "shipping": "Đang giao",
    "completed": "Đã giao",
    "failed": "Giao không thành công",
    "lost": "Thất lạc, Bụi đang xử lý",
    "returned": "Đã trả hàng",
    "cancelled": "Đã hủy",
}


def generate_order_no(conn):
    return database.new_order_no(conn)


@app.post("/api/checkout", status_code=201)
def checkout(request: Request, payload: dict = Body(default={}), conn: sqlite3.Connection = Depends(get_conn)):
    name = text_field(payload, "name", 120, required=True, label="họ tên")
    address = text_field(payload, "address", 300, required=True, label="địa chỉ nhận hàng")
    city = text_field(payload, "city", 100)
    note = text_field(payload, "note", 500)
    raw_phone = payload.get("phone")
    phone = database.normalize_phone(raw_phone if isinstance(raw_phone, (str, int)) and not isinstance(raw_phone, bool) else "")
    if not phone:
        return bad_request("Số điện thoại chưa đúng (ví dụ 0912345678).")
    shipping_method = payload.get("shipping", "standard")
    payment_method = payload.get("payment", "cod")
    if not isinstance(shipping_method, str) or shipping_method not in ("standard", "express"):
        return bad_request("Phương thức vận chuyển không hợp lệ.")
    if not isinstance(payment_method, str) or payment_method not in PAYMENT_LABELS:
        return bad_request("Phương thức thanh toán không hợp lệ.")

    items = cart_items(request)
    if not items:
        return bad_request("Giỏ hàng đang trống.")
    user = current_user(request, conn)  # None = guest checkout (still allowed)
    user_id = user["id"] if user else None
    cart_key = request.session.get("cart_id")
    if not isinstance(cart_key, str):
        cart_key = None
    # Titles for the order snapshot: fetched before the write lock is taken (may hit Discogs).
    metas = _cart_metas(items)

    # Everything below runs in one write transaction: prices are re-read from the
    # DB (never trusted from the cookie) and stock is checked + decremented atomically.
    conn.execute("BEGIN IMMEDIATE")
    try:
        # A second request carrying the same cart (double click, retry after a timeout)
        # waits for the first one above, then gets the order that one already created.
        if cart_key:
            done = conn.execute("SELECT order_no FROM orders WHERE cart_key = ?", (cart_key,)).fetchone()
            if done:
                conn.rollback()
                request.session.pop("cart", None)
                request.session.pop("cart_id", None)
                return order_payload(conn, done["order_no"])
        priced = []
        problems = []
        for item in items:
            qty = parse_qty(item.get("qty"))
            check = _line_check(item, qty, conn)
            title, artist = _line_names(item, metas)
            if check["problem"]:
                problems.append(f"{title}: {check['problem']}")
                continue
            priced.append((item, qty, check["unit"], title, artist))
        if problems:
            bad_request(
                "Một số đĩa trong giỏ không thể đặt. " + " ".join(problems),
                status_code=409,
                code="cart_unavailable",
            )

        subtotal = sum(unit * qty for _, qty, unit, _t, _a in priced)
        shipping_fee = compute_shipping_fee(shipping_method, subtotal)
        total = subtotal + shipping_fee

        order_no = generate_order_no(conn)
        cur = conn.execute(
            """
            INSERT INTO orders
                (order_no, name, phone, address, city, note, shipping_method,
                 shipping_fee, payment_method, subtotal, total, user_id, cart_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (order_no, name, phone, address, city, note, shipping_method,
             shipping_fee, payment_method, subtotal, total, user_id, cart_key),
        )
        order_id = cur.lastrowid

        for item, qty, unit, title, artist in priced:
            conn.execute(
                """
                INSERT INTO order_items
                    (order_id, item_id, item_type, title, artist, unit_price, qty)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (order_id, item["item_id"], "record", title, artist, unit, qty),
            )
            # Decrement shop stock (only rows that track a stock number).
            res = conn.execute(
                "UPDATE release_prices SET stock = stock - ?, "
                "stock_status = CASE WHEN stock - ? <= 0 THEN 'out_of_stock' ELSE stock_status END, "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE discogs_release_id = ? AND stock IS NOT NULL AND stock >= ?",
                (qty, qty, int(item["item_id"]), qty),
            )
            tracked = conn.execute(
                "SELECT 1 FROM release_prices WHERE discogs_release_id = ? AND stock IS NOT NULL",
                (int(item["item_id"]),),
            ).fetchone()
            if tracked and res.rowcount == 0:
                bad_request(f"{title}: không đủ hàng.", status_code=409, code="insufficient_stock")
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    request.session.pop("cart", None)
    request.session.pop("cart_id", None)
    return order_payload(conn, order_no)


def order_payload(conn, order_no):
    order = conn.execute(
        "SELECT * FROM orders WHERE order_no = ?", (order_no,)
    ).fetchone()
    items = conn.execute(
        """
        SELECT item_id, item_type, title, artist, unit_price, qty
        FROM order_items
        WHERE order_id = ?
        ORDER BY id
        """,
        (order["id"],),
    ).fetchall()

    return {
        "order_no": order["order_no"],
        "name": order["name"],
        "phone": order["phone"],
        "address": order["address"],
        "city": order["city"],
        "note": order["note"],
        "shipping_method": order["shipping_method"],
        "shipping_fee": order["shipping_fee"],
        "payment_method": order["payment_method"],
        "payment_label": PAYMENT_LABELS[order["payment_method"]],
        "subtotal": order["subtotal"],
        "total": order["total"],
        "status": order["status"],
        "status_label": ORDER_STATUS_LABELS.get(order["status"], order["status"]),
        "created_at": order["created_at"],
        "items": [
            {
                "item_id": item["item_id"],
                "item_type": item["item_type"],
                "title": item["title"],
                "artist": item["artist"],
                "unit_price": item["unit_price"],
                "qty": item["qty"],
                "line_total": item["unit_price"] * item["qty"],
            }
            for item in items
        ],
    }


# ------------------------------------------------------------------------
# Admin -- manage products and stock (page: /admin, auth: ADMIN_TOKEN in .env)
# ------------------------------------------------------------------------

LOW_STOCK = 2            # "sắp hết" = 1..LOW_STOCK copies left (the shop pages use the same number)
ADMIN_PER_PAGE = 20
MAX_PRICE = 1_000_000_000
MAX_STOCK = 100_000
ADMIN_STATUS_FILTERS = ("all", "available", "low", "out", "untracked")
_DISCOGS_REF_RE = re.compile(r"/release/(\d+)")


def parse_discogs_ref(value):
    """A Discogs release id ('249504') or a release URL -> int."""
    text = str(value if value is not None else "").strip()
    found = _DISCOGS_REF_RE.search(text)
    text = found.group(1) if found else text
    if not (text.isascii() and text.isdigit() and 0 < int(text) <= MAX_ID):
        bad_request("Mã hoặc đường dẫn Discogs không hợp lệ. Ví dụ: 249504 hoặc https://www.discogs.com/release/249504")
    return int(text)


def admin_int(payload, key, *, lo, hi, label, nullable=False):
    """Read a whole number from a JSON body (no strings, no booleans, no floats)."""
    value = payload.get(key)
    if value is None:
        if nullable:
            return None
        bad_request(f"Vui lòng nhập {label}.")
    if type(value) is not int or not (lo <= value <= hi):
        bad_request(f"{label.capitalize()} phải là số nguyên từ {lo:,} đến {hi:,}.".replace(",", "."))
    return value


def stock_state(row):
    """out | low | ok | untracked -- what the shop shows for this product."""
    stock = row.get("stock")
    if row["stock_status"] == "out_of_stock" or (stock is not None and stock <= 0):
        return "out"
    if stock is None:
        return "untracked"
    return "low" if stock <= LOW_STOCK else "ok"


def _release_meta(rid):
    """Discogs metadata for a list row; a Discogs hiccup must not break the admin page."""
    try:
        return discogs_client.normalize_release(cached_release(rid))
    except Exception as exc:
        log.warning("admin: metadata for release %s unavailable: %s", rid, exc)
        return {}


def admin_product(row, item=None):
    item = item or {}
    rid = row["discogs_release_id"]
    return {
        "id": rid,
        "title": item.get("title") or f"Đĩa #{rid}",
        "artist": item.get("artist") or "",
        "year": item.get("year"),
        "cover": item.get("thumb") or item.get("cover_image") or "",
        "price": row["price"],
        "currency": row["currency"],
        "stock": row["stock"],
        "stock_status": row["stock_status"],
        "state": stock_state(row),
        "updated_at": row["updated_at"],
    }


def _load_meta(ids):
    with ThreadPoolExecutor(max_workers=6) as pool:
        return dict(zip(ids, pool.map(_release_meta, ids)))


@app.get("/api/admin/products")
def admin_list_products(
    request: Request,
    search: str = "",
    status: str = "all",
    page: int = 1,
    per_page: int = ADMIN_PER_PAGE,
    conn: sqlite3.Connection = Depends(get_conn),
):
    require_admin(request)
    if status not in ADMIN_STATUS_FILTERS:
        bad_request("Bộ lọc không hợp lệ.")
    per_page = max(1, min(100, per_page))

    rows = database.list_prices(conn)
    states = {r["discogs_release_id"]: stock_state(r) for r in rows}
    summary = {
        "total": len(rows),
        "available": sum(1 for s in states.values() if s != "out"),
        "low": sum(1 for s in states.values() if s == "low"),
        "out": sum(1 for s in states.values() if s == "out"),
        "untracked": sum(1 for s in states.values() if s == "untracked"),
    }
    if status == "available":
        rows = [r for r in rows if states[r["discogs_release_id"]] != "out"]
    elif status != "all":
        rows = [r for r in rows if states[r["discogs_release_id"]] == status]

    metas = {}
    words = fold(search.strip()[:100]).split()[:8]
    if words:  # searching needs every candidate's title/artist
        metas = _load_meta([r["discogs_release_id"] for r in rows])
        rows = [
            r for r in rows
            if all(w in fold(search_text(metas[r["discogs_release_id"]]) + " " + str(r["discogs_release_id"])) for w in words)
        ]

    total = len(rows)
    pages = max(1, -(-total // per_page))
    page = max(1, min(page, pages))
    chunk = rows[(page - 1) * per_page: page * per_page]
    missing = [r["discogs_release_id"] for r in chunk if r["discogs_release_id"] not in metas]
    metas.update(_load_meta(missing))
    return {
        "products": [admin_product(r, metas.get(r["discogs_release_id"])) for r in chunk],
        "pagination": {"page": page, "pages": pages, "per_page": per_page, "items": total},
        "summary": summary,
        "low_stock": LOW_STOCK,
    }


@app.get("/api/admin/lookup")
def admin_lookup(request: Request, q: str = "", conn: sqlite3.Connection = Depends(get_conn)):
    """Find a Discogs release to add: by name / artist, or by release id / URL."""
    require_admin(request)
    q = q.strip()[:200]
    if not q:
        bad_request("Nhập tên đĩa, nghệ sĩ hoặc mã Discogs để tìm.")
    try:
        if q.isdigit() or _DISCOGS_REF_RE.search(q):
            items = [discogs_client.normalize_release(resolve_release(parse_discogs_ref(q)))]
        else:
            data = discogs_client.search(q, per_page=10)
            items = [discogs_client.normalize_search_result(x) for x in data.get("results", [])]
    except discogs_client.DiscogsError as exc:
        return discogs_error_response(exc)
    in_shop = {r["discogs_release_id"] for r in database.list_prices(conn)}
    return {
        "results": [
            {
                "id": i["id"],
                "title": i.get("title") or f"Đĩa #{i['id']}",
                "artist": i.get("artist") or "",
                "year": i.get("year"),
                "format": i.get("format") or "",
                "label": i.get("label") or "",
                "cover": i.get("thumb") or i.get("cover_image") or "",
                "in_shop": i["id"] in in_shop,
            }
            for i in items if i.get("id")
        ]
    }


@app.post("/api/admin/products", status_code=201)
def admin_add_product(request: Request, payload: dict = Body(default={}), conn: sqlite3.Connection = Depends(get_conn)):
    require_admin(request)
    rid = parse_discogs_ref(payload.get("release"))
    price = admin_int(payload, "price", lo=1, hi=MAX_PRICE, label="giá bán")
    stock = admin_int(payload, "stock", lo=0, hi=MAX_STOCK, label="số lượng tồn kho", nullable=True)
    if database.get_price(conn, rid):
        return bad_request("Đĩa này đã có trong cửa hàng.", status_code=409, code="exists")
    try:
        release = resolve_release(rid)  # proves the release exists and gives us its title for the answer
    except discogs_client.DiscogsError as exc:
        return discogs_error_response(exc)
    try:
        row = database.add_product(conn, rid, price, stock)
    except ValueError as exc:
        return bad_request(str(exc), status_code=409, code="exists")
    return admin_product(row, discogs_client.normalize_release(release))


@app.put("/api/admin/products/{release_id}")
def admin_update_product(
    release_id: int,
    request: Request,
    payload: dict = Body(default={}),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Body: any of price (number or null = "Liên hệ"), stock (number or null = not counted),
    or add_stock (+N received / -N written off). add_stock can't be combined with stock."""
    require_admin(request)
    check_id(release_id, "đĩa")
    known = {"price", "stock", "add_stock"}
    if not payload or set(payload) - known:
        bad_request("Cần gửi price, stock hoặc add_stock.")
    if "add_stock" in payload and "stock" in payload:
        bad_request("Chỉ dùng một trong hai: stock hoặc add_stock.")
    try:
        if "add_stock" in payload:
            delta = admin_int(payload, "add_stock", lo=-MAX_STOCK, hi=MAX_STOCK, label="số lượng nhập thêm")
            if delta == 0:
                bad_request("Số lượng nhập thêm phải khác 0.")
            row = database.add_stock(conn, release_id, delta)
        else:
            changes = {}
            if "price" in payload:
                changes["price"] = admin_int(payload, "price", lo=1, hi=MAX_PRICE, label="giá bán", nullable=True)
            if "stock" in payload:
                changes["stock"] = admin_int(payload, "stock", lo=0, hi=MAX_STOCK, label="số lượng tồn kho", nullable=True)
            row = database.update_product(conn, release_id, **changes)
    except ValueError as exc:
        message = str(exc)
        if "không có trong cửa hàng" in message:
            return not_found(message)
        return bad_request(message, status_code=409)
    return admin_product(row, _release_meta(release_id))


@app.delete("/api/admin/products/{release_id}")
def admin_delete_product(release_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    require_admin(request)
    check_id(release_id, "đĩa")
    try:
        database.delete_product(conn, release_id)
    except ValueError as exc:
        return not_found(str(exc))
    return {"deleted": release_id}


# ------------------------------------------------------------------------
# Admin -- dashboard, order fulfilment, warehouse (page: /admin)
# ------------------------------------------------------------------------

# What the shop sees for each status (the customer sees ORDER_STATUS_LABELS).
ADMIN_ORDER_LABELS = {
    "pending": "Chờ xác nhận",
    "confirmed": "Chờ gửi hàng",
    "shipping": "Đang giao",
    "completed": "Đã giao",
    "failed": "Giao không thành công",
    "lost": "Thất lạc",
    "returned": "Đã trả hàng",
    "cancelled": "Đã hủy",
}
# Button text for each move the shop can make (same words as the toast afterwards).
ORDER_ACTION_LABELS = {
    "confirmed": "Xác nhận đơn",
    "shipping": "Đã gửi hàng",
    "completed": "Đã giao xong",
    "failed": "Giao không thành công",
    "lost": "Thất lạc",
    "returned": "Đã nhận hàng trả về",
    "cancelled": "Hủy đơn",
}
TO_SHIP = ("pending", "confirmed")
ORDER_TABS = {
    "all": None,
    "to_ship": TO_SHIP,
    "shipping": ("shipping",),
    "completed": ("completed",),
    "failed": ("failed",),
    "lost": ("lost",),
    "returned": ("returned",),
    "cancelled": ("cancelled",),
}
# Hours the shop has to hand an order to the carrier, counted from checkout.
SHIP_SLA_HOURS = {"express": 24, "standard": 48}
_DEADLINE_SQL = (
    "datetime(o.created_at, CASE o.shipping_method "
    + " ".join(f"WHEN '{k}' THEN '+{v} hours'" for k, v in SHIP_SLA_HOURS.items())
    + " ELSE '+48 hours' END)"
)
_TO_SHIP_SQL = "o.status IN ('pending', 'confirmed')"
_OVERDUE_SQL = f"({_TO_SHIP_SQL} AND {_DEADLINE_SQL} < CURRENT_TIMESTAMP)"
_DUE_24H_SQL = (
    f"({_TO_SHIP_SQL} AND {_DEADLINE_SQL} >= CURRENT_TIMESTAMP "
    f"AND {_DEADLINE_SQL} < datetime('now', '+24 hours'))"
)
_VN_DAY_SQL = "date(o.created_at, '+7 hours')"
_ORDER_NO_RE = re.compile(r"^BUI-[A-Z0-9]{6,16}$")
DASHBOARD_PERIODS = (1, 7, 30)
MAX_EXPORT_ROWS = 5000


def vn_today():
    return (datetime.now(timezone.utc) + timedelta(hours=7)).date()


def check_order_no(value):
    text = str(value or "").strip().upper()
    if not _ORDER_NO_RE.match(text):
        raise HTTPException(status_code=404, detail="Không tìm thấy đơn hàng.")
    return text


def _period_metrics(conn, start, end):
    """GMV / customers / SKU orders / visitors for Vietnam days start..end (inclusive)."""
    args = (start.isoformat(), end.isoformat())
    o = conn.execute(
        f"SELECT COALESCE(SUM(o.subtotal), 0) AS gmv, COUNT(*) AS orders, "
        f"COUNT(DISTINCT o.phone) AS customers FROM orders o "
        f"WHERE o.status != 'cancelled' AND o.parent_order_id IS NULL AND {_VN_DAY_SQL} BETWEEN ? AND ?",
        args,
    ).fetchone()
    sku = conn.execute(
        f"SELECT COUNT(*) AS lines, COALESCE(SUM(oi.qty), 0) AS units FROM order_items oi "
        f"JOIN orders o ON o.id = oi.order_id "
        f"WHERE o.status != 'cancelled' AND o.parent_order_id IS NULL AND {_VN_DAY_SQL} BETWEEN ? AND ?",
        args,
    ).fetchone()
    visitors = conn.execute(
        "SELECT COUNT(DISTINCT visitor) FROM site_visits WHERE day BETWEEN ? AND ?", args
    ).fetchone()[0]
    return {
        "gmv": o["gmv"], "orders": o["orders"], "customers": o["customers"],
        "sku_orders": sku["lines"], "units": sku["units"], "visitors": visitors,
    }


def inventory_summary(conn):
    rows = database.list_prices(conn)
    states = [stock_state(r) for r in rows]
    tracked = [r for r in rows if r["stock"] is not None]
    return {
        "products": len(rows),
        "units": sum(max(0, r["stock"]) for r in tracked),
        "value": sum(max(0, r["stock"]) * (r["price"] or 0) for r in tracked),
        "low": states.count("low"),
        "out": states.count("out"),
        "untracked": states.count("untracked"),
    }


def fulfilment_summary(conn):
    row = conn.execute(
        f"""
        SELECT
            SUM(CASE WHEN {_TO_SHIP_SQL} THEN 1 ELSE 0 END) AS to_ship,
            SUM(CASE WHEN {_TO_SHIP_SQL} AND o.shipping_method = 'express' THEN 1 ELSE 0 END) AS express,
            SUM(CASE WHEN o.status = 'pending' THEN 1 ELSE 0 END) AS unconfirmed,
            SUM(CASE WHEN {_OVERDUE_SQL} THEN 1 ELSE 0 END) AS overdue,
            SUM(CASE WHEN {_DUE_24H_SQL} THEN 1 ELSE 0 END) AS due_24h,
            SUM(CASE WHEN o.status = 'shipping' THEN 1 ELSE 0 END) AS shipping,
            SUM(CASE WHEN o.status = 'failed' THEN 1 ELSE 0 END) AS failed
        FROM orders o
        """
    ).fetchone()
    return {k: row[k] or 0 for k in row.keys()}


@app.get("/api/admin/dashboard")
def admin_dashboard(request: Request, response: Response, days: int = 7,
                    conn: sqlite3.Connection = Depends(get_conn)):
    """Home of the admin page: what needs doing now + how the shop did over `days` days."""
    require_admin(request)
    response.headers["Cache-Control"] = "no-store"
    if days not in DASHBOARD_PERIODS:
        bad_request("Khoảng thời gian không hợp lệ.")
    end = vn_today()
    start = end - timedelta(days=days - 1)
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)
    current = _period_metrics(conn, start, end)
    previous = _period_metrics(conn, prev_start, prev_end)

    by_day = {
        r["day"]: r for r in conn.execute(
            f"SELECT {_VN_DAY_SQL} AS day, COALESCE(SUM(o.subtotal), 0) AS gmv, COUNT(*) AS orders "
            f"FROM orders o WHERE o.status != 'cancelled' AND o.parent_order_id IS NULL AND {_VN_DAY_SQL} BETWEEN ? AND ? GROUP BY day",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    }
    daily = []
    for i in range(days):
        day = (start + timedelta(days=i)).isoformat()
        r = by_day.get(day)
        daily.append({"day": day, "gmv": r["gmv"] if r else 0, "orders": r["orders"] if r else 0})

    top = conn.execute(
        f"""
        SELECT oi.item_id, MAX(oi.title) AS title, MAX(oi.artist) AS artist,
               SUM(oi.qty) AS qty, SUM(oi.qty * oi.unit_price) AS revenue
        FROM order_items oi JOIN orders o ON o.id = oi.order_id
        WHERE o.status != 'cancelled' AND o.parent_order_id IS NULL AND {_VN_DAY_SQL} BETWEEN ? AND ?
        GROUP BY oi.item_id ORDER BY qty DESC, revenue DESC LIMIT 5
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    top_ids = [int(r["item_id"]) for r in top if str(r["item_id"]).isdigit()]
    metas = _load_meta(top_ids) if top_ids else {}

    period_counts = conn.execute(
        f"SELECT SUM(CASE WHEN o.status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled, "
        f"SUM(CASE WHEN o.status = 'returned' THEN 1 ELSE 0 END) AS returned "
        f"FROM orders o WHERE {_VN_DAY_SQL} BETWEEN ? AND ?",
        (start.isoformat(), end.isoformat()),
    ).fetchone()
    reservations = conn.execute(
        "SELECT COUNT(*) FROM preorder_reservations WHERE status = 'pending'"
    ).fetchone()[0]
    case_counts = cases_summary(conn)

    return {
        "period": {"days": days, "start": start.isoformat(), "end": end.isoformat(),
                   "previous_start": prev_start.isoformat(), "previous_end": prev_end.isoformat()},
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "metrics": {k: {"value": current[k], "previous": previous[k]} for k in current},
        "todo": {
            **fulfilment_summary(conn),
            "cancelled": period_counts["cancelled"] or 0,
            "returned": period_counts["returned"] or 0,
            "reservations": reservations,
            "cases_open": case_counts["active"],
            "cases_overdue": case_counts["overdue"],
            "cases_untouched": case_counts["untouched"],
        },
        "inventory": inventory_summary(conn),
        "daily": daily,
        "top_products": [
            {
                "id": r["item_id"], "title": r["title"], "artist": r["artist"] or "",
                "qty": r["qty"], "revenue": r["revenue"],
                "cover": (metas.get(int(r["item_id"])) or {}).get("thumb") or ""
                if str(r["item_id"]).isdigit() else "",
            }
            for r in top
        ],
    }


def _orders_where(tab, q, shipping, payment, due, status):
    """WHERE clause + args shared by the order list and the CSV export."""
    if tab not in ORDER_TABS:
        bad_request("Nhóm đơn hàng không hợp lệ.")
    where, args = [], []
    statuses = ORDER_TABS[tab]
    if statuses:
        where.append(f"o.status IN ({', '.join('?' * len(statuses))})")
        args.extend(statuses)
    if status:
        if status not in database.ORDER_STATUSES or (statuses and status not in statuses):
            bad_request("Trạng thái không hợp lệ.")
        where.append("o.status = ?")
        args.append(status)
    if shipping:
        if shipping not in SHIP_SLA_HOURS:
            bad_request("Cách giao hàng không hợp lệ.")
        where.append("o.shipping_method = ?")
        args.append(shipping)
    if payment:
        if payment not in PAYMENT_LABELS:
            bad_request("Phương thức thanh toán không hợp lệ.")
        where.append("o.payment_method = ?")
        args.append(payment)
    if due == "overdue":
        where.append(_OVERDUE_SQL)
    elif due == "due_24h":
        where.append(_DUE_24H_SQL)
    elif due:
        bad_request("Bộ lọc hạn gửi không hợp lệ.")
    q = (q or "").strip()[:100]
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        parts = ["o.order_no LIKE ? ESCAPE '\\'", "o.name LIKE ? ESCAPE '\\'",
                 "EXISTS (SELECT 1 FROM order_items s WHERE s.order_id = o.id AND s.title LIKE ? ESCAPE '\\')"]
        args.extend([like.upper(), like, like])
        digits = re.sub(r"\D", "", q)
        if len(digits) >= 3:
            parts.append("o.phone LIKE ?")
            args.append("%" + digits + "%")
        where.append("(" + " OR ".join(parts) + ")")
    return (" WHERE " + " AND ".join(where)) if where else "", args


_ORDER_SORTS = {
    "newest": "o.id DESC",
    "oldest": "o.id ASC",
    "deadline": f"{_DEADLINE_SQL} ASC, o.id ASC",
    "total_desc": "o.total DESC, o.id DESC",
}

_ORDER_LIST_SQL = f"""
    SELECT o.*, {_DEADLINE_SQL} AS deadline, {_OVERDUE_SQL} AS overdue,
        (SELECT COUNT(*) FROM order_items i WHERE i.order_id = o.id) AS lines,
        (SELECT COALESCE(SUM(i.qty), 0) FROM order_items i WHERE i.order_id = o.id) AS units,
        (SELECT i.title FROM order_items i WHERE i.order_id = o.id ORDER BY i.id LIMIT 1) AS first_title,
        (SELECT i.item_id FROM order_items i WHERE i.order_id = o.id ORDER BY i.id LIMIT 1) AS first_item,
        (SELECT COUNT(*) FROM cases c WHERE c.order_id = o.id
            AND c.status IN ('open', 'waiting_carrier', 'waiting_customer')) AS open_cases
    FROM orders o
"""


def admin_order_row(row, metas=None):
    first = str(row["first_item"] or "")
    meta = (metas or {}).get(int(first)) if first.isdigit() else None
    return {
        "order_no": row["order_no"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "name": row["name"],
        "phone": row["phone"],
        "city": row["city"] or "",
        "shipping_method": row["shipping_method"],
        "payment_method": row["payment_method"],
        "payment_label": PAYMENT_LABELS.get(row["payment_method"], row["payment_method"]),
        "subtotal": row["subtotal"],
        "shipping_fee": row["shipping_fee"],
        "total": row["total"],
        "status": row["status"],
        "status_label": ADMIN_ORDER_LABELS.get(row["status"], row["status"]),
        "deadline": row["deadline"] if row["status"] in TO_SHIP else None,
        "overdue": bool(row["overdue"]),
        "lines": row["lines"],
        "units": row["units"],
        "first_title": row["first_title"] or "",
        "cover": (meta or {}).get("thumb") or "",
        "actions": [{"status": s, "label": "Giao lại" if (row["status"], s) == ("failed", "shipping")
                     else ORDER_ACTION_LABELS[s]}
                    for s in database.ORDER_TRANSITIONS.get(row["status"], ())],
        "has_account": row["user_id"] is not None,
        "open_cases": row["open_cases"],
        "carrier": row["carrier"] or "",
        "tracking_no": row["tracking_no"] or "",
        "refunded": row["refunded"] or 0,
        "is_reship": row["parent_order_id"] is not None,
    }


@app.get("/api/admin/orders")
def admin_list_orders(
    request: Request,
    response: Response,
    tab: str = "to_ship",
    q: str = "",
    shipping: str = "",
    payment: str = "",
    due: str = "",
    status: str = "",
    sort: str = "newest",
    page: int = 1,
    per_page: int = 20,
    conn: sqlite3.Connection = Depends(get_conn),
):
    require_admin(request)
    response.headers["Cache-Control"] = "no-store"
    if sort not in _ORDER_SORTS:
        bad_request("Cách sắp xếp không hợp lệ.")
    per_page = max(1, min(100, per_page))
    where, args = _orders_where(tab, q, shipping, payment, due, status)
    total = conn.execute(f"SELECT COUNT(*) FROM orders o{where}", args).fetchone()[0]
    pages = max(1, -(-total // per_page))
    page = max(1, min(page, pages))
    rows = conn.execute(
        f"{_ORDER_LIST_SQL}{where} ORDER BY {_ORDER_SORTS[sort]} LIMIT ? OFFSET ?",
        (*args, per_page, (page - 1) * per_page),
    ).fetchall()
    ids = sorted({int(r["first_item"]) for r in rows if str(r["first_item"] or "").isdigit()})
    metas = _load_meta(ids) if ids else {}

    by_status = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM orders GROUP BY status").fetchall()}
    counts = {key: (sum(by_status.values()) if sts is None else sum(by_status.get(s, 0) for s in sts))
              for key, sts in ORDER_TABS.items()}
    return {
        "orders": [admin_order_row(r, metas) for r in rows],
        "pagination": {"page": page, "pages": pages, "per_page": per_page, "items": total},
        "counts": counts,
        "summary": {**fulfilment_summary(conn), "cancelled": by_status.get("cancelled", 0),
                    "returned": by_status.get("returned", 0)},
        "sla_hours": SHIP_SLA_HOURS,
    }


def _csv_cell(value):
    """Customers type names/notes: never let a cell start a spreadsheet formula."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@app.get("/api/admin/orders/export")
def admin_export_orders(
    request: Request,
    tab: str = "all",
    q: str = "",
    shipping: str = "",
    payment: str = "",
    due: str = "",
    status: str = "",
    conn: sqlite3.Connection = Depends(get_conn),
):
    """The filtered order list as a CSV that Excel opens with Vietnamese intact (UTF-8 BOM)."""
    require_admin(request)
    rate_limit(request, "admin-export", limit=20, window=60)
    where, args = _orders_where(tab, q, shipping, payment, due, status)
    rows = conn.execute(
        f"{_ORDER_LIST_SQL}{where} ORDER BY o.id DESC LIMIT ?", (*args, MAX_EXPORT_ROWS)
    ).fetchall()
    items = {}
    if rows:
        ids = [r["id"] for r in rows]
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for it in conn.execute(
                f"SELECT order_id, title, qty FROM order_items WHERE order_id IN ({', '.join('?' * len(chunk))}) ORDER BY id",
                chunk,
            ).fetchall():
                items.setdefault(it["order_id"], []).append(f"{it['title']} x{it['qty']}")

    def vn_time(ts):
        try:
            return (datetime.strptime(ts, "%Y-%m-%d %H:%M:%S") + timedelta(hours=7)).strftime("%d/%m/%Y %H:%M")
        except (TypeError, ValueError):
            return ts or ""

    out = io.StringIO()
    out.write("\ufeff")
    writer = csv.writer(out)
    writer.writerow(["Mã đơn", "Thời gian đặt", "Trạng thái", "Khách hàng", "Số điện thoại", "Địa chỉ",
                     "Tỉnh/thành", "Cách giao", "Thanh toán", "Mặt hàng", "Số bản", "Tạm tính",
                     "Phí vận chuyển", "Tổng", "Ghi chú", "Hạn gửi hàng"])
    for r in rows:
        writer.writerow([_csv_cell(v) for v in (
            r["order_no"], vn_time(r["created_at"]), ADMIN_ORDER_LABELS.get(r["status"], r["status"]),
            r["name"], r["phone"], r["address"], r["city"] or "",
            "Giao nhanh 24h" if r["shipping_method"] == "express" else "Tiêu chuẩn",
            PAYMENT_LABELS.get(r["payment_method"], r["payment_method"]),
            "; ".join(items.get(r["id"], [])), r["units"], r["subtotal"], r["shipping_fee"], r["total"],
            r["note"] or "", vn_time(r["deadline"]) if r["status"] in TO_SHIP else "",
        )])
    filename = f"don-hang-{vn_today().isoformat()}.csv"
    return Response(
        content=out.getvalue().encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


def admin_order_detail(conn, order_no):
    row = conn.execute(f"{_ORDER_LIST_SQL} WHERE o.order_no = ?", (order_no,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Không tìm thấy đơn hàng.")
    data = {**order_payload(conn, order_no), **admin_order_row(row)}
    ids = sorted({int(i["item_id"]) for i in data["items"] if str(i["item_id"]).isdigit()})
    metas = _load_meta(ids) if ids else {}
    for item in data["items"]:
        meta = metas.get(int(item["item_id"])) if str(item["item_id"]).isdigit() else None
        item["cover"] = (meta or {}).get("thumb") or ""
    data["address"] = row["address"]
    data["note"] = row["note"] or ""
    data["email"] = None
    if row["user_id"] is not None:
        user = database.get_user(conn, row["user_id"])
        data["email"] = user["email"] if user else None
    data["cases"] = [
        {"code": c["code"], "type_label": cases.TYPES[c["type"]]["label"], "status": c["status"],
         "status_label": cases.STATUSES.get(c["status"], c["status"]), "source": c["source"],
         "created_at": c["created_at"]}
        for c in conn.execute("SELECT * FROM cases WHERE order_id = ? ORDER BY id DESC", (row["id"],))
    ]
    parent = conn.execute("SELECT order_no FROM orders WHERE id = ?", (row["parent_order_id"],)).fetchone() \
        if row["parent_order_id"] else None
    data["parent_order_no"] = parent["order_no"] if parent else None
    data["reships"] = [r["order_no"] for r in conn.execute(
        "SELECT order_no FROM orders WHERE parent_order_id = ? ORDER BY id", (row["id"],))]
    return data


@app.get("/api/admin/orders/{order_no}")
def admin_get_order(order_no: str, request: Request, response: Response,
                    conn: sqlite3.Connection = Depends(get_conn)):
    require_admin(request)
    response.headers["Cache-Control"] = "no-store"
    return admin_order_detail(conn, check_order_no(order_no))


@app.put("/api/admin/orders/{order_no}")
def admin_set_order_status(order_no: str, request: Request, payload: dict = Body(default={}),
                           conn: sqlite3.Connection = Depends(get_conn)):
    """Body: {"status": "...", "restock": true|false}. Cancelling puts the copies back in stock;
    a return does too unless restock is false (copies came back broken / not ours)."""
    require_admin(request)
    order_no = check_order_no(order_no)
    restock = payload.get("restock", True)
    if type(restock) is not bool:
        bad_request("Giá trị restock không hợp lệ.")
    try:
        database.set_order_status(conn, order_no, payload.get("status"), restock=restock)
    except ValueError as exc:
        message = str(exc)
        if message == "Không tìm thấy đơn hàng.":
            return not_found(message)
        return bad_request(message, status_code=409)
    return admin_order_detail(conn, order_no)


@app.post("/api/admin/orders/bulk")
def admin_bulk_order_status(request: Request, payload: dict = Body(default={}),
                            conn: sqlite3.Connection = Depends(get_conn)):
    """Body: {"order_nos": [...], "status": "..."}. Each order is moved on its own;
    the ones that can't move are listed with the reason instead of failing the batch."""
    require_admin(request)
    order_nos = payload.get("order_nos")
    status = payload.get("status")
    if not isinstance(order_nos, list) or not order_nos or len(order_nos) > 100:
        bad_request("Chọn từ 1 đến 100 đơn hàng.")
    # Moves that touch stock or need judgement (cancel, return, lost) are done one order at a time.
    if status not in ("confirmed", "shipping", "completed", "failed"):
        bad_request("Thao tác này chỉ làm được trên từng đơn.")
    done, failed = [], []
    for raw in dict.fromkeys(str(x) for x in order_nos):
        no = raw.strip().upper()
        if not _ORDER_NO_RE.match(no):
            failed.append({"order_no": raw[:20], "error": "Mã đơn không hợp lệ."})
            continue
        try:
            database.set_order_status(conn, no, status)
            done.append(no)
        except ValueError as exc:
            failed.append({"order_no": no, "error": str(exc)})
    return {"updated": done, "failed": failed}


@app.get("/api/admin/inventory")
def admin_inventory(request: Request, response: Response, conn: sqlite3.Connection = Depends(get_conn)):
    """Kho vận: stock totals + the pick list (copies to take off the shelf for orders not yet sent)."""
    require_admin(request)
    response.headers["Cache-Control"] = "no-store"
    rows = conn.execute(
        f"""
        SELECT i.item_id, MAX(i.title) AS title, MAX(i.artist) AS artist,
               SUM(i.qty) AS qty, COUNT(DISTINCT o.id) AS orders,
               SUM(CASE WHEN o.shipping_method = 'express' THEN i.qty ELSE 0 END) AS express_qty,
               MIN({_DEADLINE_SQL}) AS first_deadline
        FROM order_items i JOIN orders o ON o.id = i.order_id
        WHERE {_TO_SHIP_SQL}
        GROUP BY i.item_id ORDER BY first_deadline ASC
        LIMIT 200
        """
    ).fetchall()
    ids = [int(r["item_id"]) for r in rows if str(r["item_id"]).isdigit()]
    metas = _load_meta(ids) if ids else {}
    shelf = {r["discogs_release_id"]: r["stock"] for r in database.list_prices(conn)}
    return {
        "summary": {**inventory_summary(conn), **fulfilment_summary(conn)},
        "picklist": [
            {
                "id": r["item_id"], "title": r["title"], "artist": r["artist"] or "",
                "qty": r["qty"], "orders": r["orders"], "express_qty": r["express_qty"],
                "first_deadline": r["first_deadline"],
                "stock": shelf.get(int(r["item_id"])) if str(r["item_id"]).isdigit() else None,
                "cover": ((metas.get(int(r["item_id"])) or {}).get("thumb") or "")
                if str(r["item_id"]).isdigit() else "",
            }
            for r in rows
        ],
    }


@app.put("/api/admin/orders/{order_no}/shipment")
def admin_set_shipment(order_no: str, request: Request, payload: dict = Body(default={}),
                       conn: sqlite3.Connection = Depends(get_conn)):
    """Body: {"carrier": "...", "tracking_no": "..."} -- ĐVVC and mã vận đơn, needed for every claim."""
    require_admin(request)
    order_no = check_order_no(order_no)
    carrier = text_field(payload, "carrier", 60, label="đơn vị vận chuyển")
    tracking = text_field(payload, "tracking_no", 60, label="mã vận đơn")
    cur = conn.execute(
        "UPDATE orders SET carrier = ?, tracking_no = ?, updated_at = COALESCE(updated_at, CURRENT_TIMESTAMP) "
        "WHERE order_no = ?", (carrier or None, tracking or None, order_no))
    conn.commit()
    if cur.rowcount == 0:
        return not_found("Không tìm thấy đơn hàng.")
    return admin_order_detail(conn, order_no)


# ------------------------------------------------------------------------
# After-sales cases ("Sự cố")
# ------------------------------------------------------------------------

_CASE_CODE_RE = re.compile(r"^SC-[A-Z0-9]{6}$")
CASE_TABS = {
    "active": cases.OPEN_STATUSES,
    "open": ("open",),
    "waiting_carrier": ("waiting_carrier",),
    "waiting_customer": ("waiting_customer",),
    "resolved": ("resolved",),
    "rejected": ("rejected",),
    "all": None,
}
_CASE_OPEN_SQL = "c.status IN ('open', 'waiting_carrier', 'waiting_customer')"
_CASE_OVERDUE_SQL = f"({_CASE_OPEN_SQL} AND c.due_at < CURRENT_TIMESTAMP)"
# A customer's request nobody at the shop has touched yet (automatic replies don't count).
_CASE_UNTOUCHED_SQL = (
    f"(c.source = 'customer' AND {_CASE_OPEN_SQL} AND NOT EXISTS (SELECT 1 FROM case_events e "
    "WHERE e.case_id = c.id AND e.actor = 'shop' AND e.kind != 'auto'))"
)


def check_case_code(value):
    text = str(value or "").strip().upper()
    if not _CASE_CODE_RE.match(text):
        raise HTTPException(status_code=404, detail="Không tìm thấy sự cố.")
    return text


def cases_summary(conn):
    row = conn.execute(
        f"""
        SELECT
            SUM(CASE WHEN {_CASE_OPEN_SQL} THEN 1 ELSE 0 END) AS active,
            SUM(CASE WHEN c.status = 'open' THEN 1 ELSE 0 END) AS open,
            SUM(CASE WHEN c.status = 'waiting_carrier' THEN 1 ELSE 0 END) AS waiting_carrier,
            SUM(CASE WHEN c.status = 'waiting_customer' THEN 1 ELSE 0 END) AS waiting_customer,
            SUM(CASE WHEN {_CASE_OVERDUE_SQL} THEN 1 ELSE 0 END) AS overdue,
            SUM(CASE WHEN {_CASE_UNTOUCHED_SQL} THEN 1 ELSE 0 END) AS untouched,
            SUM(CASE WHEN {_CASE_OPEN_SQL} AND c.claimed > c.compensated THEN c.claimed - c.compensated ELSE 0 END) AS awaiting_money,
            SUM(c.refunded) AS refunded,
            SUM(c.compensated) AS compensated
        FROM cases c
        """
    ).fetchone()
    return {k: row[k] or 0 for k in row.keys()}


def _case_row(r):
    t = cases.TYPES[r["type"]]
    return {
        "code": r["code"], "type": r["type"], "type_label": t["label"], "group": t["group"],
        "source": r["source"], "status": r["status"], "status_label": cases.STATUSES.get(r["status"], r["status"]),
        "order_no": r["order_no"], "customer": r["name"] or "", "phone": r["phone"] or "",
        "order_status": r["order_status"],
        "order_status_label": ADMIN_ORDER_LABELS.get(r["order_status"], r["order_status"]) if r["order_status"] else None,
        "refunded": r["refunded"], "claimed": r["claimed"], "compensated": r["compensated"],
        "carrier_ref": r["carrier_ref"] or "",
        "due_at": r["due_at"], "overdue": bool(r["overdue"]), "untouched": bool(r["untouched"]),
        "created_at": r["created_at"], "updated_at": r["updated_at"], "closed_at": r["closed_at"],
    }


_CASE_LIST_SQL = f"""
    SELECT c.*, o.order_no, o.name, o.phone, o.status AS order_status,
           {_CASE_OVERDUE_SQL} AS overdue, {_CASE_UNTOUCHED_SQL} AS untouched
    FROM cases c LEFT JOIN orders o ON o.id = c.order_id
"""


@app.get("/api/admin/cases/meta")
def admin_cases_meta(request: Request):
    require_admin(request)
    return cases.meta()


@app.get("/api/admin/cases")
def admin_list_cases(
    request: Request,
    response: Response,
    tab: str = "active",
    group: str = "",
    type: str = "",
    source: str = "",
    flag: str = "",
    q: str = "",
    page: int = 1,
    per_page: int = 20,
    conn: sqlite3.Connection = Depends(get_conn),
):
    require_admin(request)
    response.headers["Cache-Control"] = "no-store"
    if tab not in CASE_TABS:
        bad_request("Nhóm sự cố không hợp lệ.")
    where, args = [], []
    if CASE_TABS[tab]:
        where.append(f"c.status IN ({', '.join('?' * len(CASE_TABS[tab]))})")
        args.extend(CASE_TABS[tab])
    if group:
        keys = [k for k, t in cases.TYPES.items() if t["group"] == group]
        if not keys:
            bad_request("Nhóm tình huống không hợp lệ.")
        where.append(f"c.type IN ({', '.join('?' * len(keys))})")
        args.extend(keys)
    if type:
        if type not in cases.TYPES:
            bad_request("Loại sự cố không hợp lệ.")
        where.append("c.type = ?")
        args.append(type)
    if source:
        if source not in ("customer", "shop"):
            bad_request("Nguồn không hợp lệ.")
        where.append("c.source = ?")
        args.append(source)
    if flag == "overdue":
        where.append(_CASE_OVERDUE_SQL)
    elif flag == "untouched":
        where.append(_CASE_UNTOUCHED_SQL)
    elif flag == "money":
        where.append(f"({_CASE_OPEN_SQL} AND c.claimed > c.compensated)")
    elif flag:
        bad_request("Bộ lọc không hợp lệ.")
    q = (q or "").strip()[:100]
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        parts = ["c.code LIKE ? ESCAPE '\\'", "o.order_no LIKE ? ESCAPE '\\'", "o.name LIKE ? ESCAPE '\\'",
                 "c.carrier_ref LIKE ? ESCAPE '\\'", "o.tracking_no LIKE ? ESCAPE '\\'"]
        args.extend([like.upper(), like.upper(), like, like, like])
        digits = re.sub(r"\D", "", q)
        if len(digits) >= 3:
            parts.append("o.phone LIKE ?")
            args.append("%" + digits + "%")
        where.append("(" + " OR ".join(parts) + ")")
    sql_where = (" WHERE " + " AND ".join(where)) if where else ""
    per_page = max(1, min(100, per_page))
    total = conn.execute(f"SELECT COUNT(*) FROM cases c LEFT JOIN orders o ON o.id = c.order_id{sql_where}", args).fetchone()[0]
    pages = max(1, -(-total // per_page))
    page = max(1, min(page, pages))
    # Untouched customer requests first, then overdue, then oldest due date.
    rows = conn.execute(
        f"{_CASE_LIST_SQL}{sql_where} ORDER BY untouched DESC, overdue DESC, "
        f"CASE WHEN {_CASE_OPEN_SQL} THEN c.due_at END ASC, c.id DESC LIMIT ? OFFSET ?",
        (*args, per_page, (page - 1) * per_page),
    ).fetchall()
    counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM cases GROUP BY status")}
    tab_counts = {k: (sum(counts.values()) if v is None else sum(counts.get(s, 0) for s in v)) for k, v in CASE_TABS.items()}
    return {
        "cases": [_case_row(r) for r in rows],
        "pagination": {"page": page, "pages": pages, "per_page": per_page, "items": total},
        "counts": tab_counts,
        "summary": cases_summary(conn),
    }


def admin_case_detail(conn, code):
    r = conn.execute(f"{_CASE_LIST_SQL} WHERE c.code = ?", (code,)).fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="Không tìm thấy sự cố.")
    data = _case_row(r)
    t = cases.TYPES[r["type"]]
    data.update({
        "detail": r["detail"] or "",
        "reason": r["reason"],
        # Both reason lists have an "other" key: pick the list by case type.
        "reason_label": (dict(cases.CANCEL_REASONS) if r["type"] == "cancel_request"
                         else {k: label for k, label, _t, _s in cases.REPORT_REASONS}).get(r["reason"]),
        "steps": t["steps"],
        "checklist": json.loads(r["checklist"] or "[]"),
        "actions": [{"key": a, "label": cases.ACTIONS[a]} for a in t["actions"]],
        "events": [
            {"at": e["created_at"], "actor": e["actor"], "kind": e["kind"], "text": e["text"], "public": bool(e["public"])}
            for e in conn.execute("SELECT * FROM case_events WHERE case_id = ? ORDER BY id DESC", (r["id"],))
        ],
        "order": admin_order_detail(conn, r["order_no"]) if r["order_no"] else None,
    })
    return data


@app.get("/api/admin/cases/{code}")
def admin_get_case(code: str, request: Request, response: Response, conn: sqlite3.Connection = Depends(get_conn)):
    require_admin(request)
    response.headers["Cache-Control"] = "no-store"
    return admin_case_detail(conn, check_case_code(code))


@app.post("/api/admin/cases", status_code=201)
def admin_create_case(request: Request, payload: dict = Body(default={}), conn: sqlite3.Connection = Depends(get_conn)):
    """Body: {"type": "...", "order_no": "BUI-..." (optional for money cases), "detail": "..."}"""
    require_admin(request)
    raw = payload.get("order_no")
    order_no = None
    if raw not in (None, ""):
        order_no = str(raw).strip().upper()
        if not _ORDER_NO_RE.match(order_no):
            bad_request("Mã đơn hàng không hợp lệ (dạng BUI-XXXXXXXX).")
    try:
        code = cases.shop_create(conn, order_no, payload.get("type"), payload.get("detail"))
    except ValueError as exc:
        return bad_request(str(exc))
    return admin_case_detail(conn, code)


@app.post("/api/admin/cases/{code}/actions")
def admin_case_action(code: str, request: Request, payload: dict = Body(default={}),
                      conn: sqlite3.Connection = Depends(get_conn)):
    """Body: {"action": "...", ...params}. See cases.apply_action for each action's params."""
    require_admin(request)
    code = check_case_code(code)
    try:
        message = cases.apply_action(conn, code, payload.get("action"), payload, database.new_order_no)
    except LookupError as exc:
        return not_found(str(exc))
    except ValueError as exc:
        return bad_request(str(exc), status_code=409)
    data = admin_case_detail(conn, code)
    data["message"] = message
    return data


@app.put("/api/admin/cases/{code}/checklist")
def admin_case_checklist(code: str, request: Request, payload: dict = Body(default={}),
                         conn: sqlite3.Connection = Depends(get_conn)):
    require_admin(request)
    code = check_case_code(code)
    try:
        cases.set_checklist(conn, code, payload.get("done"))
    except LookupError as exc:
        return not_found(str(exc))
    except ValueError as exc:
        return bad_request(str(exc))
    return {"ok": True}


@app.get("/admin")
def admin_page():
    path = os.path.join(FRONTEND_DIR, "admin.html")
    if not os.path.exists(path):
        return PlainTextResponse("admin.html not found next to index.html.", status_code=404)
    return FileResponse(path)


# ------------------------------------------------------------------------
# Static frontend -- mounted last so it only catches what /api/* didn't
# ------------------------------------------------------------------------

if os.path.isdir(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=5000, reload=os.environ.get("DEV_RELOAD") == "1")
