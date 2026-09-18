
import datetime
import sqlite3
from functools import wraps
from flask import Flask, render_template_string, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = 'workday_enterprise_leave_key_v3'
DATABASE = 'leave_tracker.db'

# --- DATABASE SETUP ---
def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        # 1. Users Table
        db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                name TEXT NOT NULL,
                role TEXT NOT NULL, -- 'admin', 'manager', or 'employee'
                join_date TEXT NOT NULL
            )
        ''')

        # 2. Dynamic Leave Types Table
        db.execute('''
            CREATE TABLE IF NOT EXISTS leave_types (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                default_days INTEGER NOT NULL
            )
        ''')

        # 3. User Balances Table
        db.execute('''
            CREATE TABLE IF NOT EXISTS user_balances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                leave_type_id INTEGER NOT NULL,
                year INTEGER NOT NULL,
                allocated_days REAL NOT NULL,
                used_days REAL DEFAULT 0,
                carried_over_days REAL DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users (id),
                FOREIGN KEY (leave_type_id) REFERENCES leave_types (id)
            )
        ''')

        # 4. Leave Requests Table
        db.execute('''
            CREATE TABLE IF NOT EXISTS leave_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                leave_type_id INTEGER NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                total_days INTEGER NOT NULL,
                reason TEXT,
                status TEXT DEFAULT 'Pending',
                FOREIGN KEY (user_id) REFERENCES users (id),
                FOREIGN KEY (leave_type_id) REFERENCES leave_types (id)
            )
        ''')

        # 5. System Settings Table
        db.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        ''')

        settings_defaults = {
            'carry_forward_percent': '50',
            'carry_forward_expiry_month': '3',
            'carry_forward_expiry_day': '31'
        }
        for key, val in settings_defaults.items():
            db.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (key, val))

        cursor = db.execute('SELECT COUNT(*) FROM leave_types')
        if cursor.fetchone()[0] == 0:
            db.execute('INSERT INTO leave_types (name, default_days) VALUES (?, ?)', ('Annual Leave', 14))
            db.execute('INSERT INTO leave_types (name, default_days) VALUES (?, ?)', ('Sick Leave', 14))
            db.execute('INSERT INTO leave_types (name, default_days) VALUES (?, ?)', ('Hospitalisation Leave', 60))

        cursor = db.execute('SELECT COUNT(*) FROM users')
        if cursor.fetchone()[0] == 0:
            today_str = datetime.date.today().strftime('%Y-%m-%d')
            db.execute('INSERT INTO users (username, password, name, role, join_date) VALUES (?, ?, ?, ?, ?)',
                       ('admin1', generate_password_hash('admin123', method='pbkdf2:sha256'), 'System Admin', 'admin', today_str))
        db.commit()

# --- BUSINESS LOGIC HELPERS ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def calculate_days(start_str, end_str):
    d1 = datetime.datetime.strptime(start_str, '%Y-%m-%d')
    d2 = datetime.datetime.strptime(end_str, '%Y-%m-%d')
    return (d2 - d1).days + 1

def calculate_prorated_days(default_days, join_date_str, current_year):
    join_date = datetime.datetime.strptime(join_date_str, '%Y-%m-%d').date()
    if join_date.year < current_year:
        return float(default_days)
    elif join_date.year > current_year:
        return 0.0
    
    remaining_months = 12 - join_date.month + 1
    prorated = (default_days / 12.0) * remaining_months
    return round(prorated, 1)

