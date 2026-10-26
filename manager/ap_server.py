#!/usr/bin/env python3
"""Starts Archipelago's MultiServer.py for one game, in the Archipelago Python environment.

The game's passwords do not go on the command line, where every process on the machine can read them (`ps`): the
manager sends them as the first line of the server's console, and this adds them to the arguments inside the process.

Usage: ap_server.py PATH/TO/MultiServer.py [ARGS...]
       first line on stdin: {"password": "...", "server_password": "..."}; the rest is the server's console
"""
import json
import os
import runpy
import sys

script, *args = sys.argv[1:]
passwords = json.loads(sys.stdin.readline())
# As `python MultiServer.py ...` has it: the script's folder first on the path, the script first in argv. The console
# reads on from the same sys.stdin, so lines that came along with the first one are not lost.
sys.path[0] = os.path.dirname(os.path.abspath(script))
sys.argv = [script, *args, f"--password={passwords['password']}", f"--server_password={passwords['server_password']}"]
runpy.run_path(script, run_name="__main__")
