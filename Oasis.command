#!/bin/zsh
# Double-click entry for this checkout's offline engineering workflow.
cd "$(dirname "$0")" || exit 2
if [[ ! -x .venv/bin/python ]]; then
  print 'Locked environment missing. Run make install first.'
  exit 2
fi
.venv/bin/python -m scripts.run_lp_research --workflow configs/workflow.current.json --output reports/runs/current --resume
run_result=$?
if [[ $run_result -eq 0 || $run_result -eq 3 ]]; then
  open reports/runs/current/report.html
fi
exit $run_result