def sync_user_balances(user_id, join_date_str):
    current_year = datetime.date.today().year
    with get_db() as db:
        types = db.execute('SELECT * FROM leave_types').fetchall()
        for lt in types:
            bal = db.execute('SELECT * FROM user_balances WHERE user_id = ? AND leave_type_id = ? AND year = ?',
                             (user_id, lt['id'], current_year)).fetchone()
            allocated = calculate_prorated_days(lt['default_days'], join_date_str, current_year)
            if not bal:
                db.execute('''
                    INSERT INTO user_balances (user_id, leave_type_id, year, allocated_days, used_days, carried_over_days)
                    VALUES (?, ?, ?, ?, 0, 0)
                ''', (user_id, lt['id'], current_year, allocated))
            else:
                db.execute('''
                    UPDATE user_balances SET allocated_days = ? 
                    WHERE user_id = ? AND leave_type_id = ? AND year = ?
                ''', (allocated, user_id, lt['id'], current_year))
        db.commit()

def check_carried_over_expired(user_id, leave_type_id, current_year):
    with get_db() as db:
        settings_rows = db.execute('SELECT * FROM settings').fetchall()
        settings = {r['key']: r['value'] for r in settings_rows}
        
        exp_m = int(settings.get('carry_forward_expiry_month', 3))
        exp_d = int(settings.get('carry_forward_expiry_day', 31))
        expiry_date = datetime.date(current_year, exp_m, exp_d)
        
        if datetime.date.today() > expiry_date:
            return True
    return False

