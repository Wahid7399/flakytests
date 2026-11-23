#!/bin/bash
# Reduced-scale campaign (full spec = 5000 for every count; see README.md for why it is reduced).
cd "$(dirname "$0")/.."
S=harness/study.py
python3 $S suite semantic-kernel --runs ${SK_SUITE:-20}
python3 $S isolation semantic-kernel --reps ${SK_ISO:-10}
python3 $S aggregate semantic-kernel
python3 $S suite openai-agents-python --runs ${OA_SUITE:-5}
python3 $S isolation openai-agents-python --reps ${OA_ISO:-5}
python3 $S aggregate openai-agents-python
echo ALL_DONE
