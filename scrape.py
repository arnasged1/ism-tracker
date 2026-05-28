import requests, json, re, datetime, uuid, os, smtplib, sys
from bs4 import BeautifulSoup
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

BASE_URL   = 'https://shipping.konsult.fo'
USERNAME   = os.environ.get('KONSULT_USER', '')
PASSWORD   = os.environ.get('KONSULT_PASS', '')
EMAIL_FROM = os.environ.get('EMAIL_FROM', '')
EMAIL_PASS = os.environ.get('EMAIL_PASS', '')
EMAIL_TO   = os.environ.get('EMAIL_TO', '')
TRACKER_URL = os.environ.get('TRACKER_URL', 'https://ism-tracker.vercel.app/#dashboard')

# Always resolve paths relative to this script file
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(SCRIPT_DIR, 'index.html')
PREV_JSON  = os.path.join(SCRIPT_DIR, 'previous_findings.json')

VESSELS = [
    ('Billefjord',     '/index.php/billefjord-report-a-notification'),
    ('Samson',         '/index.php/samson-report-a-notification'),
    ('Herkules',       '/index.php/herkules-report-a-notification-2'),
    ('Argus',          '/index.php/argus-report-a-notification'),
    ('Grettir Sterki', '/index.php/grettir-sterki-report-a-notification'),
    ('Selvik Ice',     '/index.php/selvik-ice-report-a-notification'),
]

GROUP_MAP = {
    'Non Conformance': 'NC',
    'Near Miss/Incident': 'NM',
    'Observation': 'OBS',
    'Suggestion for improvement': 'OBS',
    'Choose:': 'NC',
    'Compaint': 'NC',
}

def parse_date(s):
    s = s.strip()
    try:
        return datetime.datetime.strptime(s, '%d/%m/%Y').strftime('%Y-%m-%d')
    except:
        return ''

# ── Login ────────────────────────────────────────────────────────────────────
def _is_logged_in(text):
    """Check if the response HTML indicates an active session."""
    low = text.lower()
    return ('log out' in low or 'logout' in low or
            'task=user.logout' in low or
            USERNAME.lower() in low)

def _extract_csrf(soup):
    """Return {token_name: '1'} for all 32-char hex hidden inputs (Joomla CSRF tokens)."""
    tokens = {}
    for inp in soup.find_all('input', type='hidden'):
        name = inp.get('name', '')
        if re.fullmatch(r'[0-9a-f]{32}', name):
            tokens[name] = '1'
    return tokens

