"""Explicit coordinator CLI. No automatic mutations."""
import argparse
from .config import Settings
from .repository import Repository

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['register-worker', 'queue', 'dispatch'])
    parser.add_argument('value', nargs='?')
    arguments = parser.parse_args()
    repo = Repository(Settings.from_env())
    if arguments.operation == 'register-worker':
        worker = repo.create_worker(arguments.value)
        print('Worker ID:', worker['id'])
        print('Worker credential (shown once):', worker['token'])
    elif arguments.operation == 'queue':
        print('Job ID:', repo.enqueue(arguments.value))
    else:
        print(repo.dispatch())
