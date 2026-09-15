"""Display/archive timestamps. Never use these for send authorization."""
import math

ORIGIN_TS = 'com.jkali.origin_ts'
TS_SOURCE = 'com.beepa.timestamp_source'
CORRECTION_TYPE = 'com.beepa.timestamp_correction'


def valid_ts(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and 0 < value <= 8640000000000000 and math.isfinite(value)
            and value == int(value))


def message_ts(event):
    content = event.get('content') or {}
    value = content.get(ORIGIN_TS)
    return int(value) if valid_ts(value) else event.get('origin_server_ts')


def stamp_timestamp(content, event):
    value = message_ts(event)
    content[ORIGIN_TS] = value
    original = event.get('content') or {}
    content[TS_SOURCE] = (original.get(TS_SOURCE) or 'source_metadata'
                          if valid_ts(original.get(ORIGIN_TS)) else 'matrix_event')


def native_metadata(message):
    """Pinned imessage-cli JSON supplies Unix milliseconds in timestamp."""
    value = message.get('timestamp')
    if valid_ts(value):
        return {ORIGIN_TS: int(value), TS_SOURCE: 'imessage'}
    return {TS_SOURCE: 'unknown'}
