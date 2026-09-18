import sqlite3
from werkzeug.security import generate_password_hash

# 1. Connect to your database
conn = sqlite3.connect('leave_tracker.db')
cursor = conn.cursor()

# 2. Set your new temporary password
NEW_ADMIN_PASSWORD = "AdminPassword123!"

# 3. Hash the new password using the same security method as app.py
hashed_password = generate_password_hash(NEW_ADMIN_PASSWORD, method='pbkdf2:sha256')

# 4. Update the admin user record and trigger a mandatory password change on login
cursor.execute('''
    UPDATE users 
    SET password = ?, must_change_password = 1 
    WHERE username = 'admin1' OR role = 'admin'
''', (hashed_password,))

conn.commit()

if cursor.rowcount > 0:
    print(f"Success! Admin password has been reset to: {NEW_ADMIN_PASSWORD}")
    print("Log in with this password; you will be prompted to change it immediately.")
else:
    print("Error: No admin account found in the database.")

conn.close()