#!/bin/bash
set -e

echo "==> Instalando pybind11..."
pip install pybind11

echo "==> Detectando Python..."
PYBIND_INC=$(python3 -c "import pybind11; print(pybind11.get_include())")
PYTHON_INC=$(python3 -c "import sysconfig; print(sysconfig.get_path('include'))")
OS=$(uname -s)
WORKDIR=$(pwd)

echo "  Diretório: $WORKDIR"
echo "  Sistema: $OS"

if [ "$OS" = "Darwin" ]; then
    c++ -O3 -shared -fPIC -std=c++17 \
        -I"$PYTHON_INC" -I"$PYBIND_INC" \
        -undefined dynamic_lookup \
        evaluator.cpp -o evaluator.so
else
    LIBPYTHON=$(python3-config --ldflags 2>/dev/null || echo "")
    c++ -O3 -shared -fPIC -std=c++17 \
        -I"$PYTHON_INC" -I"$PYBIND_INC" \
        evaluator.cpp -o evaluator.so $LIBPYTHON
fi

echo "==> evaluator.so criado em: $(pwd)/evaluator.so"
echo "==> Testando..."
python3 -c "
import sys, os
sys.path.insert(0, os.getcwd())
import evaluator
r = evaluator.monte_carlo(['As','Ah'],[],1,500)
print(f'  ✓  C++ OK — AA win: {r[0]}%')
"
echo "==> Pronto!"
