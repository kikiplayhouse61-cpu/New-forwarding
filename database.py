import sqlite3
import json
import os
from datetime import datetime, date
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "forwarder.db")


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_token TEXT UNIQUE NOT NULL,
                bot_username TEXT,
                bot_name TEXT,
                is_active INTEGER DEFAULT 1,
                connected_at TEXT DEFAULT (datetime('now')),
                last_checked TEXT
            );

            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                chat_id TEXT NOT NULL,
                group_name TEXT,
                group_type TEXT,
                is_active INTEGER DEFAULT 1,
                is_accessible INTEGER DEFAULT 1,
                member_count INTEGER,
                added_at TEXT DEFAULT (datetime('now')),
                last_message_at TEXT,
                total_sent INTEGER DEFAULT 0,
                total_failed INTEGER DEFAULT 0,
                FOREIGN KEY (account_id) REFERENCES accounts(id),
                UNIQUE(account_id, chat_id)
            );

            CREATE TABLE IF NOT EXISTS campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                account_id INTEGER NOT NULL,
                message_text TEXT NOT NULL,
                interval_seconds INTEGER DEFAULT 3600,
                start_time TEXT,
                end_time TEXT,
                status TEXT DEFAULT 'stopped',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                last_run TEXT,
                next_run TEXT,
                total_sent INTEGER DEFAULT 0,
                total_failed INTEGER DEFAULT 0,
                FOREIGN KEY (account_id) REFERENCES accounts(id)
            );

            CREATE TABLE IF NOT EXISTS campaign_groups (
                campaign_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                PRIMARY KEY (campaign_id, group_id),
                FOREIGN KEY (campaign_id) REFERENCES campaigns(id),
                FOREIGN KEY (group_id) REFERENCES groups(id)
            );

            CREATE TABLE IF NOT EXISTS message_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                error_reason TEXT,
                sent_at TEXT DEFAULT (datetime('now')),
                message_preview TEXT,
                FOREIGN KEY (campaign_id) REFERENCES campaigns(id),
                FOREIGN KEY (group_id) REFERENCES groups(id)
            );

            CREATE TABLE IF NOT EXISTS daily_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stat_date TEXT NOT NULL,
                account_id INTEGER,
                group_id INTEGER,
                campaign_id INTEGER,
                total_sent INTEGER DEFAULT 0,
                total_success INTEGER DEFAULT 0,
                total_failed INTEGER DEFAULT 0,
                UNIQUE(stat_date, account_id, group_id, campaign_id)
            );

            CREATE TABLE IF NOT EXISTS templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS admin (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now'))
            );

            INSERT OR IGNORE INTO settings (key, value) VALUES
                ('max_retries', '2'),
                ('retry_delay', '30'),
                ('daily_summary_time', '23:00'),
                ('timezone', 'UTC'),
                ('backup_enabled', '1'),
                ('notification_on_fail', '1');
        """)


# ── Account operations ────────────────────────────────────────────────────────

def add_account(bot_token, bot_username, bot_name):
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO accounts (bot_token, bot_username, bot_name) VALUES (?,?,?)",
            (bot_token, bot_username, bot_name)
        )


def get_accounts():
    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM accounts WHERE is_active=1").fetchall()]


def get_account(account_id):
    with get_db() as conn:
        r = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        return dict(r) if r else None


def delete_account(account_id):
    with get_db() as conn:
        conn.execute("UPDATE accounts SET is_active=0 WHERE id=?", (account_id,))


# ── Group operations ──────────────────────────────────────────────────────────

def add_group(account_id, chat_id, group_name, group_type="group", member_count=None):
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO groups
               (account_id, chat_id, group_name, group_type, member_count)
               VALUES (?,?,?,?,?)""",
            (account_id, str(chat_id), group_name, group_type, member_count)
        )


def get_groups(account_id=None, active_only=True):
    with get_db() as conn:
        q = "SELECT g.*, a.bot_username FROM groups g JOIN accounts a ON g.account_id=a.id WHERE 1=1"
        params = []
        if account_id:
            q += " AND g.account_id=?"
            params.append(account_id)
        if active_only:
            q += " AND g.is_active=1"
        return [dict(r) for r in conn.execute(q, params).fetchall()]


def get_group(group_id):
    with get_db() as conn:
        r = conn.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
        return dict(r) if r else None


def toggle_group(group_id, is_active):
    with get_db() as conn:
        conn.execute("UPDATE groups SET is_active=? WHERE id=?", (is_active, group_id))


def delete_group(group_id):
    with get_db() as conn:
        conn.execute("DELETE FROM groups WHERE id=?", (group_id,))


def mark_group_inaccessible(group_id):
    with get_db() as conn:
        conn.execute("UPDATE groups SET is_accessible=0 WHERE id=?", (group_id,))


