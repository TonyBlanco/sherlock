#!/usr/bin/env python3
"""
Sherlock Web Interface
A simple Flask web application to search for usernames across social networks.
"""

from flask import Flask, render_template, request, jsonify, send_file
import subprocess
import os
import sys
import csv
import tempfile
import shutil
import io
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from fpdf import FPDF

app = Flask(__name__)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# In-memory cache for last search results
last_results = {}

# In-memory cache for the last cross-variant comparison matrix
last_comparison = {}


def run_sherlock_search(username, timeout=10):
    """Run Sherlock search and return results as JSON"""
    try:
        work_dir = tempfile.mkdtemp(prefix="sherlock_")

        cmd = [
            sys.executable, "-m", "sherlock_project.sherlock",
            "--local",
            "--timeout", str(timeout),
            "--csv",
            username
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=work_dir
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

        shutil.rmtree(work_dir, ignore_errors=True)

        stats = {
            'total_checked': len(results),
            'found': sum(1 for r in results if r['exists'] == 'Claimed'),
            'not_found': sum(1 for r in results if r['exists'] == 'Available'),
            'errors': sum(1 for r in results if r['exists'] not in ['Claimed', 'Available'])
        }

        # Cache results for export
        last_results[username] = results

        return {
            'success': True,
            'username': username,
            'results': results,
            'stats': stats,
            'output': result.stdout
        }

    except Exception as e:
        return {
            'success': False,
            'error': str(e),
            'username': username
        }


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/search', methods=['POST'])
def search():
    username = request.form.get('username', '').strip()
    if not username:
        return jsonify({'success': False, 'error': 'Username is required'})
    results = run_sherlock_search(username)
    return jsonify(results)


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
    """Search a full name across all its username variants."""
    full_name = request.form.get('fullname', '').strip()
    if not full_name:
        return jsonify({'success': False, 'error': 'Nombre requerido'})

    variants = generate_variants(full_name)
    variant_results = []

    # Run all variant searches in parallel (each spawns its own subprocess).
    # A shorter timeout keeps the parallel scan fast; slow sites get skipped.
    workers = min(len(variants), 8)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(run_sherlock_search, v, 5): v for v in variants}
        for future in as_completed(future_map):
            v = future_map[future]
            try:
                res = future.result()
            except Exception as e:
                res = {'success': False, 'error': str(e)}
            variant_results.append({
                'username': v,
                'found': res.get('stats', {}).get('found', 0),
                'checked': res.get('stats', {}).get('total_checked', 0),
                'success': res.get('success', False),
                'error': res.get('error'),
            })

    # Sort: found desc
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
    # Sites matching in ALL variants first, then by number of matches
    comparison.sort(key=lambda c: (-c['all'], -c['count'], c['site'].lower()))

    # Cache the matrix for export
    last_comparison[full_name] = {
        'fullname': full_name,
        'variants': [v['username'] for v in variant_results],
        'comparison': comparison,
    }

    return jsonify({
        'success': True,
        'fullname': full_name,
        'variants': variant_results,
        'comparison': comparison,
    })


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
    results = last_results.get(username)
    if not results:
        return jsonify({'success': False, 'error': 'Sin resultados en caché'}), 404

    stats = {
        'total_checked': len(results),
        'found': sum(1 for r in results if r['exists'] == 'Claimed'),
        'not_found': sum(1 for r in results if r['exists'] == 'Available'),
        'errors': sum(1 for r in results if r['exists'] not in ['Claimed', 'Available']),
    }

    return jsonify({
        'success': True,
        'username': username,
        'results': results,
        'stats': stats,
        'cached': True,
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
    app.run(debug=False, host='0.0.0.0', port=5000)
