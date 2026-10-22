"""
SQLite persistence for Bui Records.

Database scope is intentionally limited to:
- preorders
- orders + order_items
- release_prices (shop price per Discogs release, in VND)
- users (customer accounts; orders.user_id links an order to its owner)

Catalogue/product metadata is NOT stored locally. It is read from Discogs.
Cart state and the logged-in user id are kept in the signed session cookie.
"""

import csv
import os
import re
import sqlite3
import sys
from contextlib import closing, contextmanager

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "bui_records.db")


def get_db():
    """
    Open a SQLite connection with dictionary-like rows.

    check_same_thread=False: FastAPI runs sync dependencies (like the one
    that yields this connection) in a worker thread pool, and the "enter",
    "handle the request", and "exit" steps of a single request can each
    land on a different worker thread. Nothing here actually touches the
    connection from two threads *at once* -- it's the same sequential
    open -> use -> close per request -- but sqlite3's default same-thread
    check doesn't know that and refuses regardless. This documents why
    it's safe to disable for this access pattern.
    """
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db_session():
    """
    `with db_session() as conn:` -- commit on success, rollback on error, and
    always close. (A bare `with sqlite3.connect(...) as conn:` only commits or
    rolls back; it never closes the connection, so every use leaked one.)
    """
    with closing(get_db()) as conn:
        with conn:
            yield conn


def _table_columns(conn, table_name):
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row["name"] for row in rows}


def _ensure_column(conn, table_name, column_name, definition):
    if column_name not in _table_columns(conn, table_name):
        conn.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"
        )


