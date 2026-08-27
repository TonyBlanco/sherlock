#!/usr/bin/env python3
"""
Sherlock Web Interface
A Flask web application to search for usernames across social networks.
"""

from flask import Flask, render_template, request, jsonify, send_file
import subprocess
import os
import sys
import json
import csv
import tempfile
import shutil
import io
import re
import time
import unicodedata
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from fpdf import FPDF

app = Flask(__name__)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Persistent on-disk result cache
# ---------------------------------------------------------------------------
# Each search is stored as cache/<username>.json with the full results plus a
# timestamp. A repeated search for the same username within CACHE_TTL is served
# instantly from disk instead of re-checking 490+ sites.
CACHE_DIR = os.path.join(SCRIPT_DIR, 'cache')
CACHE_TTL = 8 * 3600          # 8 hours
CACHE_MAX_ENTRIES = 100       # drop the oldest entries beyond this

# In-memory cache for last search results (export + fast variant path)
last_results = {}

# In-memory cache for the last cross-variant comparison matrix
last_comparison = {}

# Live progress for asynchronous variant searches
search_progress = {}      # search_id -> progress dict
variant_final_results = {}  # search_id -> final payload

# Asynchronous single searches: on free hosting the HTTP proxy cuts requests
# at ~60s and a fresh 492-site search regularly exceeds that, which used to
# kill the response with a 502. Fresh searches now run in a background thread
# and the UI polls /search-status/<username> until done.
single_searches = {}          # username -> {'status': 'running'|'done', 'result': ...}
_single_search_lock = threading.Lock()


def _cache_path(username):
    safe = re.sub(r'[^a-zA-Z0-9_.\-]', '_', username)
    return os.path.join(CACHE_DIR, f'{safe}.json')


def cache_get(username):
    """Return (results, cached_at) from disk if fresh, else (None, None).

    Entries with empty results are treated as missing: they used to be cached
    when a subprocess failed, poisoning the cache with a useless 0-site reply.
    """
    path = _cache_path(username)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        cached_at = data.get('cached_at', 0)
        if not data.get('results'):
            return None, None
        if time.time() - cached_at <= CACHE_TTL:
            return data['results'], cached_at
    except Exception:
        pass
    return None, None


