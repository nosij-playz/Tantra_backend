from flask import Flask, render_template, request, redirect, url_for, send_file, Response, jsonify
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime
import json
import re
import io
import csv
from typing import List, Dict
import os
import glob
from werkzeug.utils import secure_filename
import openpyxl

# Reference to openpyxl to keep static analyzers quiet (used when exporting xlsx via pandas/openpyxl)
_ = getattr(openpyxl, '__version__', None)

app = Flask(__name__)

# Base link to use when constructing absolute URLs for saved DB links.
# Change this to your deployment base URL when you deploy (e.g. https://example.com)
curr_link = os.environ.get('CURR_LINK', "https://tantra-vl7d.onrender.com/")

def make_static_url(filename: str) -> str:
    """Return an absolute URL for a file in the static folder using curr_link as base.

    Example: make_static_url('logos/foo.png') -> 'http://127.0.0.1:5000/static/logos/foo.png'
    """
    # use url_for to get the path portion, then prefix with curr_link
    path = url_for('static', filename=filename, _external=False)
    return curr_link.rstrip('/') + path

# -------------------- Upload folders --------------------
UPLOAD_QR_FOLDER = 'static/qr'
UPLOAD_EVENT_FOLDER = 'static/event_images'
UPLOAD_LOGO_FOLDER = 'static/logos'

os.makedirs(UPLOAD_QR_FOLDER, exist_ok=True)
os.makedirs(UPLOAD_EVENT_FOLDER, exist_ok=True)
os.makedirs(UPLOAD_LOGO_FOLDER, exist_ok=True)

app.config['UPLOAD_QR_FOLDER'] = UPLOAD_QR_FOLDER
app.config['UPLOAD_EVENT_FOLDER'] = UPLOAD_EVENT_FOLDER
app.config['UPLOAD_LOGO_FOLDER'] = UPLOAD_LOGO_FOLDER

# -------------------- Firebase --------------------
# Initialize Firebase using one of the following (in order of preference):
# 1) FIREBASE_SERVICE_ACCOUNT_JSON environment variable containing the
#    service account JSON string
# 2) FIREBASE_CREDENTIALS_FILE environment variable pointing to a file path
# 3) A file checked into the repo (not recommended)

_sa_json = os.environ.get('FIREBASE_SERVICE_ACCOUNT_JSON')
_sa_file = os.environ.get('FIREBASE_CREDENTIALS_FILE', 'techfestadmin-a2e2c-firebase-adminsdk-fbsvc-a2a3aaa0e7.json')

if _sa_json:
    try:
        sa_info = json.loads(_sa_json) if isinstance(_sa_json, str) else _sa_json
        cred = credentials.Certificate(sa_info)
    except Exception as e:
        raise RuntimeError('Failed to parse FIREBASE_SERVICE_ACCOUNT_JSON: ' + str(e))
elif os.path.exists(_sa_file):
    cred = credentials.Certificate(_sa_file)
else:
    # Try to auto-detect a service account file matching the typical filename pattern.
    candidates = glob.glob(os.path.join(os.getcwd(), 'techfestadmin*.json'))
    if candidates:
        # pick the first candidate and proceed
        picked = candidates[0]
        print(f"Using detected Firebase service account file: {picked}")
        cred = credentials.Certificate(picked)
    else:
        raise RuntimeError('Firebase service account not found. Set FIREBASE_SERVICE_ACCOUNT_JSON or FIREBASE_CREDENTIALS_FILE.')

firebase_admin.initialize_app(cred)
db = firestore.client()

# -------------------- Home --------------------
@app.route('/')
def index():
    # Dashboard summary counts
    # Count departments
    depts = list(db.collection('departments').stream())
    total_departments = len(depts)
    # build department name map to avoid repeated lookups
    dept_map = {d.id: d.to_dict().get('name', '') for d in depts}

    # Count events
    events = list(db.collection('events').stream())
    total_events = len(events)

    # Count registrations/participants and unique participants
    # Some deployments use a 'registrations' collection; others (your setup) use 'participants'.
    parts = list(db.collection('participants').stream())
    total_registrations = len(parts)
    unique_participants = set()
    for pdoc in parts:
        p = pdoc.to_dict()
        # prefer email as unique id, fallback to phone or doc id
        ident = (p.get('email') or p.get('phone') or pdoc.id)
        if ident:
            unique_participants.add(str(ident).strip().lower())
    total_unique_participants = len(unique_participants)

    # Build a small recent events list for the dashboard
    recent_events = []
    for e in events:
        ed = e.to_dict()
        did = ed.get('department')
        recent_events.append({
            'id': e.id,
            'name': ed.get('name'),
            'dept_id': did,
            # show the department name when known, otherwise show the raw dept id
            'dept_name': dept_map.get(did, (did or '')),
            'date': ed.get('date'),
            'status': ed.get('status', 1)
        })

    # prepare a simple departments list for the dashboard (id, name, logo_url)
    dept_list = [(d.id, d.to_dict().get('name', ''), d.to_dict().get('logo_url', '')) for d in depts]

    return render_template('index.html',
                           total_departments=total_departments,
                           total_events=total_events,
                           total_registrations=total_registrations,
                           total_unique_participants=total_unique_participants,
                           recent_events=recent_events,
                           departments=dept_list)


