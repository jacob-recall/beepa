"""Validated installation endpoints supplied by the owning user's LaunchAgent."""
import os


def _port(key, default):
    value = int(os.environ.get(key, default))
    if not 1024 <= value <= 65531:
        raise ValueError('Invalid loopback helper port')
    return value


def app_origins():
    port = _port('BEEPA_APP_PORT', 8011)
    return ('http://127.0.0.1:%d' % port, 'http://localhost:%d' % port)


def helper_port(name):
    key, default = {'gmessages': ('BEEPA_GMESSAGES_PORT', 8020),
                    'session': ('BEEPA_SESSION_PORT', 8021)}[name]
    return _port(key, default)
