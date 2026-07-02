DB_PASSWORD = "SuperSecret123!"
def get_user(conn, user_id):
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id = " + str(user_id))
    return cur.fetchall()
