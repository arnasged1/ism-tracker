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
TRACKER_URL = os.environ.get('TRACKER_URL', 'https://ism-tracker-qga2ynufb-shippingfo.vercel.app')

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
def login():
    session = requests.Session()
    session.headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'

    r = session.get(BASE_URL + '/index.php')
    soup = BeautifulSoup(r.text, 'html.parser')

    # Find login form
    form = soup.find('form', id='login-form')
    if not form:
        for f in soup.find_all('form'):
            if f.find('input', {'name': 'username'}):
                form = f
                break

    data = {
        'username': USERNAME,
        'passwd':   PASSWORD,
        'task':     'user.login',
        'option':   'com_users',
    }
    if form:
        for inp in form.find_all('input', type='hidden'):
            name = inp.get('name', '')
            if name:
                data[name] = inp.get('value', '')
        action = form.get('action', BASE_URL + '/index.php')
    else:
        action = BASE_URL + '/index.php'

    if not action.startswith('http'):
        action = BASE_URL + action

    r = session.post(action, data=data, allow_redirects=True)

    if 'logout' in r.text.lower():
        print('Login successful')
    else:
        print('WARNING: login may have failed — check credentials')

    return session

# ── Scrape one vessel ────────────────────────────────────────────────────────
def scrape_vessel(session, vessel_name, path):
    url = BASE_URL + path

    # Get page, then resubmit with limit=0 to show all records
    r = session.get(url)
    soup = BeautifulSoup(r.text, 'html.parser')

    for form in soup.find_all('form'):
        if form.find('select', {'name': 'limit'}) or form.find('input', {'name': 'limit'}):
            data = {'limit': '0'}
            for inp in form.find_all('input', type='hidden'):
                name = inp.get('name', '')
                if name:
                    data[name] = inp.get('value', '')
            action = form.get('action', url)
            if not action.startswith('http'):
                action = BASE_URL + action
            r = session.post(action, data=data)
            break

    soup = BeautifulSoup(r.text, 'html.parser')
    table = soup.find('table')
    if not table:
        print(f'  {vessel_name}: no table found')
        return []

    findings = []
    today = datetime.date.today().isoformat()

    rows = table.find_all('tr')[1:]  # skip header
    for row in rows:
        cells = [td.get_text(separator=' ', strip=True) for td in row.find_all('td')]
        if len(cells) < 5:
            continue

        # Detect optional numeric ID column
        offset = 1 if re.match(r'^\d+$', cells[0].strip()) else 0

        if offset + 4 >= len(cells):
            continue

        title     = cells[offset].strip()
        group_raw = cells[offset + 1].strip()
        registered = cells[offset + 3].strip()
        deadline_s = cells[offset + 4].strip() if offset + 4 < len(cells) else ''
        conclusion = cells[offset + 5].strip() if offset + 5 < len(cells) else ''

        # Closed = last cell that is exactly 'Yes' or 'No'
        closed = False
        for cell in reversed(cells):
            t = cell.strip()
            if t == 'Yes':
                closed = True
                break
            elif t == 'No':
                closed = False
                break

        ftype      = GROUP_MAP.get(group_raw, 'NC')
        date_raised = parse_date(registered)
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
            'id':              str(uuid.uuid4()),
            'vessel':          vessel_name,
            'type':            ftype,
            'title':           title,
            'description':     '',
            'dateRaised':      date_raised,
            'deadline':        deadline_iso,
            'status':          status,
            'correctiveAction': conclusion,
            'raisedBy':        '',
            'notes':           '',
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
      <h2 style="color:#1a3a5c">ISM Tracker — New Findings Detected</h2>
      <p><strong>{len(new_findings)} new finding(s)</strong> have been added to the system:</p>
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

# ── Rebuild index.html with new SEED ────────────────────────────────────────
def update_html(all_findings):
    with open('index.html', 'r', encoding='utf-8') as fh:
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
    # Bump storage key to a timestamp so browser always reloads
    ts_key = f"'ism_v{int(datetime.datetime.utcnow().timestamp())}'"
    new_html = re.sub(r"'ism_v\d+'", ts_key, html[:start] + 'const SEED = ' + seed_json + html[end:])

    with open('index.html', 'w', encoding='utf-8') as fh:
        fh.write(new_html)
    print('index.html updated')
    return True

# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    print(f'=== ISM Sync {datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")} ===')

    session = login()

    all_findings = []
    for vessel_name, path in VESSELS:
        findings = scrape_vessel(session, vessel_name, path)
        all_findings.extend(findings)

    print(f'Total: {len(all_findings)} findings')

    # Load previous snapshot
    previous = []
    if os.path.exists('previous_findings.json'):
        try:
            previous = json.load(open('previous_findings.json'))
        except Exception:
            previous = []

    # Detect and notify new findings
    new_ones = find_new(all_findings, previous)
    if new_ones:
        print(f'{len(new_ones)} NEW finding(s) detected!')
        send_email(new_ones)
    else:
        print('No new findings since last sync')

    # Save snapshot for next run
    with open('previous_findings.json', 'w') as fh:
        json.dump(all_findings, fh, indent=2)

    # Rebuild the tracker HTML
    update_html(all_findings)

if __name__ == '__main__':
    main()
