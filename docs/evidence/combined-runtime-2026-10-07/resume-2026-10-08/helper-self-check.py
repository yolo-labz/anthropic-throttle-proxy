"""Static admission check for the retained receipt helper; no runtime contact.

The helper is a live verifier: importing it would talk to :8765 and the journal,
so this check reads its source instead. It proves the properties the receipt
relies on — one bounded, shell-free subprocess call site, an exact allowlist for
the unit and property arguments, and refusal by exception otherwise — without
executing a single line of it.

The earlier version of this check extracted and executed the helper's command()
function through exec(). The universal code-slop gate rejected that on review
(security/python-exec, ai-slop/swallowed-exception); the harness was replaced
instead of suppressed. The gate output is retained in
code-slop-gate-self-check-failure.txt.
"""

import ast
import pathlib

HELPER = (
    pathlib.Path(__file__).resolve().parent.parent
    / "resumed-2317"
    / "2683-verify-combined-runtime.py"
)
EXPECTED_UNITS = {
    "mimo-desktop-subscription.service",
    "mimo-throttle-proxy.service",
    "zai-throttle-proxy.service",
    "mimo-tokenplan-shim.service",
    "mimo-desktop-quota-report.timer",
}
EXPECTED_PROPERTIES = {
    "ExecStart",
    "MainPID",
    "FragmentPath",
    "DropInPaths",
    "ActiveState",
    "SubState",
    "ExecMainStartTimestamp",
    "Environment",
    "UnitFileState",
    "LastTriggerUSec",
}
LIFECYCLE_VERBS = {"start", "stop", "restart", "reload", "enable", "disable"}

source = HELPER.read_text()
tree = ast.parse(source)
command = next(
    node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "command"
)

assignments = {
    target.id: node.value
    for node in ast.walk(command)
    if isinstance(node, ast.Assign)
    for target in node.targets
    if isinstance(target, ast.Name)
}


def admitted_names(node):
    """String constants in a set literal, plus the bare UNIT name it may hold."""
    names = set()
    for element in node.elts:
        if isinstance(element, ast.Constant):
            names.add(element.value)
        else:
            assert isinstance(element, ast.Name) and element.id == "UNIT", ast.dump(element)
            names.add("UNIT")
    return names


units = admitted_names(assignments["units"])
properties = admitted_names(assignments["properties"])
assert units == EXPECTED_UNITS | {"UNIT"}, units
assert properties == EXPECTED_PROPERTIES, properties

raises = [node for node in ast.walk(command) if isinstance(node, ast.Raise)]
assert len(raises) >= 2, "unsupported arguments must raise, never fall through"

calls = [
    node
    for node in ast.walk(command)
    if isinstance(node, ast.Call)
    and isinstance(node.func, ast.Attribute)
    and node.func.attr == "check_output"
]
assert len(calls) == 1, "exactly one subprocess call site expected"
keywords = {keyword.arg: keyword.value for keyword in calls[0].keywords}
assert set(keywords) == {"text", "timeout"}, keywords
assert isinstance(keywords["text"], ast.Constant) and keywords["text"].value is True
assert isinstance(keywords["timeout"], ast.Constant) and keywords["timeout"].value == 5

inside_command = {id(node) for node in ast.walk(command)}
subprocess_uses = [
    node
    for node in ast.walk(tree)
    if isinstance(node, ast.Attribute)
    and isinstance(node.value, ast.Name)
    and node.value.id == "subprocess"
]
assert subprocess_uses, "helper should invoke subprocess"
assert all(id(node) in inside_command for node in subprocess_uses), (
    "subprocess used outside command()"
)

literals = {
    node.value
    for node in ast.walk(tree)
    if isinstance(node, ast.Constant) and isinstance(node.value, str)
}
assert not literals & LIFECYCLE_VERBS, literals & LIFECYCLE_VERBS
keyword_args = {
    keyword.arg
    for node in ast.walk(tree)
    if isinstance(node, ast.Call)
    for keyword in node.keywords
    if keyword.arg
}
assert "shell" not in keyword_args, "no shell=True anywhere in the helper"

print(
    "PASS static: allowlisted units/properties, one shell-free 5s-bounded subprocess "
    "call site, exception refusal, no lifecycle verb"
)
