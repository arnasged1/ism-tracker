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

    # Detect which column is the "Closed" column from the header row
    closed_col = -1
    header_row = table.find('tr')
    if header_row:
        headers = [th.get_text(strip=True).lower() for th in header_row.find_all(['th', 'td'])]
        print('  ' + vessel_name + ': headers: ' + str(headers))
        for i, h in enumerate(headers):
            if 'clos' in h:
                closed_col = i
                break
    print('  ' + vessel_name + ': closed_col=' + str(closed_col))

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
        # Search all cells in the row for any link (title cell first, then others)
        for td_idx, td in enumerate(tds):
            for a_tag in td.find_all('a'):
                href = a_tag.get('href', '')
                if href and href != '#':
                    detail_url = href if href.startswith('http') else BASE_URL + href
                    break
            if detail_url:
                break
        if not detail_url:
            print('  ' + vessel_name + ': no link found in row: ' + str([td.get_text(strip=True)[:20] for td in tds]))

        title       = cells[offset].strip()
        group_raw   = cells[offset + 1].strip()
        registered  = cells[offset + 3].strip()
        deadline_s  = cells[offset + 4].strip() if offset + 4 < len(cells) else ''
        conclusion  = cells[offset + 5].strip() if offset + 5 < len(cells) else ''

        closed = False
        if closed_col >= 0 and closed_col < len(cells):
            closed = bool(re.search(r'\byes\b', cells[closed_col], re.IGNORECASE))
        else:
            # Fallback: scan last few cells only (avoid matching text in title/description)
            for cell in cells[max(0, len(cells)-3):]:
                t = cell.strip()
                if re.search(r'\byes\b', t, re.IGNORECASE):
                    closed = True; break
                elif t.lower() == 'no':
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
            'detailUrl':               detail_url,
            'rootCause':               '',
            'managementComment':       '',
            'finalManagementComment':  '',
        })

    print('  ' + vessel_name + ': ' + str(len(findings)) + ' findings')
    return findings

def scrape_detail(session, url):
    """Fetch an individual NC detail page and extract key fields.

    Konsult field labels (from page inspection):
      'Original cause:'                  -> rootCause
      'Explanation:'                     -> description
      'Corrective action made immediately:' -> correctiveAction
      'Final conclusion from the management' -> finalManagementComment
      'Comment from management' (if present) -> managementComment
    """
    empty = {
        'rootCause': '',
        'description': '',
        'correctiveAction': '',
        'managementComment': '',
        'finalManagementComment': '',
    }
    if not url:
        return empty
    try:
        r = session.get(url, timeout=20)
        soup = BeautifulSoup(r.text, 'html.parser')

        def _extract(check):
            # Strategy 1: table row where first cell label matches
            for row in soup.find_all('tr'):
                cells = row.find_all(['th', 'td'])
                if len(cells) >= 2:
                    lbl = cells[0].get_text(strip=True).lower()
                    if check(lbl):
                        return cells[1].get_text(separator=' ', strip=True)
            # Strategy 2: inline label/heading tag followed by sibling value
            for tag in soup.find_all(['label', 'dt', 'th', 'td', 'strong', 'b', 'span']):
                lbl = tag.get_text(strip=True).lower()
                if check(lbl):
                    nxt = tag.find_next_sibling()
                    if nxt:
                        return nxt.get_text(separator=' ', strip=True)
                    if tag.parent:
                        parent_nxt = tag.parent.find_next_sibling()
                        if parent_nxt:
                            return parent_nxt.get_text(separator=' ', strip=True)
            return ''

        result = dict(empty)
        result['rootCause']              = _extract(lambda l: 'original' in l and 'cause' in l)
        result['description']            = _extract(lambda l: l.startswith('explanation'))
        result['correctiveAction']       = _extract(lambda l: 'corrective' in l and 'action' in l)
        result['managementComment']      = _extract(lambda l: 'comment' in l and 'management' in l and 'final' not in l)
        result['finalManagementComment'] = _extract(lambda l: 'final' in l and ('conclusion' in l or 'management' in l))

        print('  Detail scraped: root=' + repr(result['rootCause'][:30]) +
              ' ca=' + repr(result['correctiveAction'][:30]) +
              ' final=' + repr(result['finalManagementComment'][:30]))
        return result
    except Exception as e:
        print('  Detail fetch error for ' + url + ': ' + str(e))
        return empty

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
    # Escape </script> so embedded JSON can't break the HTML script tag
    seed_json = seed_json.replace('</', '<\\/')
    # Replace the SEED line directly (avoids regex ];  matching issues inside JSON values)
    new_seed_line = 'const SEED = ' + seed_json + ';'
    lines = html.split('\n')
    replaced = False
    for i, line in enumerate(lines):
        if re.match(r'\s*const SEED\s*=\s*\[', line):
            lines[i] = new_seed_line
            replaced = True
            break
    if replaced:
        html = '\n'.join(lines)
    else:
        print('WARNING: SEED line not found in template — dashboard not updated')
        return
    with open(INDEX_HTML, 'w', encoding='utf-8') as f:
        f.write(html)
    print('Dashboard updated: ' + INDEX_HTML)


