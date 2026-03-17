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
echo "==> Pronto!"
