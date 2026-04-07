"""
Teste rápido de rotas e headers — simula requests sem subir servidor.
Uso: python3 test_routes.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from app import app
except Exception as e:
    print(f"❌ Falha ao importar app.py: {type(e).__name__}: {e}")
    sys.exit(1)

def check(name, cond, detail=''):
    mark = '✅' if cond else '❌'
    print(f"  {mark} {name}" + (f"  ({detail})" if detail else ''))
    return cond

def main():
    client = app.test_client()
    all_ok = True

    print("── GET / ──")
    r = client.get('/')
    all_ok &= check('status 200', r.status_code == 200, f'got {r.status_code}')
    body = r.get_data(as_text=True)
    all_ok &= check('contém <title>PokerCalc', '<title>PokerCalc' in body)
    all_ok &= check('ref /static/js/main.js', '/static/js/main.js' in body or '/static/js/main.min.js' in body)
    all_ok &= check('ref /static/css/main.css', '/static/css/main.css' in body or '/static/css/main.min.css' in body)
    all_ok &= check('ref /static/js/pwa.js', '/static/js/pwa.js' in body or '/static/js/pwa.min.js' in body)
    all_ok &= check('JSON-LD Schema.org', 'application/ld+json' in body and '"WebApplication"' in body)
    all_ok &= check('Open Graph', 'property="og:title"' in body)
    all_ok &= check('Twitter Card', 'name="twitter:card"' in body)
    all_ok &= check('FAQ container vazio', 'id="faq-container"' in body)
    has_csp = 'Content-Security-Policy' in r.headers
    all_ok &= check('CSP header presente', has_csp)
    if has_csp:
        csp = r.headers['Content-Security-Policy']
        has_unsafe_inline = "'unsafe-inline'" in csp.split('script-src')[1].split(';')[0] if 'script-src' in csp else False
        all_ok &= check("script-src inclui 'unsafe-inline' (necessário p/ onclick=)", has_unsafe_inline)
    all_ok &= check('HSTS (se ENV=production)', 'Strict-Transport-Security' in r.headers or os.environ.get('ENV','').lower() != 'production')
    all_ok &= check('X-Frame-Options: DENY', r.headers.get('X-Frame-Options') == 'DENY')
    all_ok &= check('X-Content-Type-Options: nosniff', r.headers.get('X-Content-Type-Options') == 'nosniff')
    all_ok &= check('Referrer-Policy', 'strict-origin' in r.headers.get('Referrer-Policy',''))

    print("\n── GET /static/css/main.css ──")
    r = client.get('/static/css/main.css')
    all_ok &= check('status 200', r.status_code == 200, f'got {r.status_code}')
    all_ok &= check('content-type text/css', 'css' in r.headers.get('Content-Type',''))

    print("\n── GET /static/js/main.js ──")
    r = client.get('/static/js/main.js')
    all_ok &= check('status 200', r.status_code == 200, f'got {r.status_code}')
    all_ok &= check('content-type javascript', 'javascript' in r.headers.get('Content-Type','').lower())

    print("\n── GET /static/data/content.json ──")
    r = client.get('/static/data/content.json')
    all_ok &= check('status 200', r.status_code == 200, f'got {r.status_code}')
    if r.status_code == 200:
        import json
        try:
            data = json.loads(r.get_data(as_text=True))
            all_ok &= check('JSON válido com .faq', 'faq' in data and isinstance(data['faq'], list))
        except Exception as e:
            all_ok &= check('JSON válido', False, str(e))

    print("\n── GET /robots.txt ──")
    r = client.get('/robots.txt')
    all_ok &= check('status 200', r.status_code == 200, f'got {r.status_code}')
    body = r.get_data(as_text=True)
    all_ok &= check('Disallow: /api/', 'Disallow: /api/' in body)
    all_ok &= check('Sitemap:', 'Sitemap:' in body)

    print("\n── GET /sitemap.xml ──")
    r = client.get('/sitemap.xml')
    all_ok &= check('status 200', r.status_code == 200, f'got {r.status_code}')
    body = r.get_data(as_text=True)
    all_ok &= check('contém pokercalc.com.br', 'pokercalc.com.br' in body)

    print("\n── GET /healthcheck ──")
    r = client.get('/healthcheck')
    all_ok &= check('status 200', r.status_code == 200, f'got {r.status_code}')

    print("\n── POST /calculate (AA pre-flop) ──")
    r = client.post('/calculate', json={
        'hole_cards': ['As','Ah'], 'board': [], 'opponents': 1, 'simulations': 1000
    })
    all_ok &= check('status 200', r.status_code == 200, f'got {r.status_code}')
    if r.status_code == 200:
        j = r.get_json()
        all_ok &= check('retorna job_id', 'job_id' in j)

    print("\n══════════════════════════════════")
    print("✅ TODOS OS TESTES PASSARAM" if all_ok else "❌ FALHAS DETECTADAS")
    print("══════════════════════════════════")
    return 0 if all_ok else 1

if __name__ == '__main__':
    sys.exit(main())