@app.route('/dept_events/<dept_id>', methods=['GET'])
def dept_events(dept_id):
    """Return events for a department as JSON."""
    if not dept_id:
        return jsonify({'events': []})
    try:
        ev_q = db.collection('events').where('department', '==', dept_id).stream()
    except Exception:
        # Fallback: return empty
        return jsonify({'events': []})
    evs = []
    for e in ev_q:
        ed = e.to_dict()
        evs.append({
            'id': e.id,
            'name': ed.get('name'),
            'date': ed.get('date'),
            'status': ed.get('status', 1),
            'image_url': ed.get('image_url', ''),
            'venue': ed.get('venue', ''),
            'department': ed.get('department', '')
        })
    return jsonify({'events': evs, 'dept_id': dept_id})


@app.route('/event/<event_id>', methods=['GET'])
def get_event(event_id):
    """Return a single event's details as JSON."""
    if not event_id:
        return jsonify({'error': 'missing id'}), 400
    ev_doc = db.collection('events').document(event_id).get()
    if not ev_doc.exists:
        return jsonify({'error': 'not found'}), 404
    ed = ev_doc.to_dict()
    result = {
        'id': ev_doc.id,
        'name': ed.get('name'),
        'description': ed.get('description', ''),
        'date': ed.get('date', ''),
        'time': ed.get('time', ''),
        'venue': ed.get('venue', ''),
        'image_url': ed.get('image_url', ''),
        'status': ed.get('status', 1),
        'department': ed.get('department', ''),
        'price': ed.get('price', ''),
        'prize': ed.get('prize', '')
    }
    return jsonify({'event': result})

# -------------------- Add Department --------------------
@app.route('/add_department', methods=['GET', 'POST'])
def add_department():
    if request.method == 'POST':
        name = request.form['name']
        description = request.form['description']

        # Upload logo
        logo_file = request.files.get('logo_file')
        logo_url = ""
        if logo_file and logo_file.filename != "":
            filename = secure_filename(logo_file.filename)
            path = os.path.join(app.config['UPLOAD_LOGO_FOLDER'], filename)
            logo_file.save(path)
            logo_url = make_static_url(f'logos/{filename}')

        # Upload QR
        qr_file = request.files.get('qr_file')
        qr_url = ""
        if qr_file and qr_file.filename != "":
            filename = secure_filename(qr_file.filename)
            path = os.path.join(app.config['UPLOAD_QR_FOLDER'], filename)
            qr_file.save(path)
            qr_url = make_static_url(f'qr/{filename}')

        db.collection('departments').document().set({
            'name': name,
            'description': description,
            'logo_url': logo_url,
            'qr_url': qr_url,
            'created_at': datetime.utcnow()
        })
        return redirect(url_for('index'))
    # show existing departments on the page for quick reference
    departments = list(db.collection('departments').stream())
    dept_list = [(d.id, d.to_dict().get('name', ''), d.to_dict().get('description', '')) for d in departments]
    return render_template('add_department.html', departments=dept_list)

