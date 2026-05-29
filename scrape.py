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

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(SCRIPT_DIR, 'index.html')
PREV_JSON  = os.path.join(SCRIPT_DIR, 'previous_findings.json')
EMAIL_SNAP = os.path.join(SCRIPT_DIR, 'email_snapshot.json')

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
        print('  Fetching ' + url)
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
                print('  Login form found on ' + url)
                break
        if form:
            break

    if not form:
        print('  No login form found — will attempt blind POST')

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
    print('  POST to ' + action + ' with fields: ' + str(list(debug.keys())))

    r = session.post(action, data=data, allow_redirects=True, timeout=30)
    print('  Response URL: ' + r.url + '  Status: ' + str(r.status_code))

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
        print('  Retry response URL: ' + r3.url + '  Status: ' + str(r3.status_code))
        logged_in = _is_logged_in(r3.text)
        print('  Login retry: OK' if logged_in else '  Login FAILED — verify KONSULT_USER / KONSULT_PASS secrets')

    return session, logged_in

def scrape_vessel(session, vessel_name, path):
    url = BASE_URL + path
    r = session.get(url, timeout=30)
    print('  ' + vessel_name + ': GET -> ' + r.url + ' (' + str(r.status_code) + ')')
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
            print('  ' + vessel_name + ': limit=0 POST -> ' + r.url + ' (' + str(r.status_code) + ')')
            break

    soup = BeautifulSoup(r.text, 'html.parser')
    all_tables = soup.find_all('table')
    print('  ' + vessel_name + ': ' + str(len(all_tables)) + ' table(s) on page')
    table = all_tables[0] if all_tables else None
    if not table:
        print('  ' + vessel_name + ': no table found — page preview: ' + soup.get_text(' ', strip=True)[:200])
        return []

    rows = table.find_all('tr')
    print('  ' + vessel_name + ': table has ' + str(len(rows)) + ' rows')
    if len(rows) > 1:
        sample = [td.get_text(strip=True)[:25] for td in rows[1].find_all('td')]
        print('  ' + vessel_name + ': row1 cells: ' + str(sample))

    findings = []
    today = datetime.date.today().isoformat()

    for row in table.find_all('tr')[1:]:
        tds   = row.find_all('td')
        cells = [td.get_text(separator=' ', strip=True) for td in tds]
        if len(cells) < 5:
            continue

        offset = 1 if (not cells[0].strip() or re.match(r'^[0-9]+$', cells[0].strip())) else 0
        if offset + 4 >= len(cells):
            continue

        detail_url = ''
        if offset < len(tds):
            a_tag = tds[offset].find('a')
            if a_tag and a_tag.get('href'):
                href = a_tag['href']
                detail_url = href if href.startswith('http') else BASE_URL + href

        title       = cells[offset].strip()
        group_raw   = cells[offset + 1].strip()
        registered  = cells[offset + 3].strip()
        deadline_s  = cells[offset + 4].strip() if offset + 4 < len(cells) else ''
        conclusion  = cells[offset + 5].strip() if offset + 5 < len(cells) else ''

        closed = False
        for cell in reversed(cells):
            t = cell.strip()
            if re.search(r'\byes\b', t, re.IGNORECASE):
                closed = True; break
            elif re.search(r'\bno\b', t, re.IGNORECASE):
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
            'id':                str(uuid.uuid4()),
            'vessel':            vessel_name,
            'type':              ftype,
            'title':             title,
            'description':       '',
            'dateRaised':        date_raised,
            'deadline':          deadline_iso,
            'status':            status,
            'correctiveAction':  conclusion,
            'raisedBy':          '',
            'notes':             '',
            'detailUrl':         detail_url,
            'managementComment': '',
        })

    print('  ' + vessel_name + ': ' + str(len(findings)) + ' findings')
    return findings

def scrape_detail(session, url):
    """Fetch an individual NC detail page and return the 'Comment from management' text."""
    if not url:
        return ''
    try:
        r = session.get(url, timeout=20)
        soup = BeautifulSoup(r.text, 'html.parser')
        # Strategy 1: table row where first cell is the label
        for row in soup.find_all('tr'):
            cells = row.find_all(['th', 'td'])
            if len(cells) >= 2:
                label = cells[0].get_text(strip=True).lower()
                if 'comment' in label and 'management' in label:
                    return cells[1].get_text(separator=' ', strip=True)
        # Strategy 2: any inline label tag followed by a sibling value
        for tag in soup.find_all(['label', 'dt', 'th', 'td', 'strong', 'b', 'span']):
            tag_text = tag.get_text(strip=True).lower()
            if 'comment' in tag_text and 'management' in tag_text:
                nxt = tag.find_next_sibling()
                if nxt:
                    return nxt.get_text(separator=' ', strip=True)
                if tag.parent:
                    parent_nxt = tag.parent.find_next_sibling()
                    if parent_nxt:
                        return parent_nxt.get_text(separator=' ', strip=True)
        return ''
    except Exception as e:
        print('  Detail fetch error for ' + url + ': ' + str(e))
        return ''

