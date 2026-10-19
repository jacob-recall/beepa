#!/usr/bin/env python3
"""API client for master notifications; never uses or automates a browser."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE = 'http://127.0.0.1:8019'


def call(base, token, data):
    req = urllib.request.Request(base.rstrip('/') + '/admin/notifications',
        data=json.dumps(data).encode(), headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:
            message = json.load(error).get('error', 'Request failed')
        except (ValueError, AttributeError):
            message = 'Request failed'
        raise RuntimeError('HTTP %s: %s' % (error.code, message)) from None


def local_token(base):
    token = os.environ.get('BEEPA_MANAGER_TOKEN')
    if token:
        return token
    if base.rstrip('/') != DEFAULT_BASE:
        raise ValueError('Set BEEPA_MANAGER_TOKEN explicitly for a remote API; local credentials are never sent to a custom URL automatically.')
    # Reuse the authoritative parser; never print the bearer or put it on argv.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import enroll
    token = enroll._tokens().get('MASTER_MANAGER_TOKEN')
    if not token:
        raise ValueError('No manager token configured')
    return token


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['status','key','follow','unfollow','subscribe','unsubscribe','test','sync','delivery','seen'])
    parser.add_argument('--base', default=DEFAULT_BASE)
    parser.add_argument('--room')
    parser.add_argument('--all-messages', action='store_true')
    parser.add_argument('--device')
    parser.add_argument('--id')
    parser.add_argument('--request-id', help='Reuse the same ID when retrying one test; it will not send twice')
    parser.add_argument('--subscription-file', type=Path, help='Browser-generated PushSubscription JSON (keep private)')
    parser.add_argument('--wait-for', choices=['accepted','shown','clicked'])
    parser.add_argument('--timeout', type=float, default=45)
    args = parser.parse_args()
    data = {'action':args.action}
    for key in ('room','device','id','request_id'):
        value = getattr(args,key)
        if value is not None:
            data[key] = value
    if args.action == 'follow':
        data['all_messages'] = args.all_messages
    if args.subscription_file:
        data['subscription'] = json.loads(args.subscription_file.read_text())
    token = local_token(args.base)
    result = call(args.base, token, data)
    if args.wait_for:
        nid = result.get('notification_id') or (result.get('id') if args.action=='delivery' else None)
        if not nid:
            raise ValueError('--wait-for requires test or delivery')
        deadline = time.monotonic() + args.timeout
        while True:
            result = call(args.base, token, {'action':'delivery','id':nid})
            deliveries = result['deliveries']
            reached = lambda d: (d.get('clicked_at') is not None if args.wait_for=='clicked' else
                d.get('shown_at') is not None or d.get('clicked_at') is not None if args.wait_for=='shown' else
                d['state']=='accepted' or d.get('shown_at') is not None or d.get('clicked_at') is not None)
            if deliveries and all(reached(d) for d in deliveries):
                break
            if time.monotonic() >= deadline:
                print(json.dumps({'wait_timed_out':True,'notification':result},indent=2))
                return 1
            time.sleep(1)
    print(json.dumps(result,indent=2))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, ValueError, OSError) as error:
        print(str(error),file=sys.stderr)
        sys.exit(1)
