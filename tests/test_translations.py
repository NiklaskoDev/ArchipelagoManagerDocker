"""The German web UI: every text the code marks for translation is in the dictionary, with the same placeholders.

The browser translates the server's English messages too (i18n.js, tServer). For those the messages are collected
from the code: the detail of each HTTPException and the message of the errors the API passes on.
"""
import ast
import json
import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "manager" / "archipelago_manager"
STATIC = PACKAGE / "static" / "js"
DICTIONARY = json.loads((STATIC / "i18n" / "de.json").read_text(encoding="utf-8"))

# t("..."), and both forms of plural(n, "...", "...")
_STRING = r'"((?:[^"\\\n]|\\.)*)"'
T_CALL = re.compile(r"\bt\(\s*" + _STRING)
PLURAL_CALL = re.compile(r"\bplural\([^,()]+(?:\([^()]*\))?[^,()]*,\s*" + _STRING + r",\s*" + _STRING)
# Errors whose message is a code or never reaches anyone
NOT_SHOWN = {"DiscordError", "ConstructorError", "RuntimeError"}


def js_keys() -> dict[str, str]:
    """Each marked text and a file it is in."""
    keys = {}
    for path in sorted(STATIC.rglob("*.js")):
        source = path.read_text(encoding="utf-8")
        for match in T_CALL.finditer(source):
            keys.setdefault(json.loads(f'"{match.group(1)}"'), path.name)
        for match in PLURAL_CALL.finditer(source):
            for group in (1, 2):
                keys.setdefault(json.loads(f'"{match.group(group)}"'), path.name)
    return keys


def _message(node: ast.expr) -> str | None:
    """The English message as the dictionary has it: the changing parts of an f-string become {0}, {1}, ..."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts, index = [], 0
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(value.value)
            else:
                parts.append(f"{{{index}}}")
                index += 1
        return "".join(parts)
    return None


def server_messages() -> dict[str, str]:
    messages = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        if path.name == "ap_helper.py":  # runs inside Archipelago and answers the manager, not people
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None) or ""
            if name == "HTTPException":
                arg = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "detail"), None)
            elif name.endswith("Error") and name not in NOT_SHOWN:
                arg = node.args[0] if node.args else None
            else:
                continue
            message = _message(arg) if arg is not None else None
            if message and re.search(r"[A-Za-z]{2}", message):
                messages.setdefault(message, f"{path.relative_to(PACKAGE)}:{node.lineno}")
    return messages


def placeholders(text: str) -> set[str]:
    return set(re.findall(r"\{(\w+)\}", text))


def test_every_marked_text_is_translated():
    strings = DICTIONARY["strings"]
    missing = [f"{key!r} ({where})" for key, where in js_keys().items() if key not in strings]
    assert not missing, "Not in i18n/de.json \"strings\":\n" + "\n".join(missing)


def test_every_server_message_is_translated():
    server = DICTIONARY["server"]
    missing = [f"{key!r} ({where})" for key, where in server_messages().items() if key not in server]
    assert not missing, "Not in i18n/de.json \"server\":\n" + "\n".join(missing)


def test_translations_keep_their_placeholders():
    wrong = [f"{key!r} -> {value!r}" for part in ("strings", "server") for key, value in DICTIONARY[part].items()
             if placeholders(key) != placeholders(value) or not value.strip()]
    assert not wrong, "\n".join(wrong)


def test_the_dictionary_has_no_leftovers():
    # Texts that are no longer in the code would pile up unnoticed
    used, messages = js_keys(), server_messages()
    unused = [key for key in DICTIONARY["strings"] if key not in used]
    assert not unused, "No longer used in the web UI: " + ", ".join(map(repr, unused))
    extra = set(DICTIONARY.get("server_extra", []))
    unused = [key for key in DICTIONARY["server"] if key not in messages and key not in extra]
    assert not unused, "Not a server message (list ones the server sends some other way in \"server_extra\"): " + \
        ", ".join(map(repr, unused))