# ── Campaign operations ───────────────────────────────────────────────────────

def create_campaign(name, account_id, message_text, interval_seconds, group_ids,
                    start_time=None, end_time=None):
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO campaigns (name, account_id, message_text, interval_seconds, start_time, end_time)
               VALUES (?,?,?,?,?,?)""",
            (name, account_id, message_text, interval_seconds, start_time, end_time)
        )
        campaign_id = cur.lastrowid
        for gid in group_ids:
            conn.execute(
                "INSERT OR IGNORE INTO campaign_groups (campaign_id, group_id) VALUES (?,?)",
                (campaign_id, gid)
            )
        return campaign_id


def get_campaigns(status=None):
    with get_db() as conn:
        q = """SELECT c.*, a.bot_username,
                      COUNT(cg.group_id) as group_count
               FROM campaigns c
               LEFT JOIN accounts a ON c.account_id=a.id
               LEFT JOIN campaign_groups cg ON c.id=cg.campaign_id
               WHERE 1=1"""
        params = []
        if status:
            q += " AND c.status=?"
            params.append(status)
        q += " GROUP BY c.id ORDER BY c.created_at DESC"
        return [dict(r) for r in conn.execute(q, params).fetchall()]


def get_campaign(campaign_id):
    with get_db() as conn:
        r = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not r:
            return None
        c = dict(r)
        groups = conn.execute(
            """SELECT g.* FROM groups g
               JOIN campaign_groups cg ON g.id=cg.group_id
               WHERE cg.campaign_id=?""",
            (campaign_id,)
        ).fetchall()
        c["groups"] = [dict(g) for g in groups]
        return c


def update_campaign_status(campaign_id, status, next_run=None, last_run=None):
    with get_db() as conn:
        conn.execute(
            """UPDATE campaigns SET status=?, next_run=?, last_run=coalesce(?,last_run),
               updated_at=datetime('now') WHERE id=?""",
            (status, next_run, last_run, campaign_id)
        )


def update_campaign(campaign_id, **kwargs):
    allowed = {"name", "message_text", "interval_seconds", "start_time", "end_time", "status"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    set_clause = ", ".join(f"{k}=?" for k in fields)
    with get_db() as conn:
        conn.execute(
            f"UPDATE campaigns SET {set_clause}, updated_at=datetime('now') WHERE id=?",
            (*fields.values(), campaign_id)
        )


def delete_campaign(campaign_id):
    with get_db() as conn:
        conn.execute("DELETE FROM campaign_groups WHERE campaign_id=?", (campaign_id,))
        conn.execute("DELETE FROM campaigns WHERE id=?", (campaign_id,))


def duplicate_campaign(campaign_id):
    c = get_campaign(campaign_id)
    if not c:
        return None
    group_ids = [g["id"] for g in c.get("groups", [])]
    return create_campaign(
        name=c["name"] + " (copy)",
        account_id=c["account_id"],
        message_text=c["message_text"],
        interval_seconds=c["interval_seconds"],
        group_ids=group_ids,
        start_time=c.get("start_time"),
        end_time=c.get("end_time"),
    )


# ── Logging & stats ───────────────────────────────────────────────────────────

def log_message(campaign_id, group_id, account_id, status, error_reason=None, message_preview=None):
    today = date.today().isoformat()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO message_logs
               (campaign_id, group_id, account_id, status, error_reason, message_preview)
               VALUES (?,?,?,?,?,?)""",
            (campaign_id, group_id, account_id, status, error_reason,
             message_preview[:100] if message_preview else None)
        )
        # Update daily stats
        success = 1 if status == "success" else 0
        failed = 1 if status == "failed" else 0
        conn.execute(
            """INSERT INTO daily_stats (stat_date, account_id, group_id, campaign_id,
               total_sent, total_success, total_failed)
               VALUES (?,?,?,?,1,?,?)
               ON CONFLICT(stat_date, account_id, group_id, campaign_id)
               DO UPDATE SET total_sent=total_sent+1,
                             total_success=total_success+?,
                             total_failed=total_failed+?""",
            (today, account_id, group_id, campaign_id, success, failed, success, failed)
        )
        # Update group totals
        if status == "success":
            conn.execute(
                "UPDATE groups SET total_sent=total_sent+1, last_message_at=datetime('now') WHERE id=?",
                (group_id,)
            )
            conn.execute("UPDATE campaigns SET total_sent=total_sent+1 WHERE id=?", (campaign_id,))
        else:
            conn.execute("UPDATE groups SET total_failed=total_failed+1 WHERE id=?", (group_id,))
            conn.execute("UPDATE campaigns SET total_failed=total_failed+1 WHERE id=?", (campaign_id,))