# --- UI WORKDAY HTML TEMPLATES ---
BASE_HTML = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Workday Time Off Portal</title>
    <link href="https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --wd-navy: #081c3c;
            --wd-blue: #005cb9;
            --wd-blue-hover: #00448a;
            --wd-bg: #f4f6f9;
            --wd-card-bg: #ffffff;
            --wd-border: #ced4da;
            --wd-text: #333333;
            --wd-gray-text: #5f6368;
        }
        body { font-family: 'Roboto', sans-serif; background-color: var(--wd-bg); margin: 0; padding: 0; color: var(--wd-text); }
        .wd-navbar { background-color: var(--wd-navy); color: white; padding: 12px 24px; display: flex; justify-content: space-between; align-items: center; box-shadow: 0 2px 4px rgba(0,0,0,0.15); }
        .wd-logo { font-size: 18px; font-weight: 700; letter-spacing: 0.5px; display: flex; align-items: center; gap: 10px; }
        .wd-logo span { background: var(--wd-blue); color: white; padding: 2px 6px; border-radius: 4px; font-size: 12px; }
        .wd-user-info { font-size: 14px; font-weight: 400; display: flex; align-items: center; gap: 15px; }
        .wd-logout { color: #8ab4f8; text-decoration: none; font-weight: 500; }
        .wd-logout:hover { text-decoration: underline; }
        .container { max-width: 1100px; margin: 30px auto; padding: 0 15px; }
        .page-title { border-bottom: 2px solid #e0e0e0; padding-bottom: 12px; margin-bottom: 25px; display: flex; justify-content: space-between; align-items: flex-end; }
        .page-title h1 { margin: 0; font-size: 24px; font-weight: 500; color: var(--wd-navy); }
        .page-title .subtitle { color: var(--wd-gray-text); font-size: 14px; margin-top: 4px; }
        .cards-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 20px; margin-bottom: 30px; }
        .wd-card { background: var(--wd-card-bg); border-radius: 8px; border: 1px solid #e2e8f0; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); border-top: 4px solid var(--wd-blue); }
        .wd-card h3 { margin: 0 0 8px 0; font-size: 13px; text-transform: uppercase; color: var(--wd-gray-text); letter-spacing: 0.5px; }
        .wd-card .val { font-size: 26px; font-weight: 700; color: var(--wd-navy); }
        .wd-card .sub { font-size: 12px; color: var(--wd-gray-text); margin-top: 5px; }
        .wd-panel { background: white; border-radius: 8px; border: 1px solid #e2e8f0; padding: 25px; margin-bottom: 30px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
        .wd-panel-header { font-size: 16px; font-weight: 500; color: var(--wd-navy); margin-bottom: 20px; border-bottom: 1px solid #f0f0f0; padding-bottom: 10px; }
        .form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; }
        .form-group { margin-bottom: 15px; }
        .form-group.full { grid-column: 1 / -1; }
        label { display: block; margin-bottom: 6px; font-size: 13px; font-weight: 500; color: #4a5568; }
        input, select, textarea { width: 100%; padding: 10px; box-sizing: border-box; border: 1px solid var(--wd-border); border-radius: 4px; font-family: inherit; font-size: 14px; }
        input:focus, select:focus, textarea:focus { outline: none; border-color: var(--wd-blue); box-shadow: 0 0 0 2px rgba(0,92,185,0.2); }
        .btn { background: var(--wd-blue); color: white; border: none; padding: 10px 20px; border-radius: 20px; font-weight: 500; cursor: pointer; font-size: 14px; transition: background 0.2s; text-decoration: none; display: inline-block; }
        .btn:hover { background: var(--wd-blue-hover); }
        .btn-success { background: #2e7d32; }
        .btn-success:hover { background: #1b5e20; }
        .btn-danger { background: #c62828; }
        .btn-danger:hover { background: #b71c1c; }
        .btn-secondary { background: #5f6368; }
        .btn-secondary:hover { background: #3c4043; }
        .btn-sm { padding: 6px 14px; font-size: 12px; }
        table { width: 100%; border-collapse: collapse; font-size: 14px; }
        th { background: #f8fafc; color: var(--wd-navy); text-align: left; padding: 12px; font-weight: 500; border-bottom: 2px solid #edf2f7; }
        td { padding: 12px; border-bottom: 1px solid #edf2f7; color: #2d3748; }
        .wd-badge { padding: 4px 10px; border-radius: 12px; font-size: 12px; font-weight: 500; display: inline-block; }
        .wd-badge-Pending { background: #fef3c7; color: #92400e; }
        .wd-badge-Approved { background: #d1fae5; color: #065f46; }
        .wd-badge-Rejected { background: #fee2e2; color: #991b1b; }
        .alert { padding: 12px 16px; background: #e0f2fe; border-left: 4px solid var(--wd-blue); margin-bottom: 20px; border-radius: 4px; color: #0369a1; font-size: 14px; }
        .actions-cell { display: flex; gap: 8px; }
    </style>
</head>
<body>
    {% if session.get('user_id') %}
    <div class="wd-navbar">
        <div class="wd-logo">workday. <span>Absence</span></div>
        <div class="wd-user-info">
            Logged in as <b>{{ session['name'] }}</b> ({{ session['role']|upper }})
            <a href="/logout" class="wd-logout">Sign Out</a>
        </div>
    </div>
    {% endif %}

    <div class="container">
        {% with messages = get_flashed_messages() %}
          {% if messages %}
            {% for message in messages %}
              <div class="alert">{{ message }}</div>
            {% endfor %}
          {% endif %}
        {% endwith %}

        {% block content %}{% endblock %}
    </div>
</body>
</html>
'''

LOGIN_HTML = BASE_HTML.replace('{% block content %}{% endblock %}', '''
    <div class="wd-panel" style="max-width: 400px; margin: 60px auto;">
        <div style="text-align: center; margin-bottom: 25px;">
            <h2 style="color: var(--wd-navy); margin: 0;">workday.</h2>
            <p style="color: var(--wd-gray-text); font-size: 14px; margin-top: 5px;">Sign in to Time Off Application</p>
        </div>
        <form method="POST" action="/login">
            <div class="form-group">
                <label>Username</label>
                <input type="text" name="username" placeholder="Enter username" required>
            </div>
            <div class="form-group">
                <label>Password</label>
                <input type="password" name="password" required>
            </div>
            <button type="submit" class="btn" style="width: 100%; border-radius: 4px; margin-top: 10px;">Sign In</button>
        </form>
    </div>
''')

EMPLOYEE_HTML = BASE_HTML.replace('{% block content %}{% endblock %}', '''
    <div class="page-title">
        <div>
            <h1>Request Absence</h1>
            <div class="subtitle">Employment Join Date: <b>{{ user.join_date }}</b> (Pro-rated allocations applied)</div>
        </div>
    </div>

    <div class="cards-grid">
        {% for b in balances %}
        <div class="wd-card">
            <h3>{{ b.leave_name }}</h3>
            <div class="val">{{ b.remaining }} <span style="font-size:16px; font-weight:normal;">Days</span></div>
            <div class="sub">Allocated: {{ b.allocated_days }}d | Used: {{ b.used_days }}d {% if b.carried_over_days > 0 %} | Carried: {{ b.carried_over_days }}d{% endif %}</div>
        </div>
        {% endfor %}
    </div>

    <div class="wd-panel">
        <div class="wd-panel-header">Enter Absence Details</div>
        <form method="POST" action="/submit_leave">
            <div class="form-grid">
                <div class="form-group">
                    <label>Absence Type</label>
                    <select name="leave_type_id">
                        {% for lt in leave_types %}
                        <option value="{{ lt.id }}">{{ lt.name }}</option>
                        {% endfor %}
                    </select>
                </div>
                <div class="form-group">
                    <label>From Date</label>
                    <input type="date" name="start_date" required>
                </div>
                <div class="form-group">
                    <label>To Date</label>
                    <input type="date" name="end_date" required>
                </div>
                <div class="form-group full">
                    <label>Comment / Business Reason</label>
                    <textarea name="reason" rows="2" placeholder="Provide details for your manager..."></textarea>
                </div>
            </div>
            <button type="submit" class="btn">Submit Request</button>
        </form>
    </div>

    <div class="wd-panel">
        <div class="wd-panel-header">Absence Request History</div>
        <table>
            <thead>
                <tr>
                    <th>Type</th>
                    <th>Date Range</th>
                    <th>Requested Duration</th>
                    <th>Comment</th>
                    <th>Approval Status</th>
                </tr>
            </thead>
            <tbody>
                {% for req in requests %}
                <tr>
                    <td><b>{{ req.leave_name }}</b></td>
                    <td>{{ req.start_date }} to {{ req.end_date }}</td>
                    <td>{{ req.total_days }} Business Day(s)</td>
                    <td>{{ req.reason if req.reason else 'N/A' }}</td>
                    <td><span class="wd-badge wd-badge-{{ req.status }}">{{ req.status }}</span></td>
                </tr>
                {% else %}
                <tr><td colspan="5" style="color: var(--wd-gray-text);">No past absence requests found.</td></tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
''')

MANAGER_HTML = BASE_HTML.replace('{% block content %}{% endblock %}', '''
    <div class="page-title">
        <div>
            <h1>Manager Inbox & Absence Oversight</h1>
            <div class="subtitle">Review team time-off submissions and monitor employee balances</div>
        </div>
    </div>

    <div class="wd-panel">
        <div class="wd-panel-header">Pending Approval Requests</div>
        <table>
            <thead>
                <tr>
                    <th>Worker Name</th>
                    <th>Absence Type</th>
                    <th>Date Range</th>
                    <th>Duration</th>
                    <th>Reason</th>
                    <th>Action</th>
                </tr>
            </thead>
            <tbody>
                {% for req in pending %}
                <tr>
                    <td><b>{{ req.employee_name }}</b></td>
                    <td>{{ req.leave_name }}</td>
                    <td>{{ req.start_date }} to {{ req.end_date }}</td>
                    <td>{{ req.total_days }} Day(s)</td>
                    <td>{{ req.reason }}</td>
                    <td class="actions-cell">
                        <a href="/action_leave/{{ req.id }}/approve" class="btn btn-sm btn-success">Approve</a>
                        <a href="/action_leave/{{ req.id }}/reject" class="btn btn-sm btn-danger">Reject</a>
                    </td>
                </tr>
                {% else %}
                <tr><td colspan="6" style="color: var(--wd-gray-text);">Your inbox is clear. No pending approvals!</td></tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
''')

ADMIN_HTML = BASE_HTML.replace('{% block content %}{% endblock %}', '''
    <div class="page-title">
        <div>
            <h1>System Administrator Control Panel</h1>
            <div class="subtitle">Configure settings, leave types, rules, and manage user accounts</div>
        </div>
    </div>

    <!-- Rule Settings Panel -->
    <div class="wd-panel">
        <div class="wd-panel-header">Annual Leave Carry-Forward Rules</div>
        <form method="POST" action="/admin/update_settings">
            <div class="form-grid">
                <div class="form-group">
                    <label>Max Carry-Forward Percent (%)</label>
                    <input type="number" name="carry_forward_percent" value="{{ settings.carry_forward_percent }}" required min="0" max="100">
                </div>
                <div class="form-group">
                    <label>Expiry Month (1-12)</label>
                    <input type="number" name="carry_forward_expiry_month" value="{{ settings.carry_forward_expiry_month }}" min="1" max="12" required>
                </div>
                <div class="form-group">
                    <label>Expiry Day (1-31)</label>
                    <input type="number" name="carry_forward_expiry_day" value="{{ settings.carry_forward_expiry_day }}" min="1" max="31" required>
                </div>
            </div>
            <button type="submit" class="btn">Save Rule Settings</button>
        </form>
    </div>

    <!-- Manage Leave Types -->
    <div class="wd-panel">
        <div class="wd-panel-header">Configure Leave Types & Default Allocations</div>
        <form method="POST" action="/admin/add_leave_type" style="margin-bottom: 20px;">
            <div class="form-grid">
                <div class="form-group">
                    <label>Leave Type Name</label>
                    <input type="text" name="name" placeholder="e.g. Parental Leave" required>
                </div>
                <div class="form-group">
                    <label>Annual Default Days (Full Year)</label>
                    <input type="number" name="default_days" placeholder="e.g. 14" required min="1">
                </div>
            </div>
            <button type="submit" class="btn">Add New Leave Type</button>
        </form>
        
        <table>
            <thead>
                <tr>
                    <th>Leave Name</th>
                    <th>Default Days / Year</th>
                    <th>Action</th>
                </tr>
            </thead>
            <tbody>
                {% for lt in leave_types %}
                <tr>
                    <td><b>{{ lt.name }}</b></td>
                    <td>{{ lt.default_days }} Days</td>
                    <td>
                        <a href="/admin/delete_leave_type/{{ lt.id }}" class="btn btn-sm btn-danger" onclick="return confirm('Delete this leave type?');">Remove</a>
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>

    <!-- Create User Account -->
    <div class="wd-panel">
        <div class="wd-panel-header">Register New Manager / Employee Account</div>
        <form method="POST" action="/admin/create_user">
            <div class="form-grid">
                <div class="form-group">
                    <label>Full Name</label>
                    <input type="text" name="name" placeholder="e.g. John Smith" required>
                </div>
                <div class="form-group">
                    <label>Username</label>
                    <input type="text" name="username" placeholder="e.g. jsmith" required>
                </div>
                <div class="form-group">
                    <label>Password</label>
                    <input type="password" name="password" required>
                </div>
                <div class="form-group">
                    <label>System Role</label>
                    <select name="role">
                        <option value="employee">Employee</option>
                        <option value="manager">Manager</option>
                        <option value="admin">Admin</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>Employment Start Date (For Pro-rating)</label>
                    <input type="date" name="join_date" required>
                </div>
            </div>
            <button type="submit" class="btn btn-success">Create User Account</button>
        </form>
    </div>

    <!-- User Roster -->
    <div class="wd-panel">
        <div class="wd-panel-header">System User Roster & Account Management</div>
        <table>
            <thead>
                <tr>
                    <th>Name</th>
                    <th>Username</th>
                    <th>Role</th>
                    <th>Start Date</th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody>
                {% for u in users %}
                <tr>
                    <td><b>{{ u.name }}</b></td>
                    <td>{{ u.username }}</td>
                    <td><span class="wd-badge wd-badge-Approved">{{ u.role|upper }}</span></td>
                    <td>{{ u.join_date }}</td>
                    <td class="actions-cell">
                        <a href="/admin/edit_user/{{ u.id }}" class="btn btn-sm btn-secondary">Edit</a>
                        {% if u.id != session['user_id'] %}
                        <a href="/admin/delete_user/{{ u.id }}" class="btn btn-sm btn-danger" onclick="return confirm('Are you sure you want to delete this account?');">Delete</a>
                        {% endif %}
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
''')

ADMIN_EDIT_USER_HTML = BASE_HTML.replace('{% block content %}{% endblock %}', '''
    <div class="page-title">
        <div>
            <h1>Edit Account: {{ target_user.name }}</h1>
            <div class="subtitle">Modify account details, system roles, start dates, or reset password</div>
        </div>
        <a href="/" class="btn btn-secondary">Back to Admin Panel</a>
    </div>

    <div class="wd-panel">
        <div class="wd-panel-header">Update User Information</div>
        <form method="POST" action="/admin/edit_user/{{ target_user.id }}">
            <div class="form-grid">
                <div class="form-group">
                    <label>Full Name</label>
                    <input type="text" name="name" value="{{ target_user.name }}" required>
                </div>
                <div class="form-group">
                    <label>Username</label>
                    <input type="text" name="username" value="{{ target_user.username }}" required>
                </div>
                <div class="form-group">
                    <label>New Password (Leave blank to keep current password)</label>
                    <input type="password" name="password" placeholder="••••••••">
                </div>
                <div class="form-group">
                    <label>System Role</label>
                    <select name="role">
                        <option value="employee" {% if target_user.role == 'employee' %}selected{% endif %}>Employee</option>
                        <option value="manager" {% if target_user.role == 'manager' %}selected{% endif %}>Manager</option>
                        <option value="admin" {% if target_user.role == 'admin' %}selected{% endif %}>Admin</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>Employment Start Date</label>
                    <input type="date" name="join_date" value="{{ target_user.join_date }}" required>
                </div>
            </div>
            <button type="submit" class="btn btn-success">Save Changes</button>
            <a href="/" class="btn btn-secondary" style="margin-left: 10px;">Cancel</a>
        </form>
    </div>
''')

# --- ROUTES ---
@app.route('/')
@login_required
def index():
    if session['role'] == 'admin':
        with get_db() as db:
            leave_types = db.execute('SELECT * FROM leave_types').fetchall()
            users = db.execute('SELECT * FROM users ORDER BY id DESC').fetchall()
            settings_rows = db.execute('SELECT * FROM settings').fetchall()
            settings = {r['key']: r['value'] for r in settings_rows}
        return render_template_string(ADMIN_HTML, leave_types=leave_types, users=users, settings=settings)

    elif session['role'] == 'manager':
        with get_db() as db:
            pending = db.execute('''
                SELECT l.*, u.name as employee_name, lt.name as leave_name 
                FROM leave_requests l 
                JOIN users u ON l.user_id = u.id 
                JOIN leave_types lt ON l.leave_type_id = lt.id
                WHERE l.status = 'Pending'
            ''').fetchall()
        return render_template_string(MANAGER_HTML, pending=pending)

    else: # Employee
        current_year = datetime.date.today().year
        sync_user_balances(session['user_id'], session['join_date'])
        
        with get_db() as db:
            user = db.execute("SELECT * FROM users WHERE id = ?", (session['user_id'],)).fetchone()
            leave_types = db.execute("SELECT * FROM leave_types").fetchall()
            raw_balances = db.execute('''
                SELECT b.*, lt.name as leave_name 
                FROM user_balances b 
                JOIN leave_types lt ON b.leave_type_id = lt.id 
                WHERE b.user_id = ? AND b.year = ?
            ''', (session['user_id'], current_year)).fetchall()

            balances = []
            for b in raw_balances:
                carried = b['carried_over_days']
                if check_carried_over_expired(session['user_id'], b['leave_type_id'], current_year):
                    carried = 0.0
                
                rem = (b['allocated_days'] + carried) - b['used_days']
                balances.append({
                    'leave_name': b['leave_name'],
                    'allocated_days': b['allocated_days'],
                    'used_days': b['used_days'],
                    'carried_over_days': carried,
                    'remaining': max(0.0, rem)
                })

            requests = db.execute('''
                SELECT l.*, lt.name as leave_name 
                FROM leave_requests l 
                JOIN leave_types lt ON l.leave_type_id = lt.id 
                WHERE l.user_id = ? ORDER BY l.id DESC
            ''', (session['user_id'],)).fetchall()

        return render_template_string(EMPLOYEE_HTML, user=user, balances=balances, leave_types=leave_types, requests=requests)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        with get_db() as db:
            user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        
        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['name'] = user['name']
            session['role'] = user['role']
            session['join_date'] = user['join_date']
            return redirect(url_for('index'))
        
        flash('Invalid credentials!')
    return render_template_string(LOGIN_HTML)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/submit_leave', methods=['POST'])
@login_required
def submit_leave():
    leave_type_id = request.form['leave_type_id']
    start_date = request.form['start_date']
    end_date = request.form['end_date']
    reason = request.form['reason']

    days = calculate_days(start_date, end_date)
    if days <= 0:
        flash('End date must be on or after start date.')
        return redirect(url_for('index'))

    with get_db() as db:
        db.execute('''
            INSERT INTO leave_requests (user_id, leave_type_id, start_date, end_date, total_days, reason)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (session['user_id'], leave_type_id, start_date, end_date, days, reason))
        db.commit()

    flash('Time off request submitted successfully to your manager.')
    return redirect(url_for('index'))

@app.route('/action_leave/<int:req_id>/<string:action>')
@login_required
def action_leave(req_id, action):
    if session['role'] not in ['manager', 'admin']:
        return "Unauthorized", 403

    status = 'Approved' if action == 'approve' else 'Rejected'
    current_year = datetime.date.today().year

    with get_db() as db:
        req = db.execute("SELECT * FROM leave_requests WHERE id = ?", (req_id,)).fetchone()
        if req and req['status'] == 'Pending':
            db.execute("UPDATE leave_requests SET status = ? WHERE id = ?", (status, req_id))
            
            if status == 'Approved':
                db.execute('''
                    UPDATE user_balances 
                    SET used_days = used_days + ? 
                    WHERE user_id = ? AND leave_type_id = ? AND year = ?
                ''', (req['total_days'], req['user_id'], req['leave_type_id'], current_year))
            db.commit()

    flash(f"Request marked as {status}.")
    return redirect(url_for('index'))

# --- ADMIN ACCOUNT MANAGEMENT ROUTES ---
@app.route('/admin/create_user', methods=['POST'])
@login_required
def admin_create_user():
    if session['role'] != 'admin':
        return "Unauthorized", 403

    name = request.form['name']
    username = request.form['username']
    password = request.form['password']
    role = request.form['role']
    join_date = request.form['join_date']

    with get_db() as db:
        try:
            db.execute('INSERT INTO users (username, password, name, role, join_date) VALUES (?, ?, ?, ?, ?)',
                       (username, generate_password_hash(password, method='pbkdf2:sha256'), name, role, join_date))
            db.commit()
            flash(f"Account for {name} ({role}) created successfully!")
        except sqlite3.IntegrityError:
            flash("Username already exists!")

    return redirect(url_for('index'))

@app.route('/admin/edit_user/<int:user_id>', methods=['GET', 'POST'])
@login_required
def admin_edit_user(user_id):
    if session['role'] != 'admin':
        return "Unauthorized", 403

    with get_db() as db:
        target_user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not target_user:
            flash("User not found.")
            return redirect(url_for('index'))

        if request.method == 'POST':
            name = request.form['name']
            username = request.form['username']
            password = request.form['password']
            role = request.form['role']
            join_date = request.form['join_date']

            try:
                if password.strip():
                    hashed_pw = generate_password_hash(password, method='pbkdf2:sha256')
                    db.execute('''
                        UPDATE users 
                        SET name = ?, username = ?, password = ?, role = ?, join_date = ? 
                        WHERE id = ?
                    ''', (name, username, hashed_pw, role, join_date, user_id))
                else:
                    db.execute('''
                        UPDATE users 
                        SET name = ?, username = ?, role = ?, join_date = ? 
                        WHERE id = ?
                    ''', (name, username, role, join_date, user_id))
                
                db.commit()
                sync_user_balances(user_id, join_date)
                flash(f"Account details for {name} updated successfully.")
                return redirect(url_for('index'))

            except sqlite3.IntegrityError:
                flash("Error: That username is already in use by another account.")

    return render_template_string(ADMIN_EDIT_USER_HTML, target_user=target_user)

@app.route('/admin/delete_user/<int:user_id>')
@login_required
def admin_delete_user(user_id):
    if session['role'] != 'admin':
        return "Unauthorized", 403

    if user_id == session['user_id']:
        flash("You cannot delete your own active Admin account!")
        return redirect(url_for('index'))

    with get_db() as db:
        db.execute('DELETE FROM users WHERE id = ?', (user_id,))
        db.execute('DELETE FROM user_balances WHERE user_id = ?', (user_id,))
        db.execute('DELETE FROM leave_requests WHERE user_id = ?', (user_id,))
        db.commit()

    flash("Account and associated data deleted successfully.")
    return redirect(url_for('index'))

@app.route('/admin/add_leave_type', methods=['POST'])
@login_required
def admin_add_leave_type():
    if session['role'] != 'admin':
        return "Unauthorized", 403

    name = request.form['name']
    default_days = int(request.form['default_days'])

    with get_db() as db:
        try:
            db.execute('INSERT INTO leave_types (name, default_days) VALUES (?, ?)', (name, default_days))
            db.commit()
            flash(f"Added leave type '{name}' with {default_days} default days.")
        except sqlite3.IntegrityError:
            flash("Leave type already exists!")

    return redirect(url_for('index'))

@app.route('/admin/delete_leave_type/<int:lt_id>')
@login_required
def admin_delete_leave_type(lt_id):
    if session['role'] != 'admin':
        return "Unauthorized", 403

    with get_db() as db:
        db.execute('DELETE FROM leave_types WHERE id = ?', (lt_id,))
        db.execute('DELETE FROM user_balances WHERE leave_type_id = ?', (lt_id,))
        db.commit()

    flash("Leave type deleted.")
    return redirect(url_for('index'))

@app.route('/admin/update_settings', methods=['POST'])
@login_required
def admin_update_settings():
    if session['role'] != 'admin':
        return "Unauthorized", 403

    with get_db() as db:
        db.execute('REPLACE INTO settings (key, value) VALUES (?, ?)', ('carry_forward_percent', request.form['carry_forward_percent']))
        db.execute('REPLACE INTO settings (key, value) VALUES (?, ?)', ('carry_forward_expiry_month', request.form['carry_forward_expiry_month']))
        db.execute('REPLACE INTO settings (key, value) VALUES (?, ?)', ('carry_forward_expiry_day', request.form['carry_forward_expiry_day']))
        db.commit()

    flash("Rule settings updated successfully.")
    return redirect(url_for('index'))

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5022, debug=True)