def login():
    session = requests.Session()
    session.headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'

    # Try two candidate pages to find the login form
    candidate_urls = [
        BASE_URL + '/index.php?option=com_users&view=login',
        BASE_URL + '/index.php',
    ]

    form = None
    soup_page = None
    for url in candidate_urls:
        print(f'  Fetching {url}')
        r = session.get(url, timeout=30)
        soup_page = BeautifulSoup(r.text, 'html.parser')
        # Look for a form that has a username-style input
        for f in soup_page.find_all('form'):
            user_inp = (f.find('input', {'name': 'username'}) or
                        f.find('input', {'name': 'user'}) or
                        f.find('input', {'type': 'text'}))
            pass_inp = (f.find('input', {'name': 'passwd'}) or
                        f.find('input', {'name': 'password'}) or
                        f.find('input', {'type': 'password'}))
            if user_inp and pass_inp:
                form = f
                print(f'  Login form found on {url}')
                break
        if form:
            break

    if not form:
        print('  No login form found on any candidate page — will attempt blind POST')

    # Build POST data
    data = {}
    action = BASE_URL + '/index.php'

    if form:
        # Capture all hidden inputs (includes Joomla's dynamic CSRF token)
        for inp in form.find_all('input'):
            name = inp.get('name', '')
            if name and inp.get('type') == 'hidden':
                data[name] = inp.get('value', '')
        raw_action = form.get('action', '')
        if raw_action:
            action = raw_action if raw_action.startswith('http') else BASE_URL + raw_action
    elif soup_page:
        # No form found — at least grab the CSRF token from the page
        data.update(_extract_csrf(soup_page))

    # Always include Joomla login fields (override any defaults from the form)
    data['username'] = USERNAME
    data['passwd']   = PASSWORD
    data['password'] = PASSWORD   # send both variants for safety
    data['task']     = 'user.login'
    data['option']   = 'com_users'
    data['return']   = 'aW5kZXgucGhw'  # base64('index.php')

    # Debug: show what we're sending (mask password)
    debug = {k: ('***' if 'pass' in k.lower() else v) for k, v in data.items()}
    print(f'  POST to {action} with fields: {list(debug.keys())}')

    r = session.post(action, data=data, allow_redirects=True, timeout=30)
    print(f'  Response URL: {r.url}  Status: {r.status_code}')

    logged_in = _is_logged_in(r.text)
    print('  Login: OK' if logged_in else '  Login attempt 1 failed')

    if not logged_in:
        # Retry: fetch the CSRF token fresh and try again
        print('  Retrying login with fresh CSRF token...')
        r2 = session.get(BASE_URL + '/index.php?option=com_users&view=login', timeout=30)
        soup2 = BeautifulSoup(r2.text, 'html.parser')
        data2 = {
            'username': USERNAME,
            'passwd':   PASSWORD,
            'password': PASSWORD,
            'task':     'user.login',
            'option':   'com_users',
            'return':   'aW5kZXgucGhw',
        }
        data2.update(_extract_csrf(soup2))
        r3 = session.post(BASE_URL + '/index.php', data=data2, allow_redirects=True, timeout=30)
        print(f'  Retry response URL: {r3.url}  Status: {r3.status_code}')
        logged_in = _is_logged_in(r3.text)
        print('  Login retry: OK' if logged_in else '  Login FAILED — verify KONSULT_USER / KONSULT_PASS secrets')

    return session, logged_in

# ── Scrape one vessel ────────────────────────────────────────────────────────
def scrape_vessel(session, vessel_name, path):
    url = BASE_URL + path

    r = session.get(url, timeout=30)
    print(f'  {vessel_name}: GET → {r.url} ({r.status_code})')
    soup = BeautifulSoup(r.text, 'html.parser')

    # Submit with limit=0 to show all records
    for form in soup.find_all('form'):
        sel = form.find('select', {'name': 'limit'})
        if sel:
            data = {'limit': '0'}
            for inp in form.find_all('input', type='hidden'):
                name = inp.get('name', '')
                if name:
                    data[name] = inp.get('value', '')
            action = form.get('action', url)
            if not action.startswith('http'):
                action = BASE_URL + action
            r = session.post(action, data=data, timeout=30)
            print(f'  {vessel_name}: limit=0 POST → {r.url} ({r.status_code})')
            break

    soup = BeautifulSoup(r.text, 'html.parser')
    all_tables = soup.find_all('table')
    print(f'  {vessel_name}: {len(all_tables)} table(s) on page')
    table = all_tables[0] if all_tables else None
    if not table:
        print(f'  {vessel_name}: no table found — page preview: {soup.get_text(" ",strip=True)[:200]}')
        return []

    rows = table.find_all('tr')
    print(f'  {vessel_name}: table has {len(rows)} rows')
    if len(rows) > 1:
        sample = [td.get_text(strip=True)[:25] for td in rows[1].find_all('td')]
        print(f'  {vessel_name}: row1 cells: {sample}')

    findings = []
    today = datetime.date.today().isoformat()

    for row in table.find_all('tr')[1:]:
        cells = [td.get_text(separator=' ', strip=True) for td in row.find_all('td')]
        if len(cells) < 5:
            continue

        # Detect optional numeric ID column
        offset = 1 if (not cells[0].strip() or re.match(r'^[0-9]+$', cells[0].strip())) else 0
        if offset + 4 >= len(cells):
            continue

        title       = cells[offset].strip()
        group_raw   = cells[offset + 1].strip()
        registered  = cells[offset + 3].strip()
        deadline_s  = cells[offset + 4].strip() if offset + 4 < len(cells) else ''
        conclusion  = cells[offset + 5].strip() if offset + 5 < len(cells) else ''

        # Closed = last cell that is exactly Yes or No
        closed = False
        for cell in reversed(cells):
            t = cell.strip()
            if t == 'Yes':
                closed = True; break
            elif t == 'No':
                closed = False; break

        ftype        = GROUP_MAP.get(group_raw, 'NC')
        date_raised  = parse_date(registered)
        deadline_iso = parse_date(deadline_s)

        if not title or not date_raised:
            continue

        if closed:
            status = 'Closed'
        elif deadline_iso and deadline_iso < today:
            status = 'Overdue'
        else:
            status = 'Open'

        findings.append({
            'id':               str(uuid.uuid4()),
            'vessel':           vessel_name,
            'type':             ftype,
            'title':            title,
            'description':      '',
            'dateRaised':       date_raised,
            'deadline':         deadline_iso,
            'status':           status,
            'correctiveAction': conclusion,
            'raisedBy':         '',
            'notes':            '',
        })

    print(f'  {vessel_name}: {len(findings)} findings')
    return findings

