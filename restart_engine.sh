#!/bin/bash
cd ~/Documents/quantrift_index_future
pkill -f live_engine.py 2>/dev/null
sleep 5
nohup venv/bin/python live_engine.py --port 4001 >> logs/live_20260610.log 2>&1 &
echo "Wed Jun 10 22:31:53 PDT 2026: 引擎已重启 PID=0" >> logs/restart.log