def find_new(new_all, previous_all):
    prev_keys = {
        (f.get('vessel',''), f.get('title','').lower().strip(), f.get('dateRaised',''))
        for f in previous_all
    }
    return [
        f for f in new_all
        if (f['vessel'], f['title'].lower().strip(), f['dateRaised']) not in prev_keys
    ]

def _finding_rows(findings):
    rows = ''
    for f in findings:
        if f['status'] == 'Overdue':
            color = '#c0392b'
        elif f['status'] == 'Open':
            color = '#e67e22'
        else:
            color = '#27ae60'
        rows += (
            '<tr>'
            '<td style="padding:8px;border-bottom:1px solid #eee"><strong>[' + f['type'] + ']</strong></td>'
            '<td style="padding:8px;border-bottom:1px solid #eee">' + f['vessel'] + '</td>'
            '<td style="padding:8px;border-bottom:1px solid #eee">' + f['title'] + '</td>'
            '<td style="padding:8px;border-bottom:1px solid #eee">' + f['dateRaised'] + '</td>'
            '<td style="padding:8px;border-bottom:1px solid #eee">' + f.get('deadline','') + '</td>'
            '<td style="padding:8px;border-bottom:1px solid #eee;color:' + color + '">' + f['status'] + '</td>'
            '</tr>'
        )
    return rows

TABLE_HDR = (
    '<table style="width:100%;border-collapse:collapse;font-size:14px">'
    '<tr style="background:#1a3a5c;color:#fff">'
    '<th style="padding:8px;text-align:left">Type</th>'
    '<th style="padding:8px;text-align:left">Vessel</th>'
    '<th style="padding:8px;text-align:left">Title</th>'
    '<th style="padding:8px;text-align:left">Raised</th>'
    '<th style="padding:8px;text-align:left">Deadline</th>'
    '<th style="padding:8px;text-align:left">Status</th>'
    '</tr>'
)

def send_email(new_findings, overdue_findings):
    if not EMAIL_FROM or not EMAIL_TO or not EMAIL_PASS:
        print('Email secrets not configured — skipping notification')
        return

    parts = []
    if new_findings:
        parts.append(str(len(new_findings)) + ' new')
    if overdue_findings:
        parts.append(str(len(overdue_findings)) + ' overdue')
    if not parts:
        parts.append('all clear')
    subject = 'ISM Daily Report: ' + ', '.join(parts)

    if new_findings:
        new_section = (
            '<h2 style="color:#1a3a5c;margin-top:0">[NEW] New Findings (' + str(len(new_findings)) + ')</h2>'
            '<p>The following findings were added since the last sync:</p>'
            + TABLE_HDR + _finding_rows(new_findings) + '</table>'
        )
    else:
        new_section = (
            '<h2 style="color:#1a3a5c;margin-top:0">[NEW] New Findings</h2>'
            '<p style="color:#555">No new findings since last sync.</p>'
        )

    if overdue_findings:
        overdue_section = (
            '<h2 style="color:#c0392b;margin-top:32px">[!] Overdue Findings (' + str(len(overdue_findings)) + ')</h2>'
            '<p>The following open findings have passed their deadline:</p>'
            + TABLE_HDR + _finding_rows(overdue_findings) + '</table>'
        )
    else:
        overdue_section = (
            '<h2 style="color:#27ae60;margin-top:32px">[OK] No Overdue Findings</h2>'
            '<p style="color:#555">All open findings are within deadline.</p>'
        )

    tracker_link = (
        '<p style="margin-top:28px">'
        '<a href="' + TRACKER_URL + '" style="background:#1a3a5c;color:#fff;padding:10px 20px;'
        'text-decoration:none;border-radius:4px;font-family:Arial,sans-serif;font-size:14px">'
        'Open Tracker</a></p>'
    )

    html_body = (
        '<div style="font-family:Arial,sans-serif;max-width:740px;padding:16px">'
        + new_section + overdue_section + tracker_link + '</div>'
    )

    # Build recipient list (EMAIL_TO secret + fixed extra recipients)
    recipients = [r.strip() for r in EMAIL_TO.split(',') if r.strip()]
    for extra in ['per@shipping.fo']:
        if extra not in recipients:
            recipients.append(extra)

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = EMAIL_FROM
    msg['To']      = ', '.join(recipients)
    msg.attach(MIMEText(html_body, 'html'))

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(EMAIL_FROM, EMAIL_PASS)
            server.sendmail(EMAIL_FROM, recipients, msg.as_string())
        print('Email sent to ' + str(recipients) + ': ' + subject)
    except Exception as e:
        print('Email error: ' + str(e))