# -------------------- Add Event --------------------
@app.route('/add_event', methods=['GET', 'POST'])
def add_event():
    departments = db.collection('departments').stream()
    dept_list = [(dept.id, dept.to_dict()['name']) for dept in departments]

    if request.method == 'POST':
        dept_id = request.form['dept_id']
        name = request.form['name']
        description = request.form['description']
        date = request.form['date']
        time = request.form['time']
        venue = request.form['venue']

        # Event image upload
        event_file = request.files.get('event_image')
        image_url = ""
        if event_file and event_file.filename != "":
            filename = secure_filename(event_file.filename)
            path = os.path.join(app.config['UPLOAD_EVENT_FOLDER'], filename)
            event_file.save(path)
            image_url = make_static_url(f'event_images/{filename}')

        # Get department QR and store event in top-level `events` collection
        dept_doc = db.collection('departments').document(dept_id).get()
        payment_qr_url = ''
        if dept_doc.exists:
            payment_qr_url = dept_doc.to_dict().get('qr_url', '')

        # status: 1=open, 0=closed
        status = int(request.form.get('status', '1'))
        price = request.form.get('price', '')
        prize = request.form.get('prize', '')

        # Find the highest numeric event id in the collection
        all_events = db.collection('events').stream()
        max_id = 0
        for ev in all_events:
            try:
                eid = int(ev.id)
                if eid > max_id:
                    max_id = eid
            except Exception:
                continue
        new_id = max_id + 1
        event_ref = db.collection('events').document(str(new_id))
        event_ref.set({
            'id': new_id,
            'department': dept_id,
            'name': name,
            'description': description,
            'date': date,
            'time': time,
            'venue': venue,
            'image_url': image_url,
            'payment_qr_url': payment_qr_url,
            'price': price,
            'prize': prize,
            'status': status,
            'created_at': datetime.utcnow()
        })
        return redirect(url_for('index'))
    return render_template('add_event.html', departments=dept_list)


@app.route('/toggle_event_status', methods=['POST'])
def toggle_event_status():
    event_id = request.form.get('event_id')
    if not event_id:
        return redirect(url_for('index'))
    ev_doc = db.collection('events').document(event_id).get()
    if not ev_doc.exists:
        return redirect(url_for('index'))
    ev = ev_doc.to_dict()
    current = ev.get('status', 1)
    new_status = 0 if current == 1 else 1
    db.collection('events').document(event_id).update({'status': new_status})
    return redirect(url_for('index'))


def _resolve_participant_from_registration(reg_data: dict) -> Dict:
    """Given a registration document dict, attempt to resolve participant data.
    Handles keys: participant_id, user_id, 'participant' inline dict, participant_email, email.
    Returns participant dict or None if not found.
    """
    if not reg_data or not isinstance(reg_data, dict):
        return None

    # Inline participant object
    inline = reg_data.get('participant')
    if inline and isinstance(inline, dict):
        return inline

    # Common id fields
    pid = reg_data.get('participant_id') or reg_data.get('user_id') or reg_data.get('uid')
    if pid:
        # Try participants collection first
        ref = db.collection('participants').document(pid).get()
        if ref.exists:
            return ref.to_dict()
        # fallback to users collection
        ref2 = db.collection('users').document(pid).get()
        if ref2.exists:
            return ref2.to_dict()

    # Try to resolve by email if provided in registration
    email = reg_data.get('participant_email') or reg_data.get('email') or reg_data.get('user_email')
    if email:
        # search participants by email
        q = db.collection('participants').where('email', '==', email).limit(1).stream()
        for doc in q:
            return doc.to_dict()
        q2 = db.collection('users').where('email', '==', email).limit(1).stream()
        for doc in q2:
            return doc.to_dict()
    return None

