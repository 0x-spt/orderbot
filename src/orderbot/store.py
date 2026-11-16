import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from orderbot.domain import MAIN, Action, Decision, Order, OrderLine, PriceSettings, Reason

STATES = {"NEW", "OBSERVED", "AWAITING_RESERVE", "ESCALATED", "CONFIRMING",
          "CONFIRMED", "HANDLED_EXTERNALLY", "REJECTED_BY_HUMAN", "RESERVE_APPROVED",
          "CANCELLED_BY_HUMAN"}
MODES = {"observe", "confirm"}
SETTING_KEYS = {"soinvest", "tolerance", "multi_qty_verified", "brand_discount"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    marketplace TEXT NOT NULL, order_id TEXT NOT NULL,
    state TEXT NOT NULL, order_json TEXT NOT NULL, decision_json TEXT,
    created_ts TEXT NOT NULL, updated_ts TEXT NOT NULL,
    PRIMARY KEY (marketplace, order_id));
CREATE TABLE IF NOT EXISTS settings_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, marketplace TEXT NOT NULL,
    key TEXT NOT NULL, value_json TEXT NOT NULL, user TEXT NOT NULL, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS flags (
    name TEXT PRIMARY KEY, value TEXT NOT NULL, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, marketplace TEXT,
    order_id TEXT, event TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL,
    payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alert_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, marketplace TEXT NOT NULL,
    order_id TEXT NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
    kind TEXT NOT NULL, html TEXT NOT NULL, ts TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS alert_messages_order ON alert_messages (marketplace, order_id);
CREATE TABLE IF NOT EXISTS managers (
    user_id INTEGER PRIMARY KEY, name TEXT, granted_by TEXT, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lamoda_orders (
    order_nr TEXT PRIMARY KEY, state TEXT NOT NULL, partner_id INTEGER,
    message_id INTEGER, text TEXT, warned INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0, sent_files TEXT NOT NULL DEFAULT '[]',
    ts TEXT NOT NULL);
"""

# Lamoda label orders (lamoda_labels.py): "baseline" = awaiting before the first run, never
# posted; "pending" = the message is posted, its labels are not all sent yet; "done";
# "gave_up" = the labels never came, people were told to take them from the cabinet.
LAMODA_STATES = {"baseline", "pending", "done", "gave_up"}
LAMODA_BASELINE_FLAG = "lamoda_baseline"
# Shipment reminders (shipments.py): the last slot posted, "YYYY-MM-DD HH:MM" (Moscow).
SHIPMENTS_LAST_FLAG = "shipments_last_slot"


@dataclass
class LamodaRow:
    order_nr: str
    state: str
    partner_id: int | None
    message_id: int | None
    text: str | None
    warned: bool
    attempts: int
    sent_files: list[str]   # names of the label files already in the group


@dataclass
class Manager:
    """A person given the order buttons in the bot (not from .env)."""
    user_id: int
    name: str | None
    granted_by: str | None
    ts: datetime


@dataclass
class AlertMessage:
    chat_id: int
    message_id: int
    kind: str
    html: str
    result: str = ""   # the last result line added under the order (finish)
    notes: str = ""    # lines added after it, e.g. the stock sync report, "\n"-joined


def _utc_now():
    return datetime.now(UTC)


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _default(o):
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"not serializable: {type(o)}")


def _dumps(o) -> str:
    return json.dumps(o, default=_default, ensure_ascii=False)


def _dec(v):
    return None if v is None else Decimal(v)


def _dt(v):
    return None if v is None else datetime.fromisoformat(v)


def _order_to_json(o: Order) -> str:
    return _dumps({
        "marketplace": o.marketplace, "order_id": o.order_id,
        "lines": [{"article": l.article, "qty": l.qty, "warehouse": l.warehouse,
                   "paid_total": l.paid_total, "brand": l.brand, "promos": l.promos,
                   "points": l.points, "paid_foreign": l.paid_foreign}
                  for l in o.lines],
        "created_at": o.created_at, "deadline": o.deadline, "status": o.status,
        "campaign": o.campaign, "express": o.express, "mp_warehouse": o.mp_warehouse,
        "account": o.account})


def _order_from_json(s: str) -> Order:
    d = json.loads(s)
    lines = [OrderLine(l["article"], l["qty"], l["warehouse"], _dec(l["paid_total"]), l["brand"],
                       list(l.get("promos") or []), _dec(l.get("points")),
                       l.get("paid_foreign"))
             for l in d["lines"]]
    return Order(d["marketplace"], d["order_id"], lines, _dt(d["created_at"]),
                 _dt(d["deadline"]), d["status"], d.get("campaign"),
                 bool(d.get("express", False)), d.get("mp_warehouse"),
                 d.get("account") or MAIN)  # rows written before the second company: MAIN


def _decision_to_json(d: Decision) -> str:
    return _dumps({"action": d.action.value,
                   "reasons": [[a, r.value] for a, r in d.reasons],
                   "floors": d.floors})


def _decision_from_json(s: str) -> Decision:
    d = json.loads(s)
    return Decision(Action(d["action"]), [(a, Reason(r)) for a, r in d["reasons"]],
                    {k: Decimal(v) for k, v in d["floors"].items()})


encode_order, decode_order = _order_to_json, _order_from_json
encode_decision, decode_decision = _decision_to_json, _decision_from_json


class Store:
    def __init__(self, path, clock=_utc_now):
        self._clock = clock
        self._db = sqlite3.connect(str(path))
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(alert_messages)")}
        for col in ("result_html", "notes_html"):   # databases from before the stock sync
            if col not in cols:
                self._db.execute(f"ALTER TABLE alert_messages ADD COLUMN {col} TEXT NOT NULL"
                                 " DEFAULT ''")
        self._db.commit()

    def _now(self) -> str:
        return _aware(self._clock()).isoformat()

    # orders
    def upsert_new(self, order: Order) -> bool:
        now = self._now()
        with self._db:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO orders (marketplace, order_id, state, order_json,"
                " created_ts, updated_ts) VALUES (?,?,?,?,?,?)",
                (order.marketplace, order.order_id, "NEW", _order_to_json(order), now, now))
        return cur.rowcount == 1

    def get_state(self, mp, order_id):
        r = self._db.execute("SELECT state FROM orders WHERE marketplace=? AND order_id=?",
                             (mp, order_id)).fetchone()
        return r[0] if r else None

    def set_state(self, mp, order_id, state, decision: Decision | None = None):
        if state not in STATES:
            raise ValueError(f"unknown state {state}")
        with self._db:
            if decision is None:
                cur = self._db.execute(
                    "UPDATE orders SET state=?, updated_ts=? WHERE marketplace=? AND order_id=?",
                    (state, self._now(), mp, order_id))
            else:
                cur = self._db.execute(
                    "UPDATE orders SET state=?, decision_json=?, updated_ts=?"
                    " WHERE marketplace=? AND order_id=?",
                    (state, _decision_to_json(decision), self._now(), mp, order_id))
        if cur.rowcount != 1:
            raise KeyError((mp, order_id))

    def set_state_if(self, mp, order_id, expected: set[str], new: str) -> bool:
        if new not in STATES:
            raise ValueError(f"unknown state {new}")
        bad = set(expected) - STATES
        if bad:
            raise ValueError(f"unknown state {sorted(bad)}")
        if not expected:
            return False
        marks = ",".join("?" * len(expected))
        with self._db:
            cur = self._db.execute(
                "UPDATE orders SET state=?, updated_ts=? WHERE marketplace=? AND order_id=?"
                f" AND state IN ({marks})", (new, self._now(), mp, order_id, *sorted(expected)))
        return cur.rowcount == 1

    def get_order(self, mp, order_id) -> Order:
        r = self._db.execute("SELECT order_json FROM orders WHERE marketplace=? AND order_id=?",
                             (mp, order_id)).fetchone()
        if not r:
            raise KeyError((mp, order_id))
        return _order_from_json(r[0])

    def get_decision(self, mp, order_id) -> Decision | None:
        r = self._db.execute("SELECT decision_json FROM orders WHERE marketplace=? AND order_id=?",
                             (mp, order_id)).fetchone()
        return _decision_from_json(r[0]) if r and r[0] else None

    def pending(self) -> list[tuple[str, str]]:
        rows = self._db.execute("SELECT marketplace, order_id FROM orders WHERE state='NEW'"
                                " ORDER BY created_ts, rowid").fetchall()
        return [(a, b) for a, b in rows]

    def in_state(self, state) -> list[tuple[str, str]]:
        rows = self._db.execute("SELECT marketplace, order_id FROM orders WHERE state=?"
                                " ORDER BY created_ts, rowid", (state,)).fetchall()
        return [(a, b) for a, b in rows]

    # settings
    def change_setting(self, mp, key, value, user):
        if key not in SETTING_KEYS:
            raise ValueError(f"unknown setting {key}")
        if key == "brand_discount":
            value = {"brand": value["brand"].strip().lower(), "percent": value["percent"]}
        with self._db:
            self._db.execute(
                "INSERT INTO settings_history (marketplace, key, value_json, user, ts)"
                " VALUES (?,?,?,?,?)", (mp, key, _dumps(value), user, self._now()))
        self.log(mp, None, "setting_changed", {"key": key, "value": value, "user": user})

    def settings_at(self, mp, when: datetime) -> PriceSettings:
        s = PriceSettings(False, {}, Decimal("50"), False)
        rows = self._db.execute(
            "SELECT key, value_json, ts FROM settings_history WHERE marketplace=? ORDER BY id",
            (mp,)).fetchall()
        when = _aware(when)
        for key, vj, ts in rows:
            if datetime.fromisoformat(ts) > when:
                continue
            v = json.loads(vj)
            if key == "soinvest":
                s.soinvest = bool(v)
            elif key == "multi_qty_verified":
                s.multi_qty_verified = bool(v)
            elif key == "tolerance":
                s.tolerance = Decimal(str(v))
            elif key == "brand_discount":
                if v["percent"] is None:
                    s.brand_discounts.pop(v["brand"], None)
                else:
                    s.brand_discounts[v["brand"]] = Decimal(str(v["percent"]))
        return s

    # flags
    def _flag(self, name):
        return self._db.execute("SELECT value, ts FROM flags WHERE name=?", (name,)).fetchone()

    def _set_flag(self, name, value):
        with self._db:
            self._db.execute("INSERT INTO flags (name, value, ts) VALUES (?,?,?) ON CONFLICT(name)"
                             " DO UPDATE SET value=excluded.value, ts=excluded.ts",
                             (name, value, self._now()))

    def get_mode(self, mp) -> str:
        r = self._flag(f"mode:{mp}")
        return r[0] if r else "observe"

    def set_mode(self, mp, mode, user):
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode}")
        self._set_flag(f"mode:{mp}", mode)
        self.log(mp, None, "mode_changed", {"mode": mode, "user": user})

    def is_paused(self) -> bool:
        r = self._flag("paused")
        return bool(r and r[0] == "1")

    def paused_since(self) -> datetime | None:
        r = self._flag("paused")
        return datetime.fromisoformat(r[1]) if r and r[0] == "1" else None

    def set_paused(self, paused: bool, user):
        if paused and self.is_paused():
            return
        self._set_flag("paused", "1" if paused else "0")
        self.log(None, None, "paused" if paused else "resumed", {"user": user})

    # stock sync on the other marketplaces: off until an admin turns it on
    # Yandex has a switch of its own: its writes stay off until verified.
    def get_stocksync(self, mp: str | None = None) -> bool:
        r = self._flag("stocksync" if mp is None else f"stocksync:{mp}")
        return bool(r and r[0] == "1")

    def set_stocksync(self, on: bool, user, mp: str | None = None) -> None:
        self._set_flag("stocksync" if mp is None else f"stocksync:{mp}", "1" if on else "0")
        payload = {"on": bool(on), "user": user}
        if mp is not None:
            payload["marketplace"] = mp
        self.log(None, None, "stocksync_changed", payload)

    # journal
    def has_event(self, mp, order_id, event) -> bool:
        return self._db.execute(
            "SELECT 1 FROM journal WHERE marketplace=? AND order_id=? AND event=? LIMIT 1",
            (mp, order_id, event)).fetchone() is not None

    def log(self, mp, order_id, event, payload: dict):
        with self._db:
            self._db.execute(
                "INSERT INTO journal (ts, marketplace, order_id, event, payload_json)"
                " VALUES (?,?,?,?,?)", (self._now(), mp, order_id, event, _dumps(payload)))

    def last_event_ts(self, event) -> datetime | None:
        r = self._db.execute("SELECT ts FROM journal WHERE event=? ORDER BY id DESC LIMIT 1",
                             (event,)).fetchone()
        return datetime.fromisoformat(r[0]) if r else None

    def last_event(self, event) -> dict | None:
        r = self._db.execute("SELECT ts, payload_json FROM journal WHERE event=?"
                             " ORDER BY id DESC LIMIT 1", (event,)).fetchone()
        return {"ts": datetime.fromisoformat(r[0]), "payload": json.loads(r[1])} if r else None

    def events(self, mp=None, order_id=None) -> list[dict]:
        q, a = "SELECT ts, marketplace, order_id, event, payload_json FROM journal WHERE 1=1", []
        if mp is not None:
            q += " AND marketplace=?"
            a.append(mp)
        if order_id is not None:
            q += " AND order_id=?"
            a.append(order_id)
        return [{"ts": datetime.fromisoformat(ts), "marketplace": m, "order_id": o,
                 "event": e, "payload": json.loads(p)}
                for ts, m, o, e, p in self._db.execute(q + " ORDER BY id", a)]

    def open_role_request(self, user_id: int) -> dict | None:
        """The payload of the user's last access request when no admin has answered it."""
        r = self._db.execute(
            "SELECT event, payload_json FROM journal WHERE event IN"
            " ('role_requested', 'role_granted', 'role_denied')"
            " AND json_extract(payload_json, '$.user_id') = ? ORDER BY id DESC LIMIT 1",
            (user_id,)).fetchone()
        return json.loads(r[1]) if r and r[0] == "role_requested" else None

    # managers given in the bot; the .env ones are not stored
    def add_manager(self, user_id: int, name, granted_by) -> bool:
        """False when the user already is a manager (nothing changes)."""
        with self._db:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO managers (user_id, name, granted_by, ts) VALUES (?,?,?,?)",
                (user_id, name, granted_by, self._now()))
        return cur.rowcount == 1

    def remove_manager(self, user_id: int) -> bool:
        with self._db:
            cur = self._db.execute("DELETE FROM managers WHERE user_id=?", (user_id,))
        return cur.rowcount == 1

    def managers(self) -> list[Manager]:
        return [Manager(u, n, g, datetime.fromisoformat(t)) for u, n, g, t in self._db.execute(
            "SELECT user_id, name, granted_by, ts FROM managers ORDER BY ts, user_id")]

    # alerts that could not be sent: kept until a later send succeeds
    def queue_alert(self, kind: str, payload: dict) -> int:
        with self._db:
            cur = self._db.execute("INSERT INTO outbox (ts, kind, payload_json) VALUES (?,?,?)",
                                   (self._now(), kind, _dumps(payload)))
        self.log(payload.get("marketplace"), payload.get("order_id"), "alert_failed",
                 {"kind": kind, "outbox_id": cur.lastrowid})
        return cur.lastrowid

    def outbox(self) -> list[tuple[int, str, dict]]:
        return [(i, k, json.loads(p)) for i, k, p in self._db.execute(
            "SELECT id, kind, payload_json FROM outbox ORDER BY id")]

    def drop_alert(self, alert_id: int) -> None:
        with self._db:
            self._db.execute("DELETE FROM outbox WHERE id=?", (alert_id,))

    # sent Telegram messages per order, so every copy can be edited later
    def save_alert_message(self, mp, order_id, chat_id, message_id, kind, html) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO alert_messages (marketplace, order_id, chat_id, message_id, kind,"
                " html, ts) VALUES (?,?,?,?,?,?,?)",
                (mp, order_id, chat_id, message_id, kind, html, self._now()))

    def alert_messages(self, mp, order_id) -> list[AlertMessage]:
        return [AlertMessage(c, m, k, h, r or "", n or "") for c, m, k, h, r, n in self._db.execute(
            "SELECT chat_id, message_id, kind, html, result_html, notes_html FROM alert_messages"
            " WHERE marketplace=? AND order_id=? ORDER BY id", (mp, order_id))]

    def set_alert_result(self, mp, order_id, line_html: str) -> None:
        with self._db:
            self._db.execute("UPDATE alert_messages SET result_html=? WHERE marketplace=? AND"
                             " order_id=?", (line_html, mp, order_id))

    def add_alert_note(self, mp, order_id, line_html: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE alert_messages SET notes_html = CASE WHEN notes_html = '' THEN ?"
                " ELSE notes_html || char(10) || ? END WHERE marketplace=? AND order_id=?",
                (line_html, line_html, mp, order_id))

    # Lamoda orders posted with their labels: each order exactly once, across restarts
    def lamoda_known(self, order_nr) -> bool:
        return self._db.execute("SELECT 1 FROM lamoda_orders WHERE order_nr=?",
                                (order_nr,)).fetchone() is not None

    def lamoda_add(self, order_nr, state, partner_id=None, message_id=None, text=None,
                   warned=False, attempts=0) -> None:
        if state not in LAMODA_STATES:
            raise ValueError(f"unknown Lamoda state {state}")
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO lamoda_orders (order_nr, state, partner_id, message_id,"
                " text, warned, attempts, ts) VALUES (?,?,?,?,?,?,?,?)",
                (order_nr, state, partner_id, message_id, text, int(bool(warned)), attempts,
                 self._now()))

    def lamoda_update(self, order_nr, **fields) -> None:
        allowed = {"state", "warned", "attempts", "sent_files"}
        if not fields or set(fields) - allowed:
            raise ValueError(f"cannot update {sorted(fields)}")
        if fields.get("state", "done") not in LAMODA_STATES:
            raise ValueError(f"unknown Lamoda state {fields['state']}")
        if "sent_files" in fields:
            fields["sent_files"] = json.dumps(list(fields["sent_files"]), ensure_ascii=False)
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._db:
            self._db.execute(f"UPDATE lamoda_orders SET {cols}, ts=? WHERE order_nr=?",
                             (*[int(v) if isinstance(v, bool) else v for v in fields.values()],
                              self._now(), order_nr))

    def lamoda_pending(self) -> list[LamodaRow]:
        return [LamodaRow(n, s, p, m, t, bool(w), a, json.loads(f))
                for n, s, p, m, t, w, a, f in
                self._db.execute(
                    "SELECT order_nr, state, partner_id, message_id, text, warned, attempts,"
                    " sent_files FROM lamoda_orders WHERE state='pending' ORDER BY ts, order_nr")]

    def lamoda_baseline_done(self) -> bool:
        return self._flag(LAMODA_BASELINE_FLAG) is not None

    def lamoda_set_baseline(self, order_nrs) -> None:
        """First run: the given orders count as posted (people handled them by hand)."""
        for nr in order_nrs:
            self.lamoda_add(nr, "baseline")
        self._set_flag(LAMODA_BASELINE_FLAG, "1")

    # shipment reminders
    def shipments_last_slot(self) -> str | None:
        r = self._flag(SHIPMENTS_LAST_FLAG)
        return r[0] if r else None

    def set_shipments_last_slot(self, key: str) -> None:
        self._set_flag(SHIPMENTS_LAST_FLAG, key)
