"""Conservative phone identities using pinned libphonenumber metadata.

An extension/post-dial target is deliberately not eligible for automatic
person matching. Bare provider IDs require an explicit provider=True call;
freeform national numbers require a region. Metadata validity does not prove
that a number is assigned or that two handles belong to the same human.
"""
import re


def metadata():
    try:
        import phonenumbers
    except ImportError:
        raise RuntimeError('Phone metadata is unavailable; run setup.sh or update.sh --apply with the managed Python runtime') from None
    return phonenumbers


def normalize_phone(raw, region=None, provider=False):
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value or len(value) > 128:
        return None
    # Do not map vanity letters, discard extension digits, collapse embedded
    # plus signs, or strip a post-dial suffix into a different person's number.
    if re.search(r'[^0-9+().\s/-]', value) or value.count('+') > 1 or ('+' in value and not value.startswith('+')):
        return None
    pn = metadata()
    if provider:
        if not value.isascii() or not value.isdigit():
            return None
        value = '+' + value
    elif value.startswith('00'):
        value = '+' + value[2:]
    if region is not None:
        region = str(region).upper()
        # Compatibility for the previous importer API's calling-code argument.
        if region.isdigit():
            region = pn.region_code_for_country_code(int(region))
        if region not in pn.SUPPORTED_REGIONS:
            region = None
    if not value.startswith('+') and not region:
        return None
    try:
        parsed = pn.parse(value, region)
    except pn.NumberParseException:
        return None
    if parsed.extension or not pn.is_valid_number(parsed):
        return None
    return pn.format_number(parsed, pn.PhoneNumberFormat.E164)
