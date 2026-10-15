"""Run reviewed migrations via local peer authentication as postgres only."""
import os
import pwd
from pathlib import Path
import sys

if pwd.getpwuid(os.geteuid()).pw_name != 'postgres':
    raise SystemExit('Run this helper as the postgres OS account.')
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'shared-scraper'))
from migrations.__main__ import migrate
migrate('postgresql:///datascraper?host=/var/run/postgresql&user=postgres')