def generate_html(all_findings):
    template_path = INDEX_HTML
    alt_template  = os.path.join(SCRIPT_DIR, 'index.html.html')
    if not os.path.exists(template_path) and os.path.exists(alt_template):
        template_path = alt_template
    if not os.path.exists(template_path):
        print('No HTML template found — skipping dashboard generation')
        return
    with open(template_path, encoding='utf-8') as f:
        html = f.read()
    seed_json = json.dumps(all_findings, ensure_ascii=False)
    html = re.sub(r'const SEED\s*=\s*\[.*?\];', 'const SEED = ' + seed_json + ';', html, flags=re.DOTALL)
    with open(INDEX_HTML, 'w', encoding='utf-8') as f:
        f.write(html)
    print('index.html updated with ' + str(len(all_findings)) + ' findings')

def main():
    print('=== ISM Tracker Sync ===')
    session, logged_in = login()
    if not logged_in:
        print('Cannot proceed without login')
        sys.exit(1)

    all_findings = []
    for vessel_name, path in VESSELS:
        findings = scrape_vessel(session, vessel_name, path)
        all_findings.extend(findings)

    print('Total findings scraped: ' + str(len(all_findings)))

    # ── Fetch management comments for open/overdue findings ──────────────────
    # For closed findings, use cached value from previous run to avoid extra requests
    prev_cache = {}
    if os.path.exists(PREV_JSON):
        try:
            with open(PREV_JSON, encoding='utf-8') as f:
                prev_list = json.load(f)
            for pf in prev_list:
                key = (pf.get('vessel',''), pf.get('title','').lower().strip(), pf.get('dateRaised',''))
                prev_cache[key] = pf
        except Exception as e:
            print('Could not load previous findings for cache: ' + str(e))

    open_count = sum(1 for f in all_findings if f['status'] in ('Open', 'Overdue'))
    print('Fetching management comments for ' + str(open_count) + ' open/overdue findings...')
    for f in all_findings:
        key = (f['vessel'], f['title'].lower().strip(), f['dateRaised'])
        if f['status'] in ('Open', 'Overdue') and f.get('detailUrl'):
            f['managementComment'] = scrape_detail(session, f['detailUrl'])
        elif key in prev_cache:
            f['managementComment'] = prev_cache[key].get('managementComment', '')

    # ── Update website (every run) ───────────────────────────────────────────
    with open(PREV_JSON, 'w', encoding='utf-8') as f:
        json.dump(all_findings, f, indent=2, ensure_ascii=False)
    print('Saved ' + str(len(all_findings)) + ' findings to previous_findings.json')

    generate_html(all_findings)

    # ── Send daily email (only at 06:00 UTC run) ─────────────────────────────
    utc_hour = datetime.datetime.utcnow().hour
    print('Current UTC hour: ' + str(utc_hour))
    first_run = not os.path.exists(EMAIL_SNAP)
    manual = os.environ.get('GITHUB_EVENT_NAME', '') == 'workflow_dispatch'
    if utc_hour == 6 or first_run or manual:
        print('Running daily email report...')
        email_snapshot = []
        if os.path.exists(EMAIL_SNAP):
            try:
                with open(EMAIL_SNAP, encoding='utf-8') as f:
                    email_snapshot = json.load(f)
            except Exception as e:
                print('Could not load email snapshot: ' + str(e))
        print('Email snapshot had ' + str(len(email_snapshot)) + ' findings')

        new_ones = find_new(all_findings, email_snapshot)
        if new_ones:
            print(str(len(new_ones)) + ' NEW finding(s) since last email!')
            for f in new_ones:
                print('  + [' + f['vessel'] + '] ' + f['title'])
        else:
            print('No new findings since last email')

        overdue_ones = [f for f in all_findings if f['status'] == 'Overdue']
        print(str(len(overdue_ones)) + ' overdue finding(s) fleet-wide')
        for f in overdue_ones:
            print('  OVERDUE: [' + f['vessel'] + '] ' + f['title'] + ' | deadline: ' + f.get('deadline','') + ' | detailUrl: ' + f.get('detailUrl','')[:60])

        send_email(new_ones, overdue_ones)

        with open(EMAIL_SNAP, 'w', encoding='utf-8') as f:
            json.dump(all_findings, f, indent=2, ensure_ascii=False)
        print('Email snapshot updated')
    else:
        print('Skipping email — not the 06:00 UTC run (hour=' + str(utc_hour) + ')')

    print('=== Sync complete ===')

if __name__ == '__main__':
    main()