def cache_put(username, results):
    """Write results to disk cache, pruning the oldest entries when full."""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        data = {'username': username, 'cached_at': time.time(), 'results': results}
        with open(_cache_path(username), 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass

    # Prune oldest entries beyond the cap
    try:
        entries = []
        for name in os.listdir(CACHE_DIR):
            if name.endswith('.json'):
                p = os.path.join(CACHE_DIR, name)
                entries.append((os.path.getmtime(p), p))
        if len(entries) > CACHE_MAX_ENTRIES:
            entries.sort()
            for _, p in entries[:len(entries) - CACHE_MAX_ENTRIES]:
                try:
                    os.remove(p)
                except Exception:
                    pass
    except Exception:
        pass


def load_config():
    """Load config/settings.json (blocked sites to skip, etc.)."""
    cfg_path = os.path.join(SCRIPT_DIR, 'config', 'settings.json')
    try:
        with open(cfg_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def load_false_positives():
    """Load the curated false-positive site list from disk."""
    fp_path = os.path.join(SCRIPT_DIR, 'config', 'false_positives.json')
    try:
        with open(fp_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data.get('sites', [])
    except Exception:
        return []


def sanitize_username(username):
    """Clean a username so it can never produce broken %20 URLs.

    Strips surrounding whitespace, drops spaces entirely (a space in a
    username is almost always a full-name query that should go through
    the variant generator instead), and normalizes %20 encoded spaces
    that may come from old cached history entries.
    """
    u = username.strip()
    u = u.replace('%20', ' ').strip()
    # Remove internal spaces -> concatenate the words (tony blanco -> tonyblanco)
    u = re.sub(r'\s+', '', u)
    return u


def sanitize_result_urls(results):
    """Replace %20 (and other encoded spaces) in result URLs.

    Old searches with a literal space produced urls like
    https://www.shelf.im/tony%20blanco which point to dead pages but are
    marked 'Claimed'. Encoding the space as '_' keeps the link meaningful
    without the broken %20.
    """
    for r in results:
        for key in ('url_main', 'url_user'):
            if r.get(key):
                r[key] = r[key].replace('%20', '_')
    return results


def run_sherlock_search(username, timeout=5, skip_sites=None):
    """Run Sherlock search and return results as JSON.

    Uses the on-disk cache when a fresh result already exists.
    """
    username = sanitize_username(username)

    # Fast path: fresh cached results on disk
    cached, cached_at = cache_get(username)
    if cached is not None:
        results = sanitize_result_urls([dict(r) for r in cached])
        last_results[username] = results
        stats = compute_stats(results)
        return {
            'success': True,
            'username': username,
            'results': results,
            'stats': stats,
            'cached': True,
            'cached_at': cached_at,
        }

    try:
        work_dir = tempfile.mkdtemp(prefix="sherlock_")

        cmd = [
            sys.executable, "-m", "sherlock_project.sherlock",
            "--local",
            "--no-update-check",
            "--timeout", str(timeout),
            # print_found defaults to True in Sherlock, which makes the CSV only
            # contain Claimed sites. We want every site so the UI can show
            # found/not-found/error counts accurately.
            "--print-all",
            "--csv",
        ]

        # Optional: skip sites known to be dead/blocked (speed).
        # The skip list is configured in config/settings.json under "skip_sites"
        # and defaults to empty so totals always reflect the full site list.
        skip_sites = skip_sites if skip_sites is not None else load_config().get('skip_sites', [])
        if skip_sites:
            cmd.append("--site-list")
            cmd.append(','.join(skip_sites))

        cmd.append(username)

        # Always use the local sherlock_project copy (this project's data.json
        # with the extra sites), even if an editable install of sherlock is
        # present on PYTHONPATH (e.g. a network share).
        env = dict(os.environ)
        # The sherlock_project package lives next to web_app.py, so its parent
        # (the directory containing the package) IS SCRIPT_DIR itself.
        local_root = SCRIPT_DIR
        env['PYTHONPATH'] = local_root + os.pathsep + env.get('PYTHONPATH', '')

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=work_dir,
            env=env,
            timeout=timeout * 6 + 60,
        )

        results = []
        csv_file = os.path.join(work_dir, f"{username}.csv")

        if os.path.exists(csv_file):
            with open(csv_file, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    results.append({
                        'site_name': row.get('name', ''),
                        'url_main': row.get('url_main', ''),
                        'url_user': row.get('url_user', ''),
                        'exists': row.get('exists', ''),
                        'http_status': row.get('http_status', ''),
                        'response_time_s': row.get('response_time_s', '')
                    })

        sanitize_result_urls(results)

        shutil.rmtree(work_dir, ignore_errors=True)

        if not results:
            # The subprocess ran but produced no CSV. Surface everything it
            # printed so container problems are debuggable, and never cache
            # an empty result.
            return {
                'success': False,
                'username': username,
                'error': 'Sherlock subprocess produced no CSV',
                'returncode': result.returncode,
                'stdout_tail': (result.stdout or '')[-600:],
                'stderr_tail': (result.stderr or '')[-600:],
                'results': [],
                'stats': compute_stats([]),
                'cached': False,
            }

        stats = compute_stats(results)

        # Cache results for export and future searches
        last_results[username] = results
        cache_put(username, results)

        return {
            'success': True,
            'username': username,
            'results': results,
            'stats': stats,
            'cached': False,
            'cached_at': None,
            'output': result.stdout
        }

    except Exception as e:
        return {
            'success': False,
            'error': str(e),
            'username': username
        }


def compute_stats(results):
    return {
        'total_checked': len(results),
        'found': sum(1 for r in results if r['exists'] == 'Claimed'),
        'not_found': sum(1 for r in results if r['exists'] == 'Available'),
        'errors': sum(1 for r in results if r['exists'] not in ['Claimed', 'Available'])
    }


@app.route('/')
def index():
    # Pass the real site count from data.json so the header stays accurate
    # even after adding/removing sites.
    site_count = 0
    data_load_error = None
    try:
        data_path = os.path.join(SCRIPT_DIR, 'sherlock_project', 'resources', 'data.json')
        with open(data_path, 'r', encoding='utf-8') as f:
            site_count = len(json.load(f)) - 1  # minus the $schema key
    except Exception as e:
        # Log instead of silently swallowing: a missing/corrupt data.json in a
        # deployed container used to be invisible (site_count rendered empty).
        data_load_error = str(e)
        app.logger.error('Could not load data.json for site count: %s', e)
    return render_template('index.html', site_count=site_count, data_load_error=data_load_error)


@app.route('/api/false-positives')
def api_false_positives():
    """Return the curated false-positive site list (server-managed)."""
    return jsonify({'sites': load_false_positives()})


@app.route('/api/debug')
def api_debug():
    """Diagnostic endpoint: container filesystem and data.json health."""
    info = {
        'script_dir': SCRIPT_DIR,
        'cwd': os.getcwd(),
        'python': sys.executable,
        'env_pythonpath': os.environ.get('PYTHONPATH', ''),
        'dir_listing': sorted(os.listdir(SCRIPT_DIR)),
    }
    # Memory readings (Linux container): app RSS + RSS of child processes.
    try:
        with open('/proc/self/status', 'r') as f:
            for line in f:
                if line.startswith('VmRSS'):
                    info['app_rss_kb'] = int(line.split()[1])
                    break
        children = []
        for pid in os.listdir('/proc'):
            if not pid.isdigit() or int(pid) == os.getpid():
                continue
            try:
                with open(f'/proc/{pid}/status', 'r') as f:
                    fields = {}
                    for line in f:
                        if line.startswith('VmRSS'):
                            fields['rss_kb'] = int(line.split()[1])
                            break
                if fields:
                    children.append({'pid': int(pid), 'rss_kb': fields['rss_kb']})
            except Exception:
                pass
        info['child_processes'] = children
    except Exception:
        pass
    data_path = os.path.join(SCRIPT_DIR, 'sherlock_project', 'resources', 'data.json')
    info['data_json_path'] = data_path
    info['data_json_exists'] = os.path.exists(data_path)
    if info['data_json_exists']:
        info['data_json_size'] = os.path.getsize(data_path)
        try:
            with open(data_path, 'r', encoding='utf-8') as f:
                d = json.load(f)
            info['data_json_valid'] = True
            info['data_json_sites'] = len([k for k in d if k != '$schema'])
        except Exception as e:
            info['data_json_valid'] = False
            info['data_json_error'] = str(e)
    pkg_dir = os.path.join(SCRIPT_DIR, 'sherlock_project')
    info['sherlock_project_exists'] = os.path.isdir(pkg_dir)
    if info['sherlock_project_exists']:
        info['sherlock_project_listing'] = sorted(os.listdir(pkg_dir))
    return jsonify(info)


@app.route('/search', methods=['POST'])
def search():
    username = sanitize_username(request.form.get('username', '').strip())
    if not username:
        return jsonify({'success': False, 'error': 'Username is required'})

    # Fresh cached results: answer synchronously as before.
    cached, _ = cache_get(username)
    if cached is not None:
        return jsonify(run_sherlock_search(username))

    # Otherwise run in the background and let the UI poll for the result.
    with _single_search_lock:
        job = single_searches.get(username)
        if job and job['status'] == 'running':
            return jsonify({'success': True, 'async': True, 'username': username})
        single_searches[username] = {'status': 'running', 'result': None}

    def _worker():
        res = run_sherlock_search(username)
        with _single_search_lock:
            single_searches[username] = {'status': 'done', 'result': res}

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({'success': True, 'async': True, 'username': username})


@app.route('/search-status/<username>')
def search_status(username):
    """Poll the status of an asynchronous single search."""
    username = sanitize_username(username)
    job = single_searches.get(username)
    if not job:
        # Fall back to the disk cache: the result may have landed there.
        cached, cached_at = cache_get(username)
        if cached is not None:
            results = sanitize_result_urls([dict(r) for r in cached])
            return jsonify({
                'success': True,
                'status': 'done',
                'result': {
                    'success': True,
                    'username': username,
                    'results': results,
                    'stats': compute_stats(results),
                    'cached': True,
                    'cached_at': cached_at,
                },
            })
        return jsonify({'success': False, 'error': 'Búsqueda no encontrada'}), 404
    if job['status'] == 'running':
        return jsonify({'success': True, 'status': 'running'})
    return jsonify({'success': True, 'status': 'done', 'result': job['result']})


def normalize_name(name):
    """Lowercase and strip accents from a name."""
    name = name.strip().lower()
    name = ''.join(
        c for c in unicodedata.normalize('NFD', name)
        if unicodedata.category(c) != 'Mn'
    )
    return name


def generate_variants(full_name):
    """Generate username variants from a full name or an email address.

    For 'Tony Blanco' produces:
      tonyblanco, tony_blanco, tony-blanco, tony.blanco, tony, blanco

    For 'tony.blanco@gmail.com' the local part is used:
      tony.blanco, tonyblanco, tony_blanco, tony-blanco, tony, blanco
    """
    raw = full_name.strip()
    is_email = '@' in raw
    base = raw.split('@')[0] if is_email else raw
    local_part = base.lower() if is_email else None

    name = normalize_name(base)
    # Split into words by non-letter characters
    words = re.split(r'[^a-z]+', name)
    words = [w for w in words if w]

    if not words:
        return [name]

    variants = set()

    # Full combinations
    joined = ''.join(words)
    variants.add(joined)
    variants.add('_'.join(words))
    variants.add('-'.join(words))
    variants.add('.'.join(words))

    # First name and last name alone
    variants.add(words[0])
    variants.add(words[-1])

    # CamelCase
    variants.add(words[0] + ''.join(w.capitalize() for w in words[1:]))

    # Original input cleaned (e.g. if user typed 'TonyBlanco' already)
    if re.fullmatch(r'[a-z_\-.]+', name):
        variants.add(name)

    # Email-specific: keep the local part exactly as typed (dots preserved)
    if is_email and local_part and re.fullmatch(r'[a-z_\-.]+', local_part):
        variants.add(local_part)

    # Drop any variant that is empty or only symbols
    variants = {v for v in variants if v and re.fullmatch(r'[a-z_\-.]+', v)}

    return list(variants)


@app.route('/search-variants', methods=['POST'])
def search_variants():
    """Start a variant search in the background and return a search_id."""
    full_name = request.form.get('fullname', '').strip()
    if not full_name:
        return jsonify({'success': False, 'error': 'Nombre requerido'})

    variants = generate_variants(full_name)

    # Optional custom variants supplied by the user (comma/newline separated)
    extra_raw = request.form.get('extra', '').strip()
    if extra_raw:
        extras = [re.sub(r'\s+', '', e) for e in re.split(r'[,;\n]+', extra_raw) if e.strip()]
        for e in extras:
            if e and e not in variants:
                variants.append(e)

    search_id = uuid.uuid4().hex

    search_progress[search_id] = {
        'status': 'running',
        'fullname': full_name,
        'total': len(variants),
        'completed': [],
        'variants': variants,
    }

    threading.Thread(
        target=run_variants_background,
        args=(search_id,),
        daemon=True,
    ).start()

    return jsonify({
        'success': True,
        'search_id': search_id,
        'total': len(variants),
        'variants': [{'username': v, 'status': 'pending', 'found': 0} for v in variants],
    })


def run_variants_background(search_id):
    """Run all variant searches in parallel, updating live progress."""
    prog = search_progress[search_id]
    full_name = prog['fullname']
    variants = prog['variants']
    variant_results = []
    lock = threading.Lock()

    def search_one(v):
        res = run_sherlock_search(v, 5)
        entry = {
            'username': v,
            'found': res.get('stats', {}).get('found', 0),
            'checked': res.get('stats', {}).get('total_checked', 0),
            'success': res.get('success', False),
            'error': res.get('error'),
            'cached': res.get('cached', False),
        }
        with lock:
            variant_results.append(entry)
            prog['completed'].append(entry)
        return entry

    workers = min(len(variants), 8)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(search_one, v): v for v in variants}
        for future in as_completed(future_map):
            try:
                future.result()
            except Exception:
                pass

    variant_results.sort(key=lambda v: v['found'], reverse=True)

    # Cross-variant comparison: which sites match across ALL variants
    successful = [v['username'] for v in variant_results if v['success']]
    site_matches = {}
    for uname in successful:
        for r in last_results.get(uname, []):
            if r['exists'] == 'Claimed':
                site_matches.setdefault(r['site_name'], set()).add(uname)

    comparison = []
    for site, matched in site_matches.items():
        comparison.append({
            'site': site,
            'matched': sorted(matched),
            'all': len(successful) > 0 and len(matched) == len(successful),
            'count': len(matched),
        })
    comparison.sort(key=lambda c: (-c['all'], -c['count'], c['site'].lower()))

    payload = {
        'success': True,
        'fullname': full_name,
        'variants': variant_results,
        'comparison': comparison,
    }

    # Cache the matrix for export
    last_comparison[full_name] = {
        'fullname': full_name,
        'variants': [v['username'] for v in variant_results],
        'comparison': comparison,
    }

    variant_final_results[search_id] = payload
    prog['status'] = 'done'


@app.route('/search-progress/<search_id>')
def search_progress_endpoint(search_id):
    """Return live progress for an async variant search."""
    prog = search_progress.get(search_id)
    if not prog:
        return jsonify({'success': False, 'error': 'Búsqueda no encontrada'}), 404

    variants_list = []
    for v in prog['variants']:
        done = next((c for c in prog['completed'] if c['username'] == v), None)
        if done:
            variants_list.append({
                'username': v,
                'status': 'done',
                'found': done['found'],
                'cached': done.get('cached', False),
            })
        else:
            variants_list.append({'username': v, 'status': 'pending', 'found': 0})

    return jsonify({
        'success': True,
        'search_id': search_id,
        'status': prog['status'],
        'fullname': prog['fullname'],
        'total': prog['total'],
        'completed': len(prog['completed']),
        'variants': variants_list,
    })


@app.route('/search-variants-result/<search_id>')
def search_variants_result(search_id):
    """Return the final result of a completed async variant search."""
    payload = variant_final_results.get(search_id)
    if not payload:
        return jsonify({'success': False, 'error': 'Resultado no listo'}), 404
    return jsonify(payload)


@app.route('/search-multi', methods=['POST'])
def search_multi():
    """Search several usernames at once (comma/newline separated)."""
    usernames_raw = request.form.get('usernames', '').strip()
    if not usernames_raw:
        return jsonify({'success': False, 'error': 'Usernames requeridos'})

    usernames = [re.sub(r'\s+', '', u) for u in re.split(r'[,;\n]+', usernames_raw) if u.strip()]
    if not usernames:
        return jsonify({'success': False, 'error': 'Usernames requeridos'})

    multi_results = []
    workers = min(len(usernames), 8)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(run_sherlock_search, u, 5): u for u in usernames}
        for future in as_completed(future_map):
            u = future_map[future]
            try:
                res = future.result()
            except Exception as e:
                res = {'success': False, 'error': str(e)}
            multi_results.append({
                'username': u,
                'found': res.get('stats', {}).get('found', 0),
                'checked': res.get('stats', {}).get('total_checked', 0),
                'success': res.get('success', False),
                'error': res.get('error'),
                'cached': res.get('cached', False),
            })

    multi_results.sort(key=lambda v: v['found'], reverse=True)

    return jsonify({
        'success': True,
        'mode': 'multi',
        'fullname': ', '.join(usernames),
        'variants': multi_results,
    })


@app.route('/export/comparison/csv/<path:fullname>')
def export_comparison_csv(fullname):
    """Export the cross-variant comparison matrix as CSV."""
    data = last_comparison.get(fullname)
    if not data:
        return jsonify({'error': 'No hay comparación en caché. Busca primero.'}), 404

    output = io.StringIO()
    writer = csv.writer(output)
    header = ['Sitio'] + data['variants'] + ['Coincidencias']
    writer.writerow(header)
    for c in data['comparison']:
        row = [c['site']]
        for u in data['variants']:
            row.append('Si' if u in c['matched'] else 'No')
        row.append(f"{c['count']}/{len(data['variants'])}")
        writer.writerow(row)

    mem = io.BytesIO()
    mem.write(output.getvalue().encode('utf-8'))
    mem.seek(0)

    return send_file(
        mem,
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'comparacion_{fullname}.csv'
    )


@app.route('/export/comparison/pdf/<path:fullname>')
def export_comparison_pdf(fullname):
    """Export the cross-variant comparison matrix as PDF."""
    data = last_comparison.get(fullname)
    if not data:
        return jsonify({'error': 'No hay comparación en caché. Busca primero.'}), 404

    variants = data['variants']
    comparison = data['comparison']

    pdf = FPDF(orientation='L')
    pdf.add_page()

    # Title
    pdf.set_font('Helvetica', 'B', 16)
    pdf.cell(0, 10, f'Comparaci\u00f3n entre variantes: {fullname}', new_x="LMARGIN", new_y="NEXT", align='C')
    pdf.ln(3)

    # Header
    pdf.set_font('Helvetica', 'B', 8)
    pdf.set_fill_color(108, 92, 231)
    pdf.set_text_color(255, 255, 255)
    site_w = 60
    variant_w = max(22, int((pdf.w - pdf.l_margin - pdf.r_margin - site_w - 25) / len(variants)))
    count_w = 25
    pdf.cell(site_w, 8, 'Sitio', border=1, fill=True, align='C')
    for v in variants:
        pdf.cell(variant_w, 8, v[:16], border=1, fill=True, align='C')
    pdf.cell(count_w, 8, 'Coincidencias', border=1, fill=True, align='C')
    pdf.ln()

    # Rows
    pdf.set_font('Helvetica', '', 7)
    pdf.set_text_color(0, 0, 0)
    for c in comparison:
        if c['all']:
            pdf.set_fill_color(204, 251, 241)
        else:
            pdf.set_fill_color(255, 255, 255)
        pdf.cell(site_w, 6, c['site'][:40], border=1, fill=True)
        for u in variants:
            mark = 'Si' if u in c['matched'] else ''
            pdf.cell(variant_w, 6, mark, border=1, fill=True, align='C')
        pdf.cell(count_w, 6, f"{c['count']}/{len(variants)}", border=1, fill=True, align='C')
        pdf.ln()

    mem = io.BytesIO()
    pdf_bytes = pdf.output()
    # fpdf2 returns bytearray; normalize to bytes
    mem.write(bytes(pdf_bytes) if isinstance(pdf_bytes, (bytes, bytearray)) else pdf_bytes.encode('latin-1'))
    mem.seek(0)

    return send_file(
        mem,
        mimetype='application/pdf',
        as_attachment=True,
        download_name=f'comparacion_{fullname}.pdf'
    )


@app.route('/results/<username>')
def cached_results(username):
    """Return cached results for a username (fast path after a variant search)."""
    # Prefer the persistent on-disk cache, fall back to the in-memory one.
    cached, cached_at = cache_get(username)
    results = None
    if cached is not None:
        results = [dict(r) for r in cached]
    elif username in last_results:
        results = [dict(r) for r in last_results[username]]
        cached_at = None

    if not results:
        return jsonify({'success': False, 'error': 'Sin resultados en caché'}), 404

    # Make sure no stale %20 URLs leak out of the cache
    sanitize_result_urls(results)

    stats = compute_stats(results)

    return jsonify({
        'success': True,
        'username': username,
        'results': results,
        'stats': stats,
        'cached': True,
        'cached_at': cached_at,
    })


@app.route('/export/csv/<username>')
def export_csv(username):
    """Export results as CSV file"""
    results = last_results.get(username, [])
    if not results:
        return jsonify({'error': 'No results found. Search first.'}), 404

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Sitio', 'URL Main', 'URL Usuario', 'Estado', 'HTTP Status', 'Tiempo (s)'])
    for r in results:
        writer.writerow([
            r['site_name'],
            r['url_main'],
            r['url_user'],
            r['exists'],
            r['http_status'],
            r['response_time_s']
        ])

    mem = io.BytesIO()
    mem.write(output.getvalue().encode('utf-8'))
    mem.seek(0)

    return send_file(
        mem,
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'sherlock_{username}.csv'
    )


@app.route('/export/pdf/<username>')
def export_pdf(username):
    """Export results as PDF file"""
    results = last_results.get(username, [])
    if not results:
        return jsonify({'error': 'No results found. Search first.'}), 404

    found = sum(1 for r in results if r['exists'] == 'Claimed')
    not_found = sum(1 for r in results if r['exists'] == 'Available')

    pdf = FPDF()
    pdf.add_page()

    # Title
    pdf.set_font('Helvetica', 'B', 20)
    pdf.cell(0, 15, f'Sherlock Report: {username}', new_x="LMARGIN", new_y="NEXT", align='C')
    pdf.ln(5)

    # Stats
    pdf.set_font('Helvetica', '', 12)
    pdf.cell(0, 8, f'Total verificados: {len(results)} | Encontrados: {found} | No encontrados: {not_found}', new_x="LMARGIN", new_y="NEXT", align='C')
    pdf.ln(10)

    # Table header
    pdf.set_font('Helvetica', 'B', 10)
    pdf.set_fill_color(108, 92, 231)
    pdf.set_text_color(255, 255, 255)
    col_widths = [40, 70, 50, 25]
    headers = ['Sitio', 'URL', 'Estado', 'HTTP']
    for i, h in enumerate(headers):
        pdf.cell(col_widths[i], 8, h, border=1, fill=True, align='C')
    pdf.ln()

    # Table rows
    pdf.set_font('Helvetica', '', 8)
    pdf.set_text_color(0, 0, 0)
    fill = False
    for r in results:
        if r['exists'] == 'Claimed':
            pdf.set_fill_color(220, 252, 231)
        elif r['exists'] == 'Available':
            pdf.set_fill_color(243, 244, 246)
        else:
            pdf.set_fill_color(254, 226, 226)

        site = r['site_name'][:20]
        url = r['url_user'][:35] if r['url_user'] else 'N/A'
        status = r['exists']
        http_status = r['http_status']

        pdf.cell(col_widths[0], 6, site, border=1, fill=True)
        pdf.cell(col_widths[1], 6, url, border=1, fill=True)
        pdf.cell(col_widths[2], 6, status, border=1, fill=True, align='C')
        pdf.cell(col_widths[3], 6, str(http_status), border=1, fill=True, align='C')
        pdf.ln()
        fill = not fill

    mem = io.BytesIO()
    pdf_bytes = pdf.output()
    # fpdf2 returns bytearray; normalize to bytes
    mem.write(bytes(pdf_bytes) if isinstance(pdf_bytes, (bytes, bytearray)) else pdf_bytes.encode('latin-1'))
    mem.seek(0)

    return send_file(
        mem,
        mimetype='application/pdf',
        as_attachment=True,
        download_name=f'sherlock_{username}.pdf'
    )


if __name__ == '__main__':
    print("=" * 60)
    print("  Sherlock Web Interface")
    print("  Open http://localhost:5000 in your browser")
    print("=" * 60)
    # Read the port from the PORT env var when set (Render/Railway/Fly inject
    # this); default to 5000 for local development.
    port = int(os.environ.get('PORT', '5000'))
    # Use waitress (production WSGI server, multi-threaded) when available;
    # fall back to Flask's built-in dev server otherwise.
    try:
        from waitress import serve
        serve(app, host='0.0.0.0', port=port, threads=6)
    except ImportError:
        app.run(debug=False, host='0.0.0.0', port=port)
