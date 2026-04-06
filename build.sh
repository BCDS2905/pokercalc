#!/bin/bash
set -e

# Usa o Python do ambiente virtual se existir (Render usa .venv)
if [ -f ".venv/bin/pip" ]; then
    PIP=".venv/bin/pip"
    PYTHON=".venv/bin/python"
    echo "==> Usando .venv do Render"
else
    PIP="pip"
    PYTHON="python3"
    echo "==> Usando Python do sistema"
fi

echo "==> Instalando dependências..."
$PIP install -r requirements.txt

echo "==> Compilando evaluator.cpp..."
PYBIND_INC=$($PYTHON -c "import pybind11; print(pybind11.get_include())")
PYTHON_INC=$($PYTHON -c "import sysconfig; print(sysconfig.get_path('include'))")
OS=$(uname -s)

echo "  Sistema: $OS"
echo "  Python include: $PYTHON_INC"

if [ "$OS" = "Darwin" ]; then
    c++ -O3 -shared -fPIC -std=c++17 \
        -I"$PYTHON_INC" -I"$PYBIND_INC" \
        -undefined dynamic_lookup \
        evaluator.cpp -o evaluator.so
else
    LIBPYTHON=$($PYTHON-config --ldflags 2>/dev/null || python3-config --ldflags 2>/dev/null || echo "")
    c++ -O3 -shared -fPIC -std=c++17 \
        -I"$PYTHON_INC" -I"$PYBIND_INC" \
        evaluator.cpp -o evaluator.so $LIBPYTHON
fi

echo "==> evaluator.so criado em: $(pwd)/evaluator.so"

echo "==> Testando..."
$PYTHON -c "
import sys, os
sys.path.insert(0, os.getcwd())
import evaluator
r = evaluator.monte_carlo(['As','Ah'],[],1,500)
print(f'  ✓  C++ OK — AA win: {r[0]}%')
"
echo "==> Baixando fontes (Google Fonts → static/fonts)..."
$PYTHON - <<'PYEOF'
import urllib.request, re, os, sys

os.makedirs('static/fonts', exist_ok=True)

FONT_MAP = {
    ('Rajdhani',       '400'): 'rajdhani-400.woff2',
    ('Rajdhani',       '500'): 'rajdhani-500.woff2',
    ('Rajdhani',       '600'): 'rajdhani-600.woff2',
    ('Rajdhani',       '700'): 'rajdhani-700.woff2',
    ('JetBrains Mono', '400'): 'jetbrainsmono-400.woff2',
    ('JetBrains Mono', '500'): 'jetbrainsmono-500.woff2',
    ('JetBrains Mono', '700'): 'jetbrainsmono-700.woff2',
}

headers = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
gf_url  = 'https://fonts.googleapis.com/css2?family=Rajdhani:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap'

try:
    req = urllib.request.Request(gf_url, headers=headers)
    css = urllib.request.urlopen(req, timeout=30).read().decode()
except Exception as e:
    print(f'  ⚠  Fontes não baixadas (continuando sem elas): {e}', file=sys.stderr)
    sys.exit(0)

# Percorre segmentos separados por /* subset-name */ e pega só o bloco "latin"
segments = re.split(r'/\*\s*([a-z][a-z0-9-]*)\s*\*/', css)
downloaded = 0
i = 1
while i + 1 < len(segments):
    subset, block = segments[i].strip(), segments[i + 1]
    i += 2
    if subset != 'latin':
        continue
    for m in re.finditer(r'@font-face\s*\{([^}]+)\}', block, re.DOTALL):
        c = m.group(1)
        fm = re.search(r"font-family:\s*['\"]([^'\"]+)", c)
        wm = re.search(r'font-weight:\s*(\d+)', c)
        um = re.search(r'url\((https://fonts\.gstatic\.com[^)]+\.woff2)\)', c)
        if not (fm and wm and um):
            continue
        key = (fm.group(1), wm.group(1))
        if key not in FONT_MAP:
            continue
        fname = FONT_MAP[key]
        path  = f'static/fonts/{fname}'
        if not os.path.exists(path):
            urllib.request.urlretrieve(um.group(1), path)
            print(f'  ✓ {fname}')
        else:
            print(f'  ↩ {fname} (cache)')
        downloaded += 1

print(f'  ✓ {downloaded}/{len(FONT_MAP)} fontes prontas')
PYEOF

echo "==> Minificando JS/CSS para produção..."
$PYTHON - <<'PYEOF'
import os, sys
try:
    import rjsmin, rcssmin
except ImportError:
    print('  ⚠  rjsmin/rcssmin não instalados — pulando minificação')
    sys.exit(0)

targets = [
    ('static/js/main.js',   'static/js/main.min.js',   rjsmin.jsmin),
    ('static/js/pwa.js',    'static/js/pwa.min.js',    rjsmin.jsmin),
    ('static/css/main.css', 'static/css/main.min.css', rcssmin.cssmin),
    ('static/css/pages.css','static/css/pages.min.css',rcssmin.cssmin),
    ('static/css/fonts.css','static/css/fonts.min.css',rcssmin.cssmin),
]
for src, dst, fn in targets:
    if not os.path.exists(src):
        print(f'  ↩ {src} (não existe)')
        continue
    raw = open(src).read()
    mini = fn(raw)
    open(dst,'w').write(mini)
    rs, ms = len(raw), len(mini)
    pct = 100*(1-ms/rs) if rs else 0
    print(f'  ✓ {dst}  {rs:>8} → {ms:>8} bytes  (-{pct:.1f}%)')
PYEOF

echo "==> Pronto!"
