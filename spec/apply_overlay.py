#!/usr/bin/env python3
"""Apply an OpenAPI Overlay (https://spec.openapis.org/overlay/v1.0.0.html) to a spec.

Usage: python spec/apply_overlay.py spec/consultas-externas.overlay.yaml

Reads the target document from the overlay's `extends` (relative to the overlay file),
applies every action in order, and writes the result next to the overlay as
`<name>.resolved.json`. An action whose `target` matches nothing is an error: it
means the upstream spec changed shape and the overlay needs a look.
"""

import json
import sys
from pathlib import Path

import yaml
from jsonpath_ng.ext import parse


def merge(target, update):
    """Overlay `update` semantics: objects merge recursively, arrays append, scalars replace."""
    if isinstance(target, dict) and isinstance(update, dict):
        for key, value in update.items():
            target[key] = merge(target.get(key), value) if key in target else value
        return target
    if isinstance(target, list):
        return target + (update if isinstance(update, list) else [update])
    return update


def parent_and_key(match):
    key = match.path.fields[0] if hasattr(match.path, "fields") else match.path.index
    return match.context.value, key


def apply(doc, overlay):
    for i, action in enumerate(overlay["actions"], 1):
        matches = parse(action["target"]).find(doc)
        if not matches:
            sys.exit(f"action {i}: target matched nothing: {action['target']}")
        for m in matches:
            if action.get("remove"):
                parent, key = parent_and_key(m)
                del parent[key]
            elif isinstance(m.value, dict):
                merge(m.value, json.loads(json.dumps(action["update"])))  # in place
            else:
                parent, key = parent_and_key(m)
                parent[key] = merge(m.value, action["update"])
    return doc


def main(overlay_path):
    overlay_path = Path(overlay_path)
    overlay = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
    spec_path = overlay_path.parent / overlay["extends"]
    doc = json.loads(spec_path.read_text(encoding="utf-8"))
    resolved = apply(doc, overlay)
    out = overlay_path.with_name(overlay_path.name.replace(".overlay.yaml", ".resolved.json"))
    out.write_text(json.dumps(resolved, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(overlay['actions'])} actions applied -> {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "spec/consultas-externas.overlay.yaml")