def init_db():
    """
    Create/migrate only the tables needed for commerce.

    Legacy catalogue tables from the old SQLite design are dropped because
    catalogue data now comes from Discogs. Existing order/preorder data is kept.
    """
    with db_session() as conn:
        # One-time cleanup of the old local-catalogue tables (guarded so a future table
        # with one of these names is never wiped on every start).
        if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
            for table in (
                "tracks",
                "records",
                "gear",
                "reviews",
                "newsletter_subscribers",
                "cart_items",
                "carts",
            ):
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.execute("PRAGMA user_version = 1")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS preorders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                artist TEXT NOT NULL,
                ships_label TEXT NOT NULL,
                held INTEGER NOT NULL DEFAULT 0,
                total_slots INTEGER NOT NULL DEFAULT 0,
                price INTEGER,
                reserved_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        _ensure_column(conn, "preorders", "reserved_count", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "preorders", "created_at", "TEXT")
        _ensure_column(conn, "preorders", "discogs_release_id", "INTEGER")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS preorder_reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                preorder_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (preorder_id) REFERENCES preorders(id) ON DELETE CASCADE
            )
            """
        )

        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_reservation_active "
            "ON preorder_reservations(preorder_id, phone) WHERE status != 'cancelled'"
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_no TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                address TEXT NOT NULL,
                city TEXT,
                note TEXT,
                shipping_method TEXT NOT NULL,
                shipping_fee INTEGER NOT NULL DEFAULT 0,
                payment_method TEXT NOT NULL,
                subtotal INTEGER NOT NULL DEFAULT 0,
                total INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                item_id TEXT NOT NULL,
                item_type TEXT NOT NULL DEFAULT 'record',
                title TEXT NOT NULL,
                artist TEXT,
                unit_price INTEGER NOT NULL,
                qty INTEGER NOT NULL,
                FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
            )
            """
        )
        _ensure_column(conn, "order_items", "artist", "TEXT")
        _ensure_column(conn, "order_items", "item_type", "TEXT NOT NULL DEFAULT 'record'")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                phone TEXT,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # NULL for guest orders. Added with ALTER so existing databases keep their orders.
        _ensure_column(conn, "orders", "user_id", "INTEGER")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_orders_user ON orders(user_id)")
        # One order per cart: a double-clicked "Đặt hàng" sends the same cart twice.
        _ensure_column(conn, "orders", "cart_key", "TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_cart_key "
            "ON orders(cart_key) WHERE cart_key IS NOT NULL"
        )
        # When the shop last changed the order's status (NULL = never touched since checkout).
        _ensure_column(conn, "orders", "updated_at", "TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_orders_status ON orders(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_orders_created ON orders(created_at)")

        # Shipping + after-sales bookkeeping on each order.
        _ensure_column(conn, "orders", "carrier", "TEXT")            # ĐVVC, e.g. "J&T Express"
        _ensure_column(conn, "orders", "tracking_no", "TEXT")        # mã vận đơn
        _ensure_column(conn, "orders", "refunded", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "orders", "parent_order_id", "INTEGER")  # set on a free re-shipment

        # After-sales cases ("sự cố"): customer cancel / return requests and shipping incidents.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                order_id INTEGER REFERENCES orders(id),
                type TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'shop',
                reason TEXT,
                detail TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                checklist TEXT NOT NULL DEFAULT '[]',
                refunded INTEGER NOT NULL DEFAULT 0,
                claimed INTEGER NOT NULL DEFAULT 0,
                compensated INTEGER NOT NULL DEFAULT 0,
                carrier_ref TEXT,
                due_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                closed_at TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS ix_cases_status ON cases(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_cases_order ON cases(order_id)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS case_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                actor TEXT NOT NULL,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                public INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS ix_case_events_case ON case_events(case_id)")

        # One row per browser per day (Vietnam date) -- the "Khách truy cập" metric.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS site_visits (
                day TEXT NOT NULL,
                visitor TEXT NOT NULL,
                PRIMARY KEY (day, visitor)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS release_prices (
                discogs_release_id INTEGER PRIMARY KEY,
                price INTEGER,
                currency TEXT NOT NULL DEFAULT 'VND',
                stock_status TEXT NOT NULL DEFAULT 'contact',
                stock INTEGER,
                source_url TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        conn.commit()


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------

STOCK_STATUSES = ("in_stock", "preorder", "contact", "out_of_stock")

# Labels used on vocrecords.vn -> our stock_status values.
_STATUS_ALIASES = {
    "con hang": "in_stock",
    "còn hàng": "in_stock",
    "in stock": "in_stock",
    "preorder": "preorder",
    "pre-order": "preorder",
    "lien he": "contact",
    "liên hệ": "contact",
    "het hang": "out_of_stock",
    "hết hàng": "out_of_stock",
    "out of stock": "out_of_stock",
}


def parse_price(value):
    """
    Turn '1,010,000 ₫', '1.010.000đ', 1010000 into the integer 1010000.
    Returns None for empty values or 'Liên hệ' (contact for price).
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    digits = re.sub(r"\D", "", str(value))
    return int(digits) if digits else None


def normalize_status(value, price=None):
    key = (value or "").strip().lower()
    status = _STATUS_ALIASES.get(key, key)
    if status in STOCK_STATUSES:
        return status
    return "in_stock" if price else "contact"


def set_price(conn, release_id, price, *, stock_status=None, stock=None,
              source_url=None, currency="VND"):
    """Insert or update the shop price for one Discogs release."""
    price = parse_price(price)
    status = normalize_status(stock_status, price)
    if stock is not None and int(stock) <= 0:
        status = "out_of_stock"  # a counted stock of 0 is sold out, whatever the label says
    conn.execute(
        """
        INSERT INTO release_prices
            (discogs_release_id, price, currency, stock_status, stock, source_url, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(discogs_release_id) DO UPDATE SET
            price = excluded.price,
            currency = excluded.currency,
            stock_status = excluded.stock_status,
            stock = excluded.stock,
            source_url = excluded.source_url,
            updated_at = CURRENT_TIMESTAMP
        """,
        (int(release_id), price, currency, status, stock, source_url),
    )


def set_stock(conn, release_id, stock):
    """
    Set how many copies are on hand (None = don't track a number).
    0 marks the disc sold out; restocking a sold-out disc puts it back on sale.
    Returns the updated row. Raises ValueError (Vietnamese message) on bad input.
    """
    row = get_price(conn, release_id)
    if not row:
        raise ValueError(f"Đĩa {release_id} chưa có trong cửa hàng (dùng set-price trước).")
    if stock is not None:
        stock = int(stock)
        if stock < 0:
            raise ValueError("Số lượng tồn kho không được âm.")
    status = row["stock_status"]
    if stock is not None and stock == 0:
        status = "out_of_stock"
    elif status == "out_of_stock" and (stock is None or stock > 0):
        status = "in_stock" if row["price"] is not None else "contact"
    conn.execute(
        "UPDATE release_prices SET stock = ?, stock_status = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE discogs_release_id = ?",
        (stock, status, int(release_id)),
    )
    conn.commit()
    return get_price(conn, release_id)


# ---------------------------------------------------------------------------
# Product + stock management (used by the admin page)
# ---------------------------------------------------------------------------

_UNSET = object()


def list_prices(conn):
    """Every product of the shop, most recently changed first."""
    rows = conn.execute(
        "SELECT * FROM release_prices ORDER BY updated_at DESC, discogs_release_id"
    ).fetchall()
    return [dict(r) for r in rows]


def _status_after(status, new_price, new_stock, stock_given):
    """Keep stock_status consistent with the price / stock a product ends up with."""
    if new_stock is not None and new_stock <= 0:
        return "out_of_stock"
    if status == "out_of_stock":
        # Only an explicit stock change brings a sold-out disc back on sale.
        if stock_given:
            return "in_stock" if new_price is not None else "contact"
        return status
    if status == "contact" and new_price is not None:
        return "in_stock"
    if status == "in_stock" and new_price is None:
        return "contact"
    return status


def add_product(conn, release_id, price, stock):
    """
    Put a Discogs release on sale. stock=None means "don't count copies".
    Raises ValueError (Vietnamese message) if the release is already in the shop.
    """
    status = "out_of_stock" if stock is not None and stock <= 0 else "in_stock"
    try:
        conn.execute(
            "INSERT INTO release_prices "
            "(discogs_release_id, price, currency, stock_status, stock, updated_at) "
            "VALUES (?, ?, 'VND', ?, ?, CURRENT_TIMESTAMP)",
            (int(release_id), price, status, stock),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ValueError("Đĩa này đã có trong cửa hàng.")
    return get_price(conn, release_id)


def update_product(conn, release_id, *, price=_UNSET, stock=_UNSET):
    """
    Set the price and/or the stock count (absolute values; None clears them:
    no price = "Liên hệ", no stock = copies are not counted).
    Raises ValueError (Vietnamese message) if the product does not exist.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = get_price(conn, release_id)
        if not row:
            raise ValueError("Đĩa này không có trong cửa hàng.")
        new_price = row["price"] if price is _UNSET else price
        new_stock = row["stock"] if stock is _UNSET else stock
        status = _status_after(row["stock_status"], new_price, new_stock, stock is not _UNSET)
        conn.execute(
            "UPDATE release_prices SET price = ?, stock = ?, stock_status = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE discogs_release_id = ?",
            (new_price, new_stock, status, int(release_id)),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_price(conn, release_id)


def add_stock(conn, release_id, delta):
    """
    Receive (delta > 0) or write off (delta < 0) copies. One atomic UPDATE, so it
    stays correct even while customers are checking out. A sold-out disc that
    receives copies goes back on sale; reaching 0 marks it sold out.
    Raises ValueError (Vietnamese message) for an unknown product, a product whose
    copies are not counted, or a write-off larger than the stock.
    """
    cur = conn.execute(
        """
        UPDATE release_prices SET
            stock = stock + :d,
            stock_status = CASE
                WHEN stock + :d <= 0 THEN 'out_of_stock'
                WHEN stock_status = 'out_of_stock' THEN
                    CASE WHEN price IS NULL THEN 'contact' ELSE 'in_stock' END
                ELSE stock_status END,
            updated_at = CURRENT_TIMESTAMP
        WHERE discogs_release_id = :id AND stock IS NOT NULL AND stock + :d >= 0
        """,
        {"d": int(delta), "id": int(release_id)},
    )
    conn.commit()
    if cur.rowcount == 0:
        row = get_price(conn, release_id)
        if not row:
            raise ValueError("Đĩa này không có trong cửa hàng.")
        if row["stock"] is None:
            raise ValueError("Đĩa này chưa đếm số lượng. Hãy nhập số lượng hiện có ở ô Tồn kho rồi lưu.")
        raise ValueError(f"Không thể giảm quá số đang có ({row['stock']} bản).")
    return get_price(conn, release_id)


def delete_product(conn, release_id):
    """Take a product off sale. Past orders keep their own copy of title/price."""
    cur = conn.execute("DELETE FROM release_prices WHERE discogs_release_id = ?", (int(release_id),))
    conn.commit()
    if cur.rowcount == 0:
        raise ValueError("Đĩa này không có trong cửa hàng.")


def get_price(conn, release_id):
    """Return the price row for one release as a dict, or None."""
    row = conn.execute(
        "SELECT * FROM release_prices WHERE discogs_release_id = ?",
        (int(release_id),),
    ).fetchone()
    return dict(row) if row else None


def import_prices_csv(path):
    """
    Bulk-load prices from a CSV with the columns:

        discogs_release_id,price,stock_status,stock,source_url

    Only discogs_release_id is required. price may be '1,010,000 ₫' or blank
    (contact for price). stock_status accepts Vọc labels (CÒN HÀNG, PREORDER,
    LIÊN HỆ, HẾT HÀNG) or in_stock/preorder/contact/out_of_stock.
    Returns (imported, skipped).
    """
    imported = skipped = 0
    with db_session() as conn, open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                release_id = int(str(row.get("discogs_release_id", "")).strip())
            except ValueError:
                skipped += 1
                continue
            stock = (row.get("stock") or "").strip()
            set_price(
                conn,
                release_id,
                row.get("price"),
                stock_status=row.get("stock_status"),
                stock=int(stock) if stock.isdigit() else None,
                source_url=(row.get("source_url") or "").strip() or None,
            )
            imported += 1
        conn.commit()
    return imported, skipped


# ---------------------------------------------------------------------------
# Customer accounts
# ---------------------------------------------------------------------------

def create_user(conn, email, name, phone, password_hash):
    """Insert an account and return it. Raises ValueError (Vietnamese message) if the email is taken."""
    try:
        cur = conn.execute(
            "INSERT INTO users (email, name, phone, password_hash) VALUES (?, ?, ?, ?)",
            (email, name, phone, password_hash),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ValueError("Email này đã được đăng ký. Hãy đăng nhập.")
    return get_user(conn, cur.lastrowid)


def get_user(conn, user_id):
    row = conn.execute("SELECT * FROM users WHERE id = ?", (int(user_id),)).fetchone()
    return dict(row) if row else None


def get_user_by_email(conn, email):
    row = conn.execute(
        "SELECT * FROM users WHERE email = ?", (str(email or "").strip().lower(),)
    ).fetchone()
    return dict(row) if row else None


def update_user(conn, user_id, name, phone):
    conn.execute("UPDATE users SET name = ?, phone = ? WHERE id = ?", (name, phone, int(user_id)))
    conn.commit()
    return get_user(conn, user_id)


def set_user_password(conn, user_id, password_hash):
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, int(user_id)))
    conn.commit()


def last_shipping(conn, user_id):
    """Address of the user's most recent order (to pre-fill checkout), or None."""
    row = conn.execute(
        "SELECT address, city FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT 1",
        (int(user_id),),
    ).fetchone()
    return {"address": row["address"], "city": row["city"] or ""} if row else None


# ---------------------------------------------------------------------------
# Visits + order fulfilment (used by the admin dashboard)
# ---------------------------------------------------------------------------

# SQLite CURRENT_TIMESTAMP is UTC; the shop counts days in Vietnam time.
VN_OFFSET = "+7 hours"


def record_visit(conn, visitor):
    """Count one visit per browser per Vietnam day (repeat loads the same day are ignored)."""
    conn.execute(
        "INSERT OR IGNORE INTO site_visits (day, visitor) VALUES (date('now', ?), ?)",
        (VN_OFFSET, str(visitor)[:64]),
    )
    conn.commit()


# Order life cycle. Every status a shop can move an order to, from each status.
#   pending   -> customer placed it, shop has not looked yet
#   confirmed -> shop accepted it, still has to pack and hand it to the carrier
#   shipping  -> handed to the carrier
#   completed -> delivered
#   failed    -> carrier could not deliver, parcel is on its way back (or will be re-sent)
#   lost      -> the parcel went missing (outbound or on its way back); no copies come back
#   returned  -> the parcel is back in the shop
#   cancelled -> never shipped
ORDER_STATUSES = ("pending", "confirmed", "shipping", "completed", "failed", "lost", "returned", "cancelled")
ORDER_TRANSITIONS = {
    "pending": ("confirmed", "cancelled"),
    "confirmed": ("shipping", "cancelled"),
    # returned straight from shipping: the parcel came back before the carrier marked it failed
    "shipping": ("completed", "failed", "lost", "returned"),
    # completed from failed/lost: the customer did get it after all (system was behind)
    "failed": ("shipping", "returned", "completed", "lost"),
    "lost": ("returned", "completed"),
    "completed": ("returned", "lost"),
    "returned": (),
    "cancelled": (),
}
# Cancelling always puts the copies back (they never left). A return does by default,
# but the shop can say the copies came back broken / empty box / not ours (restock=False).
RESTOCK_STATUSES = ("cancelled", "returned")


def _restock_order(conn, order_id):
    items = conn.execute(
        "SELECT item_id, qty FROM order_items WHERE order_id = ?", (order_id,)
    ).fetchall()
    for item in items:
        item_id = str(item["item_id"])
        if not item_id.isdigit():
            continue
        # Only products that count copies; a sold-out disc that gets copies back is on sale again.
        conn.execute(
            """
            UPDATE release_prices SET
                stock = stock + ?,
                stock_status = CASE WHEN stock_status = 'out_of_stock' AND stock + ? > 0
                    THEN CASE WHEN price IS NULL THEN 'contact' ELSE 'in_stock' END
                    ELSE stock_status END,
                updated_at = CURRENT_TIMESTAMP
            WHERE discogs_release_id = ? AND stock IS NOT NULL
            """,
            (item["qty"], item["qty"], int(item_id)),
        )


def move_order(conn, order, status, restock=True):
    """
    Change one order's status inside the caller's transaction (no BEGIN/commit here).
    `order` is the orders row. Returns True if it changed, False if it already had `status`.
    """
    if status not in ORDER_STATUSES:
        raise ValueError("Trạng thái đơn hàng không hợp lệ.")
    current = order["status"]
    if current == status:
        return False
    if status not in ORDER_TRANSITIONS.get(current, ()):
        raise ValueError(
            f"Không thể chuyển đơn {order['order_no']} từ trạng thái hiện tại sang trạng thái đã chọn."
        )
    conn.execute(
        "UPDATE orders SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (status, order["id"]),
    )
    if status in RESTOCK_STATUSES and (restock or status == "cancelled"):
        _restock_order(conn, order["id"])
    return True


def set_order_status(conn, order_no, status, restock=True):
    """
    Move one order to `status` if the life cycle allows it, in one transaction.
    Cancelling puts the copies back in stock; a return does too unless restock=False.
    Returns the order row (dict). Raises ValueError with a Vietnamese message.
    """
    if status not in ORDER_STATUSES:
        raise ValueError("Trạng thái đơn hàng không hợp lệ.")
    conn.execute("BEGIN IMMEDIATE")
    try:
        order = conn.execute("SELECT * FROM orders WHERE order_no = ?", (order_no,)).fetchone()
        if not order:
            raise ValueError("Không tìm thấy đơn hàng.")
        move_order(conn, order, status, restock)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return dict(conn.execute("SELECT * FROM orders WHERE id = ?", (order["id"],)).fetchone())


_ORDER_NO_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_order_no(conn):
    import secrets
    for _ in range(20):
        candidate = "BUI-" + "".join(secrets.choice(_ORDER_NO_ALPHABET) for _ in range(8))
        if not conn.execute("SELECT 1 FROM orders WHERE order_no = ?", (candidate,)).fetchone():
            return candidate
    return f"BUI-{secrets.token_hex(6).upper()}"


def take_stock(conn, item_id, qty, title):
    """Decrement a tracked stock count inside the caller's transaction (untracked = no-op)."""
    if not str(item_id).isdigit():
        return
    rid = int(item_id)
    res = conn.execute(
        "UPDATE release_prices SET stock = stock - ?, "
        "stock_status = CASE WHEN stock - ? <= 0 THEN 'out_of_stock' ELSE stock_status END, "
        "updated_at = CURRENT_TIMESTAMP "
        "WHERE discogs_release_id = ? AND stock IS NOT NULL AND stock >= ?",
        (qty, qty, rid, qty),
    )
    tracked = conn.execute(
        "SELECT stock FROM release_prices WHERE discogs_release_id = ? AND stock IS NOT NULL", (rid,)
    ).fetchone()
    if tracked and res.rowcount == 0:
        raise ValueError(f"{title}: kho chỉ còn {tracked['stock']} bản, không đủ để gửi.")


# ---------------------------------------------------------------------------
# Preorder reservations ("giu cho")
# ---------------------------------------------------------------------------

# pending   -> customer reserved, shop has not confirmed yet
# confirmed -> shop confirmed the slot
# arrived   -> the record is in the shop, customer can pick up / pay
# cancelled -> reservation cancelled (the slot is released)
RESERVATION_STATUSES = ("pending", "confirmed", "arrived", "cancelled")
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def normalize_phone(value):
    """
    Digits only, +84/84 prefix rewritten to 0. Returns None unless the result is a
    Vietnamese number: leading 0 and 10-11 digits (e.g. 0912345678).
    """
    if isinstance(value, bool):
        return None
    digits = re.sub(r"\D", "", str(value or ""))
    if digits.startswith("84") and len(digits) in (11, 12):
        digits = "0" + digits[2:]
    return digits if digits.startswith("0") and len(digits) in (10, 11) else None


def _new_code(conn):
    import secrets
    while True:
        code = "GC-" + "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))
        if not conn.execute(
            "SELECT 1 FROM preorder_reservations WHERE code = ?", (code,)
        ).fetchone():
            return code


def create_reservation(conn, preorder_id, name, phone):
    """
    Reserve one slot. Returns the reservation dict, or raises ValueError with
    a Vietnamese message. Slot check + insert + counter run in one transaction.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM preorders WHERE id = ?", (preorder_id,)).fetchone()
        if not row:
            raise ValueError("Không tìm thấy đợt đặt trước.")
        if row["held"] + row["reserved_count"] >= row["total_slots"]:
            raise ValueError("Bản ép này đã hết suất đặt trước.")
        dup = conn.execute(
            "SELECT 1 FROM preorder_reservations WHERE preorder_id = ? AND phone = ? "
            "AND status != 'cancelled'",
            (preorder_id, phone),
        ).fetchone()
        if dup:
            raise ValueError("Số điện thoại này đã giữ chỗ bản ép này rồi.")
        code = _new_code(conn)
        conn.execute(
            "INSERT INTO preorder_reservations (code, preorder_id, name, phone) VALUES (?, ?, ?, ?)",
            (code, preorder_id, name, phone),
        )
        conn.execute(
            "UPDATE preorders SET reserved_count = reserved_count + 1 WHERE id = ?",
            (preorder_id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_reservation(conn, code)


def get_reservation(conn, code):
    row = conn.execute(
        """
        SELECT r.*, p.title AS title, p.artist AS artist, p.ships_label AS ships_label,
               p.price AS price
        FROM preorder_reservations r JOIN preorders p ON p.id = r.preorder_id
        WHERE r.code = ?
        """,
        (str(code or "").strip().upper(),),
    ).fetchone()
    return dict(row) if row else None


def set_reservation_status(conn, code, status):
    """Change a reservation's status; cancelling releases the slot (and un-cancelling retakes it)."""
    if status not in RESERVATION_STATUSES:
        raise ValueError("Trạng thái không hợp lệ.")
    conn.execute("BEGIN IMMEDIATE")
    try:
        res = get_reservation(conn, code)  # read inside the write lock: no lost updates
        if not res:
            raise ValueError("Không tìm thấy mã giữ chỗ.")
        if res["status"] == status:
            conn.rollback()
            return res
        if status == "cancelled":
            conn.execute(
                "UPDATE preorders SET reserved_count = MAX(0, reserved_count - 1) WHERE id = ?",
                (res["preorder_id"],),
            )
        elif res["status"] == "cancelled":
            p = conn.execute("SELECT * FROM preorders WHERE id = ?", (res["preorder_id"],)).fetchone()
            if p["held"] + p["reserved_count"] >= p["total_slots"]:
                raise ValueError("Bản ép này đã hết suất, không thể khôi phục.")
            dup = conn.execute(
                "SELECT 1 FROM preorder_reservations WHERE preorder_id = ? AND phone = ? "
                "AND status != 'cancelled' AND code != ?",
                (res["preorder_id"], res["phone"], res["code"]),
            ).fetchone()
            if dup:
                raise ValueError("Số điện thoại này đã có giữ chỗ khác cho bản ép này.")
            conn.execute(
                "UPDATE preorders SET reserved_count = reserved_count + 1 WHERE id = ?",
                (res["preorder_id"],),
            )
        conn.execute(
            "UPDATE preorder_reservations SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE code = ?",
            (status, res["code"]),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_reservation(conn, res["code"])


if __name__ == "__main__":
    init_db()
    if len(sys.argv) == 3 and sys.argv[1] == "import-prices":
        done, skipped = import_prices_csv(sys.argv[2])
        print(f"Imported {done} prices ({skipped} rows skipped)")
    elif len(sys.argv) >= 4 and sys.argv[1] == "set-price":
        # python database.py set-price 249504 1010000 [PREORDER|CON HANG|...]
        with db_session() as conn:
            current = get_price(conn, sys.argv[2])
            set_price(
                conn, sys.argv[2], sys.argv[3],
                stock_status=" ".join(sys.argv[4:]) or None,
                # Changing the price must not wipe the stock count.
                stock=current["stock"] if current else None,
            )
            conn.commit()
            print(get_price(conn, sys.argv[2]))
    elif len(sys.argv) == 4 and sys.argv[1] == "set-stock":
        # python database.py set-stock 249504 5      (none = không theo dõi số lượng)
        value = sys.argv[3].strip().lower()
        try:
            with db_session() as conn:
                print(set_stock(conn, sys.argv[2], None if value in ("none", "-") else int(value)))
        except ValueError as exc:
            print("Lỗi:", exc)
    elif len(sys.argv) == 2 and sys.argv[1] == "list-reservations":
        with db_session() as conn:
            rows = conn.execute(
                "SELECT r.code, r.status, r.name, r.phone, p.title, r.created_at "
                "FROM preorder_reservations r JOIN preorders p ON p.id = r.preorder_id "
                "ORDER BY r.id DESC"
            ).fetchall()
        for row in rows:
            print(dict(row))
        print(f"{len(rows)} reservations")
    elif len(sys.argv) == 4 and sys.argv[1] == "set-reservation":
        # python database.py set-reservation GC-ABC123 confirmed
        with db_session() as conn:
            print(set_reservation_status(conn, sys.argv[2], sys.argv[3]))
    elif len(sys.argv) == 2 and sys.argv[1] == "list-users":
        with db_session() as conn:
            rows = conn.execute(
                "SELECT id, email, name, phone, created_at FROM users ORDER BY id DESC"
            ).fetchall()
        for row in rows:
            print(dict(row))
        print(f"{len(rows)} users")
    elif len(sys.argv) == 3 and sys.argv[1] == "set-password":
        # python database.py set-password customer@example.com   (asks for the new password)
        import getpass
        import auth

        with db_session() as conn:
            user = get_user_by_email(conn, sys.argv[2])
            if not user:
                print("No account with that email.")
            else:
                new_password = getpass.getpass("New password (min 8 characters): ")
                if len(new_password) < 8:
                    print("Too short -- nothing changed.")
                else:
                    set_user_password(conn, user["id"], auth.hash_password(new_password))
                    print("Password updated for", user["email"])
    elif len(sys.argv) == 2 and sys.argv[1] == "list-prices":
        with db_session() as conn:
            rows = conn.execute(
                "SELECT * FROM release_prices ORDER BY updated_at DESC"
            ).fetchall()
        for row in rows:
            print(dict(row))
        print(f"{len(rows)} prices in {DB_PATH}")
    else:
        print("Commerce database ready at", DB_PATH)
        print("  python database.py set-price <discogs_id> <price> [status]")
        print("  python database.py set-stock <discogs_id> <so_luong|none>")
        print("  python database.py import-prices prices.csv")
        print("  python database.py list-prices")
        print("  python database.py list-reservations")
        print("  python database.py set-reservation <code> <pending|confirmed|arrived|cancelled>")
        print("  python database.py list-users")
        print("  python database.py set-password <email>")
