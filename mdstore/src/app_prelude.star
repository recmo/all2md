_collections = {}
_views = []
_actions = {}

def collection(name, query):
    if name in _collections:
        fail("duplicate collection: " + name)
    _collections[name] = query
    return name

def action(name, callback):
    if name in _actions:
        fail("duplicate action: " + name)
    _actions[name] = callback
    return name

def _binding(value):
    if type(value) != "string" or not (value in ["title", "path"] or value.startswith("/")):
        fail("field binding must be title, path, or a frontmatter JSON pointer")
    return value

def _view(kind, name, collection, bindings):
    if type(name) != "string" or not name or type(collection) != "string":
        fail("view name and collection must be strings")
    if any([v["name"] == name for v in _views]):
        fail("duplicate view: " + name)
    _views.append({"kind": kind, "name": name, "collection": collection, "bindings": bindings})

def table(name, collection, columns):
    if type(columns) != "list" or not columns:
        fail("table columns must be a nonempty list")
    _view("table", name, collection, {"columns": [_binding(c) for c in columns]})

def kanban(name, collection, group, columns=None, on_move=None):
    if columns != None and (type(columns) != "list" or any([type(c) != "string" for c in columns])):
        fail("kanban columns must be a list of strings")
    _view("kanban", name, collection, {"group": _binding(group), "columns": columns, "on_move": on_move})

def gantt(name, collection, start, end, group=None, dependencies=None):
    bindings = {"start": _binding(start), "end": _binding(end)}
    if group != None:
        bindings["group"] = _binding(group)
    if dependencies != None:
        bindings["dependencies"] = _binding(dependencies)
    _view("gantt", name, collection, bindings)

def update(doc, fields, timeline=None):
    return {"path": doc.path, "fields": fields, "timeline": timeline}

def mdstore_app_check():
    for view in _views:
        if view["bindings"].get("on_move") != None and view["bindings"]["on_move"] not in _actions:
            fail("unknown view action")
        if view["collection"] not in _collections:
            fail("unknown collection: " + view["collection"])

def mdstore_app_run(documents, action_name, event):
    mdstore_app_check()
    if action_name != None:
        if action_name not in _actions:
            fail("unknown action: " + action_name)
        return {"edits": _actions[action_name](documents, event)}
    by_path = {doc.path: doc for doc in documents}
    collections = {}
    for name, query in _collections.items():
        selected = query(documents)
        paths = []
        for doc in selected:
            if doc.path not in by_path or doc != by_path[doc.path]:
                fail("collections must return original document references")
            if doc.path in paths:
                fail("duplicate document in collection: " + doc.path)
            paths.append(doc.path)
        collections[name] = paths
    return {"collections": collections, "views": _views}