def main():
    print('=== ISM Scraper starting ===')
    session, logged_in = login()
    if not logged_in:
        print('Login failed — aborting')
        sys.exit(1)

    # Scrape all vessels
    all_findings = []
    for vessel_name, path in VESSELS:
        print('Scraping ' + vessel_name + '...')
        all_findings.extend(scrape_vessel(session, vessel_name, path))
    print('Total findings scraped: ' + str(len(all_findings)))

    # Load previous findings for caching detail fields of Closed findings
    previous_all = []
    if os.path.exists(PREV_JSON):
        with open(PREV_JSON, encoding='utf-8') as f:
            previous_all = json.load(f)

    prev_cache = {}
    for pf in previous_all:
        key = (pf.get('vessel', ''), pf.get('title', '').lower().strip(), pf.get('dateRaised', ''))
        prev_cache[key] = {
            'rootCause':              pf.get('rootCause', ''),
            'description':            pf.get('description', ''),
            'correctiveAction':       pf.get('correctiveAction', ''),
            'managementComment':      pf.get('managementComment', ''),
            'finalManagementComment': pf.get('finalManagementComment', ''),
        }

    # Populate detail fields from Konsult detail pages
    print('Fetching detail pages...')
    for finding in all_findings:
        key = (finding['vessel'], finding['title'].lower().strip(), finding['dateRaised'])
        if finding['status'] == 'Closed':
            cached = prev_cache.get(key, {})
            finding['rootCause']              = cached.get('rootCause', '')
            finding['managementComment']      = cached.get('managementComment', '')
            finding['finalManagementComment'] = cached.get('finalManagementComment', '')
            if cached.get('description'):
                finding['description'] = cached['description']
            if cached.get('correctiveAction'):
                finding['correctiveAction'] = cached['correctiveAction']
        else:
            durl = finding.get('detailUrl', '')
            if not durl:
                print('  No detailUrl for: ' + finding['vessel'] + ' / ' + finding['title'][:40])
            detail = scrape_detail(session, durl)
            finding['rootCause']              = detail['rootCause']
            finding['managementComment']      = detail['managementComment']
            finding['finalManagementComment'] = detail['finalManagementComment']
            # Enrich description and correctiveAction from detail page if available
            if detail['description']:
                finding['description'] = detail['description']
            if detail['correctiveAction']:
                finding['correctiveAction'] = detail['correctiveAction']

    # Overdue summary for logs
    overdue_findings = [f for f in all_findings if f['status'] == 'Overdue']
    print('Overdue findings: ' + str(len(overdue_findings)))
    for f in overdue_findings:
        print('  OVERDUE: ' + f['vessel'] + ' / ' + f['title'] + ' / deadline: ' + f['deadline'])

    # Load email snapshot to find new findings
    email_snap = []
    if os.path.exists(EMAIL_SNAP):
        with open(EMAIL_SNAP, encoding='utf-8') as f:
            email_snap = json.load(f)
    new_findings = find_new(all_findings, email_snap)

    # Send email at 06:00 UTC, on manual dispatch, or on first run
    event_name = os.environ.get('GITHUB_EVENT_NAME', '')
    hour_utc   = datetime.datetime.utcnow().hour
    should_email = (
        not os.path.exists(EMAIL_SNAP) or
        event_name == 'workflow_dispatch' or
        hour_utc == 6
    )
    if should_email:
        print('Sending email...')
        send_email(new_findings, overdue_findings)
        with open(EMAIL_SNAP, 'w', encoding='utf-8') as f:
            json.dump(all_findings, f, ensure_ascii=False, indent=2)
        print('Email snapshot saved.')
    else:
        print('Skipping email (hour=' + str(hour_utc) + ', event=' + str(event_name) + ')')

    # Save previous findings
    with open(PREV_JSON, 'w', encoding='utf-8') as f:
        json.dump(all_findings, f, ensure_ascii=False, indent=2)
    print('Saved ' + PREV_JSON)

    # Update dashboard HTML (only if we have findings, to avoid wiping dashboard on scrape failure)
    if all_findings:
        generate_html(all_findings)
    else:
        print('WARNING: all_findings is empty — skipping dashboard update to preserve existing data')
    print('=== ISM Scraper done ===')


if __name__ == '__main__':
    main()
