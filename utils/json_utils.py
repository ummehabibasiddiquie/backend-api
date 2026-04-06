import json

def to_db_json(value, *, allow_single=False):
    """
    For JSON column update/insert.

    Accepts:
      - list/tuple -> json string
      - dict -> json string
      - str JSON -> validated then returned normalized
      - int -> if allow_single True => [int]

    Returns:
      - None if value is None
      - JSON string otherwise
    """
    if value is None:
        return None

    # If frontend sends list/dict
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value)

    # If frontend sends already JSON string
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return json.dumps([]) if allow_single else json.dumps({})
        try:
            parsed = json.loads(v)
            return json.dumps(parsed)
        except Exception:
            raise ValueError("Invalid JSON format")

    # If single number allowed
    if isinstance(value, (int, float)) and allow_single:
        return json.dumps([int(value)])

    raise ValueError("Invalid JSON value type")
