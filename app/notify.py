"""
In-app notifications ("messages from the lab", HOD decisions, approval requests).
A notification targets one user, or every user of a role (user_id NULL).
"""
from .db import now_ist


def send(conn, message, link=None, user_id=None, role=None):
    conn.execute("INSERT INTO notifications(user_id, role, message, link, created_at) VALUES (?,?,?,?,?)",
                 (user_id, role, message, link, now_ist()))


def for_user(conn, user, unread_only=False, limit=50):
    sql = ("SELECT * FROM notifications WHERE (user_id=? OR (user_id IS NULL AND role=?))"
           + (" AND read_at IS NULL" if unread_only else "") + " ORDER BY id DESC LIMIT ?")
    return conn.execute(sql, (user["id"], user["role"], limit)).fetchall()


def unread_count(conn, user):
    return conn.execute("SELECT COUNT(*) FROM notifications WHERE read_at IS NULL AND "
                        "(user_id=? OR (user_id IS NULL AND role=?))", (user["id"], user["role"])).fetchone()[0]


def mark_read(conn, user):
    conn.execute("UPDATE notifications SET read_at=? WHERE read_at IS NULL AND (user_id=? OR (user_id IS NULL AND role=?))",
                 (now_ist(), user["id"], user["role"]))