def get_dashboard_stats():
    today = date.today().isoformat()
    with get_db() as conn:
        accounts = conn.execute("SELECT COUNT(*) FROM accounts WHERE is_active=1").fetchone()[0]
        groups = conn.execute("SELECT COUNT(*) FROM groups WHERE is_active=1").fetchone()[0]
        active_campaigns = conn.execute("SELECT COUNT(*) FROM campaigns WHERE status='running'").fetchone()[0]
        paused_campaigns = conn.execute("SELECT COUNT(*) FROM campaigns WHERE status='paused'").fetchone()[0]

        today_row = conn.execute(
            """SELECT COALESCE(SUM(total_sent),0) as sent,
                      COALESCE(SUM(total_success),0) as success,
                      COALESCE(SUM(total_failed),0) as failed
               FROM daily_stats WHERE stat_date=?""",
            (today,)
        ).fetchone()

        all_time = conn.execute(
            """SELECT COALESCE(SUM(total_success),0) as success,
                      COALESCE(SUM(total_failed),0) as failed
               FROM daily_stats"""
        ).fetchone()

        last_msg = conn.execute(
            "SELECT sent_at FROM message_logs ORDER BY sent_at DESC LIMIT 1"
        ).fetchone()

        return {
            "accounts": accounts,
            "groups": groups,
            "active_campaigns": active_campaigns,
            "paused_campaigns": paused_campaigns,
            "today_sent": today_row["sent"],
            "today_success": today_row["success"],
            "today_failed": today_row["failed"],
            "all_success": all_time["success"],
            "all_failed": all_time["failed"],
            "last_message_at": last_msg["sent_at"] if last_msg else None,
        }


def get_logs(limit=100, offset=0, group_id=None, campaign_id=None,
             status=None, date_from=None, date_to=None):
    with get_db() as conn:
        q = """SELECT ml.*, g.group_name, c.name as campaign_name, a.bot_username
               FROM message_logs ml
               LEFT JOIN groups g ON ml.group_id=g.id
               LEFT JOIN campaigns c ON ml.campaign_id=c.id
               LEFT JOIN accounts a ON ml.account_id=a.id
               WHERE 1=1"""
        params = []
        if group_id:
            q += " AND ml.group_id=?"; params.append(group_id)
        if campaign_id:
            q += " AND ml.campaign_id=?"; params.append(campaign_id)
        if status:
            q += " AND ml.status=?"; params.append(status)
        if date_from:
            q += " AND ml.sent_at>=?"; params.append(date_from)
        if date_to:
            q += " AND ml.sent_at<=?"; params.append(date_to + " 23:59:59")
        q += " ORDER BY ml.sent_at DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        return [dict(r) for r in conn.execute(q, params).fetchall()]


def get_reports(period="daily"):
    with get_db() as conn:
        if period == "daily":
            rows = conn.execute(
                """SELECT stat_date, SUM(total_sent) as sent,
                          SUM(total_success) as success, SUM(total_failed) as failed
                   FROM daily_stats GROUP BY stat_date ORDER BY stat_date DESC LIMIT 30"""
            ).fetchall()
        elif period == "weekly":
            rows = conn.execute(
                """SELECT strftime('%Y-W%W', stat_date) as week,
                          SUM(total_sent) as sent,
                          SUM(total_success) as success, SUM(total_failed) as failed
                   FROM daily_stats GROUP BY week ORDER BY week DESC LIMIT 12"""
            ).fetchall()
        else:  # monthly
            rows = conn.execute(
                """SELECT strftime('%Y-%m', stat_date) as month,
                          SUM(total_sent) as sent,
                          SUM(total_success) as success, SUM(total_failed) as failed
                   FROM daily_stats GROUP BY month ORDER BY month DESC LIMIT 12"""
            ).fetchall()
        return [dict(r) for r in rows]


# ── Templates ─────────────────────────────────────────────────────────────────

def get_templates():
    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM templates ORDER BY created_at DESC").fetchall()]


def add_template(name, content):
    with get_db() as conn:
        conn.execute("INSERT INTO templates (name, content) VALUES (?,?)", (name, content))


def delete_template(template_id):
    with get_db() as conn:
        conn.execute("DELETE FROM templates WHERE id=?", (template_id,))


# ── Settings ──────────────────────────────────────────────────────────────────

def get_settings():
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}


def set_setting(key, value):
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?,?,datetime('now'))",
            (key, value)
        )


# ── Admin auth ────────────────────────────────────────────────────────────────

def get_admin(username):
    with get_db() as conn:
        r = conn.execute("SELECT * FROM admin WHERE username=?", (username,)).fetchone()
        return dict(r) if r else None


def create_admin(username, password_hash):
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO admin (username, password_hash) VALUES (?,?)",
                     (username, password_hash))


def admin_exists():
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM admin").fetchone()[0] > 0