# -------------------- View Participants --------------------
@app.route('/view_participants', methods=['GET'])
def view_participants():
    departments = db.collection('departments').stream()
    dept_list = [(dept.id, dept.to_dict()['name']) for dept in departments]
    # build quick lookup map for department id -> name
    dept_map = {d[0]: d[1] for d in dept_list}

    selected_dept_id = request.args.get('dept_id')
    # template uses 'event_id' (which contains the event name in this dataset)
    selected_event_id = request.args.get('event_id')
    # If an event is selected from the dropdown, enable sorting by event
    sort_by_event = bool(selected_event_id)
    participants_info: List[Dict] = []

    # events_for_select: used to populate events dropdown. Build after we know selected_dept_id

    # Primary source: participants collection (no registrations in your setup)
    parts = db.collection('participants').stream()

    # If a department is selected via dept_id (which is a department document id), translate to department name
    selected_dept_name = None
    if selected_dept_id:
        ddoc = db.collection('departments').document(selected_dept_id).get()
        if ddoc.exists:
            selected_dept_name = ddoc.to_dict().get('name')

    # selected_event_id is taken directly from query string (if any)

    for pdoc in parts:
        p = pdoc.to_dict()
        p_dept = p.get('department') or ''
        p_event = p.get('event') or ''
        # apply filters if provided
        if selected_dept_name and p_dept != selected_dept_name:
            continue
        if selected_event_id and p_event != selected_event_id:
            continue

        participants_info.append({
            'name': p.get('name'),
            'email': p.get('email'),
            'phone': p.get('phone'),
            'college': p.get('college'),
            'branch': p.get('branch/Class'),
            'year': p.get('year'),
            'event_name': p_event,
            'dept_name': p_dept,
            'event_id': '',
            'transaction_id': p.get('transactionId') or p.get('transaction_id')
        })

    # Sort: department name first, then optional event name, then participant name
    if sort_by_event:
        participants_info = sorted(participants_info, key=lambda r: (r.get('dept_name', ''), r.get('event_name', ''), r.get('name', '')))
    else:
        participants_info = sorted(participants_info, key=lambda r: (r.get('dept_name', ''), r.get('name', '')))

    # Build events_for_select now (limit to selected department if provided)
    if selected_dept_id:
        ev_q = db.collection('events').where('department', '==', selected_dept_id).stream()
    else:
        ev_q = db.collection('events').stream()
    events_for_select = [(e.to_dict().get('name'), e.to_dict().get('name')) for e in ev_q]

    return render_template('view_participants.html',
                           departments=dept_list,
                           participants=participants_info,
                           selected_dept_id=selected_dept_id,
                           selected_event_id=selected_event_id,
                           events_for_select=events_for_select)


def _gather_participants(dept_id: str, event_id: str = None) -> List[Dict]:
    """Helper to collect participants for a department, optionally filtering by event id."""
    rows: List[Dict] = []
    if not dept_id:
        return rows

    # Build list of event ids for the department (or single event id if provided)
    event_ids = []
    event_map = {}
    if event_id:
        ev_doc = db.collection('events').document(event_id).get()
        if ev_doc.exists:
            event_ids = [ev_doc.id]
            event_map[ev_doc.id] = ev_doc.to_dict()
    else:
        events = db.collection('events').where('department', '==', dept_id).stream()
        for e in events:
            event_ids.append(e.id)
            event_map[e.id] = e.to_dict()

    if not event_ids:
        return rows

    # Firestore 'in' queries accept up to 10 items; batch if necessary
    BATCH = 10
    for i in range(0, len(event_ids), BATCH):
        batch_ids = event_ids[i:i+BATCH]
        regs_query = db.collection('registrations').where('event_id', 'in', batch_ids).stream()
        for reg in regs_query:
            reg_data = reg.to_dict()
            p = _resolve_participant_from_registration(reg_data)
            if not p:
                continue
            ev_id = reg_data.get('event_id')
            ev_data = event_map.get(ev_id, {})
            rows.append({
                'name': p.get('name'),
                'email': p.get('email'),
                'phone': p.get('phone'),
                'college': p.get('college'),
                'branch': p.get('branch'),
                'year': p.get('year'),
                'event_name': ev_data.get('name'),
                'dept_name': ev_data.get('department') or ev_data.get('dept_id') or '',
                'event_id': ev_id,
                'transaction_id': reg_data.get('transaction_id')
            })

    return rows


