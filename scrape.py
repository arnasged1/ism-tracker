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

# Login
def _is_logged_in(text):
    low = text.lower()
    return ('log out' in low or 'logout' in low or
            'task=user.logout' in low or
            USERNAME.lower() in low)

def _extract_csrf(soup):
    tokens = {}
    for inp in soup.find_all('input', type='hidden'):
        name = inp.get('name', '')
        if re.fullmatch(r'[0-9a-f]{32}', name):
            tokens[name] = '1'
    return tokens

def login():
    session = requests.Session()
    session.headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'

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

    data = {}
    action = BASE_URL + '/index.php'

    if form:
        for inp in form.find_all('input'):
            name = inp.get('name', '')
            if name and inp.get('type') == 'hidden':
                data[name] = inp.get('value', '')
        raw_action = form.get('action', '')
        if raw_action:
            action = raw_action if raw_action.startswith('http') else BASE_URL + raw_action
    elif soup_page:
        data.update(_extract_csrf(soup_page))

    data['username'] = USERNAME
    data['passwd']   = PASSWORD
    data['password'] = PASSWORD
    data['task']     = 'user.login'
    data['option']   = 'com_users'
    data['return']   = 'aW5kZXgucGhw'

    debug = {k: ('***' if 'pass' in k.lower() else v) for k, v in data.items()}
    print(f'  POST to {action} with fields: {list(debug.keys())}')

    r = session.post(action, data=data, allow_redirects=True, timeout=30)
    print(f'  Response URL: {r.url}  Status: {r.status_code}')

    logged_in = _is_logged_in(r.text)
    print('  Login: OK' if logged_in else '  Login attempt 1 failed')

    if not logged_in:
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

# Scrape one vessel
def scrape_vessel(session, vessel_name, path):
    url = BASE_URL + path

    r = session.get(url, timeout=30)
    soup = BeautifulSoup(r.text, 'html.parser')

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
            break

    soup = BeautifulSoup(r.text, 'html.parser')
    table = soup.find('table')
    if not table:
        print(f'  {vessel_name}: no table found (login required or page structure changed)')
        return []

    findings = []
    today = datetime.date.today().isoformat()

    for row in table.find_all('tr')[1:]:
        cells = [td.get_text(separator=' ', strip=True) for td in row.find_all('td')]
        if len(cells) < 5:
            continue

        offset = 1 if re.match(r'^d+$', cells[0].strip()) else 0
        if offset + 4 >= len(cells):
            continue

        title       = cells[offset].strip()
        group_raw   = cells[offset + 1].strip()
        registered  = cells[offset + 3].strip()
        deadline_s  = cells[offset + 4].strip() if offset + 4 < len(cells) else ''
        conclusion  = cells[offset + 5].strip() if offset + 5 < len(cells) else ''

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

# Detect new findings
def find_new(new_all, previous_all):
    prev_keys = {
        (f.get('vessel',''), f.get('title','').lower().strip(), f.get('dateRaised',''))
        for f in previous_all
    }
    return [
        f for f in new_all
        if (f['vessel'], f['title'].lower().strip(), f['dateRaised']) not in prev_keys
    ]

# Send email notification
def send_email(new_findings):
    if not EMAIL_FROM or not EMAIL_TO or not EMAIL_PASS:
        print('Email secrets not configured — skipping notification')
        return

    subject = f'ISM Alert: {len(new_findings)} new finding(s) across fleet'
    rows = ''
    for f in new_findings:
        color = '#c0392b' if f['status']=='Overdue' else '#e67e22' if f['status']=='Open' else '#27ae60'
        rows += (
            f'<tr><td style="padding:8px;border-bottom:1px solid #eee"><strong>[{f["type"]}]</strong></td>'
            f'<td style="padding:8px;border-bottom:1px solid #eee">{f["vessel"]}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #eee">{f["title"]}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #eee">{f["dateRaised"]}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #eee;color:{color}">{f["status"]}</td></tr>'
        )

    html_body = f"""
    <div style="font-family:Arial,sans-serif;max-width:700px">
      <h2 style="color:#1a3a5c">ISM Tracker - New Findings Detected</h2>
      <p><strong>{len(new_findings)} new finding(s)</strong> added to the system:</p>
      <table style="width:100%;border-collapse:collapse;font-size:14px">
        <tr style="background:#1a3a5c;color:#fff">
          <th style="padding:8px;text-align:left">Type</th>
          <th style="padding:8px;text-align:left">Vessel</th>
          <th style="padding:8px;text-align:left">Title</th>
          <th style="padding:8px;text-align:left">Raised</th>
          <th style="padding:8px;text-align:left">Status</th>
        </tr>
        {rows}
      </table>
      <p style="margin-top:20px">
        <a href="{TRACKER_URL}" style="background:#1a5ea8;color:#fff;padding:10px 20px;text-decoration:none;border-radius:5px">
          Open Tracker
        </a>
      </p>
    </div>
    """

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = EMAIL_FROM
    msg['To']      = EMAIL_TO
    msg.attach(MIMEText(html_body, 'html'))

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as srv:
            srv.login(EMAIL_FROM, EMAIL_PASS)
            srv.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        print(f'Email alert sent to {EMAIL_TO}')
    except Exception as e:
        print(f'Email failed: {e}')

# Rebuild index.html with new SEED
def update_html(all_findings):
    if not os.path.exists(INDEX_HTML):
        print(f'ERROR: {INDEX_HTML} not found')
        return False

    with open(INDEX_HTML, 'r', encoding='utf-8') as fh:
        html = fh.read()

    marker = 'const SEED = ['
    start = html.find(marker)
    if start == -1:
        print('ERROR: SEED marker not found in index.html')
        return False

    depth, pos = 0, start + len(marker) - 1
    while pos < len(html):
        c = html[pos]
        if c == '[': depth += 1
        elif c == ']':
            depth -= 1
            if depth == 0:
                end = pos + 1
                break
        pos += 1

    seed_json = json.dumps(all_findings, separators=(',', ':'))
    ts_key = f"'ism_v{int(datetime.datetime.utcnow().timestamp())}'"
    new_html = re.sub(r"'ism_v[\w]+'", ts_key,
                      html[:start] + 'const SEED = ' + seed_json + html[end:])

    with open(INDEX_HTML, 'w', encoding='utf-8') as fh:
        fh.write(new_html)
    print(f'index.html updated ({len(all_findings)} findings)')
    return True

# Main
def main():
    print(f'=== ISM Sync {datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")} ===')
    print(f'index.html path: {INDEX_HTML}')
    print(f'index.html exists: {os.path.exists(INDEX_HTML)}')

    session, logged_in = login()
    if not logged_in:
        print('Aborting - could not log in')
        sys.exit(1)

    all_findings = []
    for vessel_name, path in VESSELS:
        findings = scrape_vessel(session, vessel_name, path)
        all_findings.extend(findings)

    print(f'Total: {len(all_findings)} findings')

    if len(all_findings) == 0:
        print('WARNING: 0 findings scraped - not updating to avoid wiping tracker data')
        sys.exit(1)

    previous = []
    if os.path.exists(PREV_JSON):
        try:
            previous = json.load(open(PREV_JSON))
        except Exception:
            previous = []

    new_ones = find_new(all_findings, previous)
    if new_ones:
        print(f'{len(new_ones)} NEW finding(s) detected!')
        send_email(new_ones)
    else:
        print('No new findings since last sync')

    with open(PREV_JSON, 'w') as fh:
        json.dump(all_findings, fh, indent=2)

    update_html(all_findings)

if __name__ == '__main__':
    main()
