#!/usr/bin/env bash
# probe_pandas.sh -- find out WHY `import pandas` segfaults on this machine and
# which package versions work. Safe: it only creates throw-away virtualenvs in
# /tmp and never touches your project's .venv.
#
#   bash tools/probe_pandas.sh 2>&1 | tee probe_output.txt
#
# Then paste probe_output.txt (or the whole terminal output) back into the chat.

UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
[ -x "$UV" ] || { echo "uv not found; install it first or adjust UV= in this script"; exit 1; }
if [ -n "$VIRTUAL_ENV" ]; then PY_CURRENT="$VIRTUAL_ENV/bin/python"; else PY_CURRENT="python"; fi

echo "=================== 1. ENVIRONMENT ==================="
grep -E '^(PRETTY_NAME|ID)=' /etc/os-release 2>/dev/null
echo "kernel      : $(uname -r)"
echo "glibc       : $(ldd --version 2>&1 | head -1)"
echo "pacman      : $(command -v pacman || echo 'NOT FOUND in this shell')"
echo "sandbox vars: FLATPAK_ID='${FLATPAK_ID}' SNAP='${SNAP}' container='${container}' CONTAINER_ID='${CONTAINER_ID}'"
echo "CPU         : $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2)"
echo "CPU flags   : $(grep -m1 '^flags' /proc/cpuinfo | tr ' ' '\n' | grep -E '^(avx|avx2|avx512f|sse4_2|bmi2|fma)$' | tr '\n' ' ')"
echo "python      : $("$PY_CURRENT" -c 'import sys,platform; print(sys.version.split()[0], platform.python_implementation(), sys.executable)')"
echo "packages    : $("$UV" pip list 2>/dev/null | grep -i -E '^(numpy|pandas|pyarrow|streamlit) ' | tr -s ' ' | tr '\n' ';')"

echo
echo "=================== 2. WHERE DOES THE CURRENT ENVIRONMENT CRASH? ==================="
"$PY_CURRENT" -X faulthandler -c "import pandas" 2>&1 | grep -E "Fatal|pandas/|pyarrow/|numpy/|\.so|Extension modules" | head -12
echo "(exit code ${PIPESTATUS[0]}; 139 = segmentation fault, 0 = fine)"

if [ "$1" = "--env-only" ]; then exit 0; fi

echo
echo "=================== 3. TRY OTHER VERSION COMBINATIONS (throw-away venvs) ==================="
i=0
while IFS= read -r combo; do
  [ -z "$combo" ] && continue
  i=$((i+1)); d="/tmp/pandas_probe_$i"; rm -rf "$d"
  "$UV" venv -q --python 3.12 "$d" >/dev/null 2>&1
  if ! "$UV" pip install -q --python "$d/bin/python" $combo >/tmp/probe_install_$i.log 2>&1; then
    echo "[$i] INSTALL FAILED : $combo   (see /tmp/probe_install_$i.log)"; continue
  fi
  "$d/bin/python" -c "import pandas; print(pandas.__version__)" >/tmp/probe_out_$i.log 2>&1
  rc=$?
  if [ $rc -eq 0 ]; then echo "[$i] OK             : $combo"
  else echo "[$i] CRASH (exit $rc): $combo"; fi
done <<'COMBOS'
numpy==2.4.4 pandas==3.0.2 pyarrow==26.0.0
numpy==2.2.6 pandas==2.3.3 pyarrow==21.0.0
numpy==2.4.4 pandas==3.0.2
COMBOS
echo
echo "legend: OK = pandas imports fine with exactly those versions."
echo "Done. Paste everything above into the chat."