@app.route('/export_participants')
def export_participants():
    """Export participants for a department (and optional event) in csv/xlsx/pdf formats.

    Query params: dept_id, event_id (optional), format (csv|xlsx|pdf)
    """
    dept_id = request.args.get('dept_id')
    event_id = request.args.get('event_id')
    fmt = request.args.get('format', 'xlsx').lower()

    # Helper to sanitize filename parts
    def _sanitize(s: str) -> str:
        if not s:
            return ''
        s = s.lower()
        # replace non-alphanumeric with underscore
        s = re.sub(r'[^a-z0-9]+', '_', s)
        s = s.strip('_')
        return s or 'value'

    # Determine dept_name (participants store department by name in this dataset)
    dept_name = None
    if dept_id:
        d = db.collection('departments').document(dept_id).get()
        if d.exists:
            dept_name = d.to_dict().get('name')

    # Determine event_name: try doc id first, else treat as event name string
    event_name = None
    if event_id:
        # try as doc id
        evdoc = db.collection('events').document(event_id).get()
        if evdoc.exists:
            event_name = evdoc.to_dict().get('name')
        else:
            # assume event_id is a name string
            event_name = event_id

    # Build participant rows directly from participants collection (matches view)
    q = db.collection('participants')
    if dept_name:
        q = q.where('department', '==', dept_name)
    if event_name:
        q = q.where('event', '==', event_name)
    rows = []
    for doc in q.stream():
        p = doc.to_dict()
        rows.append({
            'name': p.get('name', ''),
            'email': p.get('email', ''),
            'phone': p.get('phone', ''),
            'college': p.get('college', ''),
            'branch': p.get('branch', ''),
            'year': p.get('year', ''),
            'event_name': p.get('event', ''),
            'dept_name': p.get('department', ''),
            'transaction_id': p.get('transactionId') or p.get('transaction_id', '')
        })

    # Sort
    rows = sorted(rows, key=lambda r: (r.get('dept_name', ''), r.get('event_name', ''), r.get('name', '')))

    headers = ['name', 'email', 'phone', 'college', 'branch', 'year', 'event_name', 'dept_name', 'transaction_id']

    # Build filename pattern: tantra_{department}_{event}.{ext}
    part_dept = _sanitize(dept_name) if dept_name else 'all_departments'
    part_event = _sanitize(event_name) if event_name else 'all_events'
    base_filename = f'tantra_{part_dept}_{part_event}'

    if fmt == 'xlsx':
        try:
            import pandas as pd
        except Exception:
            return Response('pandas is required to export XLSX. Install with `pip install pandas openpyxl`', status=500)
        df = pd.DataFrame(rows)
        for h in headers:
            if h not in df.columns:
                df[h] = ''
        buf = io.BytesIO()
        df.to_excel(buf, index=False)
        buf.seek(0)
        filename = f'{base_filename}.xlsx'
        return send_file(buf, as_attachment=True, download_name=filename, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

    if fmt == 'pdf':
        try:
            from reportlab.lib.pagesizes import A4, landscape
            from reportlab.lib import colors
            from reportlab.platypus import SimpleDocTemplate, Table, TableStyle
        except Exception:
            return Response('reportlab is required to export PDF. Install with `pip install reportlab`', status=500)
        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=landscape(A4))
        data = [headers]
        for r in rows:
            data.append([r.get(h, '') for h in headers])
        table = Table(data)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#7c4dff')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ]))
        doc.build([table])
        buf.seek(0)
        filename = f'{base_filename}.pdf'
        return send_file(buf, as_attachment=True, download_name=filename, mimetype='application/pdf')

    return Response('Unsupported format. Allowed: xlsx, pdf', status=400)

# -------------------- View Database Content --------------------
@app.route('/db_content')
def db_content():
    departments = db.collection('departments').stream()
    all_data = []
    for dept in departments:
        dept_data = dept.to_dict()
        # load events from top-level collection that belong to this department
        events = db.collection('events').where('dept_id', '==', dept.id).stream()
        event_list = []
        for e in events:
            ev = e.to_dict()
            ev['_id'] = e.id
            event_list.append(ev)
        all_data.append({
            'dept_id': dept.id,
            'dept_name': dept_data.get('name'),
            'description': dept_data.get('description'),
            'logo_url': dept_data.get('logo_url'),
            'qr_url': dept_data.get('qr_url'),
            'events': event_list
        })
    return render_template('db_content.html', data=all_data)


@app.route('/fix_events', methods=['GET', 'POST'])
def fix_events():
    # list departments for selection
    departments = list(db.collection('departments').stream())
    dept_list = [(d.id, d.to_dict().get('name')) for d in departments]

    message = ''
    if request.method == 'POST':
        event_id = request.form.get('event_id')
        new_dept = request.form.get('dept_id')
        if event_id and new_dept:
            db.collection('events').document(event_id).update({'dept_id': new_dept})
            message = 'Updated event department.'

    # find events with missing/unknown dept_id
    events = list(db.collection('events').stream())
    dept_ids = {d.id for d in departments}
    problematic = []
    for e in events:
        ed = e.to_dict()
        did = ed.get('dept_id')
        if not did or did not in dept_ids:
            problematic.append({'id': e.id, 'name': ed.get('name'), 'date': ed.get('date'), 'dept_id': did})

    return render_template('fix_events.html', events=problematic, departments=dept_list, message=message)

# -------------------- Run --------------------
if __name__ == '__main__':
    # When running locally, allow PORT to be overridden (Render provides $PORT).
    port = int(os.environ.get('PORT', 5000))
    # Bind to 0.0.0.0 so Render (or other hosts) can reach the service.
    app.run(host='0.0.0.0', port=port, debug=True)

