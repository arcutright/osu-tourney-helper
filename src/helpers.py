def value_or_fallback(x, fallback):
    """ return fallback if x is None else x """
    return fallback if x is None else x

def try_int(x, fallback=None):
    if x is None:
        return fallback
    try:
        return int(x)
    except Exception:
        return fallback
