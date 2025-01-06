import sqlite3

def init_db():
    conn = sqlite3.connect("database/users.db")
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER UNIQUE,
        username TEXT,
        subscription_expiry DATE
    )
    """)
    conn.commit()
    conn.close()

def add_user(telegram_id, username):
    conn = sqlite3.connect("database/users.db")
    cursor = conn.cursor()
    cursor.execute("""
    INSERT OR IGNORE INTO users (telegram_id, username, subscription_expiry)
    VALUES (?, ?, NULL)
    """, (telegram_id, username))
    conn.commit()
    conn.close()

def update_subscription(telegram_id, expiry_date):
    conn = sqlite3.connect("database/users.db")
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE users SET subscription_expiry = ? WHERE telegram_id = ?
    """, (expiry_date, telegram_id))
    conn.commit()
    conn.close()

def get_subscription_status(telegram_id):
    conn = sqlite3.connect("database/users.db")
    cursor = conn.cursor()
    cursor.execute("""
    SELECT subscription_expiry FROM users WHERE telegram_id = ?
    """, (telegram_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None
