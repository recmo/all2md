# mdstore's declaration API. Loaded once, before the template's blocks.
_definition = {"structure": {"additional_sections": True}, "sections": []}
_locations = {}
_validators = []
_change_validators = []

def configure(**settings):
    for key, value in settings.items():
        if key in _definition and key not in ["structure", "sections"]:
            fail("duplicate configuration: " + key)
        _definition[key] = value

def _field(kind, required, nullable, constraints):
    schema = {"type": [kind, "null"] if nullable else kind}
    schema.update(constraints)
    return {"schema": schema, "required": required}

def string(required=False, nullable=False, min_length=None, max_length=None, pattern=None):
    constraints = {}
    if min_length != None:
        constraints["minLength"] = min_length
    if max_length != None:
        constraints["maxLength"] = max_length
    if pattern != None:
        constraints["pattern"] = pattern
    return _field("string", required, nullable, constraints)

def integer(required=False, nullable=False, minimum=None, maximum=None):
    constraints = {}
    if minimum != None:
        constraints["minimum"] = minimum
    if maximum != None:
        constraints["maximum"] = maximum
    return _field("integer", required, nullable, constraints)

def boolean(required=False, nullable=False):
    return _field("boolean", required, nullable, {})

def enum(values, required=False, nullable=False):
    return {"schema": {"enum": values + ([None] if nullable else [])}, "required": required}

def list_of(item, required=False, nullable=False, unique=False, min_items=0):
    return _field("array", required, nullable, {"items": item["schema"], "uniqueItems": unique, "minItems": min_items})

def frontmatter(fields=None, allow_extra=False, **named_fields):
    if "frontmatter" in _definition:
        fail("frontmatter is already declared")
    fields = dict(fields or {})
    fields.update(named_fields)
    _locations["frontmatter"] = _location()
    _definition["frontmatter"] = {
        "type": "object",
        "properties": {key: value["schema"] for key, value in fields.items()},
        "required": [key for key, value in fields.items() if value["required"]],
        "additionalProperties": allow_extra,
    }

def dated_list(timestamp="rfc3339", order="ascending", min_items=1, allow_equal_timestamps=True):
    return {"timestamp": timestamp, "order": order, "min_items": min_items, "allow_equal_timestamps": allow_equal_timestamps}

def section(heading, level=2, required=False, content=None, instructions="", **rules):
    if _definition["sections"] and _definition["structure"].get("level") != level:
        fail("sibling sections must use the same heading level")
    _definition["structure"]["level"] = level
    rules = dict(rules)
    rules["required"] = required
    if type(content) == "dict":
        rules["dated_list"] = content
        rules["content"] = "list"
    elif content != None:
        rules["content"] = content
    _locations[heading] = _location()
    _definition["sections"].append({"heading": heading, "instructions": instructions, "rules": rules})

def filename(pattern, serial_scope=[]):
    if "filename" in _definition:
        fail("filename is already declared")
    _locations["filename"] = _location()
    _definition["filename"] = {"pattern": pattern, "serial_scope": serial_scope}

def validate(callback):
    if type(callback) != "function":
        fail("validate expects a function")
    _validators.append(callback)

def validate_change(callback):
    if type(callback) != "function":
        fail("validate_change expects a function")
    _change_validators.append(callback)

def require(condition, message, field=None, at=None):
    if not condition:
        location = ""
        if field != None:
            location = "[mdstore-field:" + field + "] "
        elif at != None:
            location = "[mdstore-line:" + str(at.line) + "] "
        fail(location + message)

def mdstore_validate(doc):
    for callback in _validators:
        if callback(doc) != None:
            fail("validators must return None; use require() to report a failure")

def mdstore_validate_change(before, after):
    for callback in _change_validators:
        if callback(before, after) != None:
            fail("validators must return None; use require() to report a failure")


def mdstore_schema():
    return {"definition": _definition, "locations": _locations, "changes": len(_change_validators)}
