#!/bin/bash
OGDIR=$PWD
BASEDIR=$(dirname "$0")
cd "$BASEDIR"

# Look for the .env file
while [[ "$PWD" != "$HOME" && ! -e .env ]]; do cd ..; done
if [[ -e .env ]]; then
  set -a; source .env; set +a
  echo "Using .env file found in $PWD"
else
  echo "CANCELED"
  echo "  No .env file found in the directory tree."
  echo "  Please setup the .env file and retry"
  exit 1
fi

# Derive PROJECT_PATH from script location if not set in .env
if [[ -z "$PROJECT_PATH" ]]; then
  PROJECT_PATH="$(cd "$BASEDIR/.." && pwd)"
  echo "PROJECT_PATH not set in .env, derived: $PROJECT_PATH"
else
  PROJECT_PATH="${PROJECT_PATH//\'/}"
  PROJECT_PATH="${PROJECT_PATH//\"/}"
  echo "PROJECT_PATH: $PROJECT_PATH"
fi

# Activate the virtual environment
cd "$PROJECT_PATH"
echo "Activating .venv in $PWD"

if [[ "$OSTYPE" == "msys" ]] || [[ "$OSTYPE" == "cygwin" ]] || [[ "$OSTYPE" == "win32" ]]; then
    PYCOMMAND="python"
    echo "Running on Windows"
    source ".venv/Scripts/activate"
else
    PYCOMMAND="python3"
    echo "Running on Unix"
    source ".venv/bin/activate"
fi

# First argument = python file
PYFILE="$1"

if [[ -z "$PYFILE" ]]; then
  echo "ERROR: No python file provided."
  echo "Usage: ./run.sh script.py [args...]"
  deactivate
  cd "$OGDIR"
  exit 1
fi

shift  # remove first argument so $@ now contains only script args

cd "$PROJECT_PATH/src"
echo "Running $PYFILE with args: $@"
$PYCOMMAND "$PYFILE" "$@"

deactivate
echo ".venv deactivated"
cd "$OGDIR"