# ── Detect new findings ──────────────────────────────────────────────────────
def find_new(new_all, previous_all):
    prev_keys = {
        (f.get('vessel',''), f.get('title','').lower().strip(), f.get('dateRaised',''))
        for f in previous_all
    }
    return [
        f for f in new_all
        if (f['vessel'], f['title'].lower().strip(), f['dateRaised']) not in prev_keys
    ]

# ── Send email notification ──────────────────────────────────────────────────
def _finding_rows(findings):
    """Return HTML table rows for a list of findings."""
    rows = ''
    for f in findings:
        color = '#c0392b' if f['status'] == 'Overdue' else '#e67e22' if f['status'] == 'Open' else '#27ae60'
        rows += (
            f'<tr><td style="padding:8px;border-bottom:1px solid #eee"><strong>[{f["type"]}]</strong></td>'
            f'<td style="padding:8px;border-bottom:1px solid #eee">{f["vessel"]}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #eee">{f["title"]}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #eee">{f["dateRaised"]}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #eee">{f.get("deadline","")}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #eee;color:{color}">{f["status"]}</td></tr>'
        )
    return rows

TABLE_HEADER = """
  <table style="width:100%;border-collapse:collapse;font-size:14px">
    <tr style="background:#1a3a5c;color:#fff">
      <th style="padding:8px;text-align:left">Type</th>
      <th style="padding:8px;text-align:left">Vessel</th>
      <th style="padding:8px;text-align:left">Title</th>
      <th style="padding:8px;text-align:left">Raised</th>
      <th style="padding:8px;text-align:left">Deadline</th>
      <th style="padding:8px;text-align:left">Status</th>
    </tr>"""

def send_email(new_findings, overdue_findings):
    if not EMAIL_FROM or not EMAIL_TO or not EMAIL_PASS:
        print('Email secrets not configured — skipping notification')
        return

    parts = []
    if new_findings:
        parts.append(f'{len(new_findings)} new')
    if overdue_findings:
        parts.append(f'{len(overdue_findings)} overdue')
    subject = f'ISM Daily Report: {", ".join(parts)}'

    # ── Section 1: New findings ──────────────────────────────────────────────
    new_section = ''
    if new_findings:
        new_section = f"""
      <h2 style="color:#1a3a5c;margin-top:0">[NEW] New Findings ({len(new_findings)})</h2>
      <p>The following findings were added since the last sync:</p>
      {TABLE_HEADER}
        {_finding_rows(new_findings)}
      </table>"""
    else:
        new_section = '<h2 style="color:#1a3a5c;margin-top:0">[NEW] New Findings</h2><p style="color:#555">No new findings since last sync.</p>'

    # ── Section 2: Overdue findings ──────────────────────
