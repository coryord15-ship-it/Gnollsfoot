"""Guard against the bug class that killed the learning loop.

On 2026-08-08 the observation uploader thread was found dead on arrival: it
called time.sleep() and `time` was never imported in app/main.py. Because the
thread was a daemon and nothing joined it, the NameError vanished silently on
every launch — and `log_observations` sat at zero rows for weeks while everyone
assumed it was an adoption problem.

py_compile and a clean import both PASS on that file: the name is only resolved
when the line actually runs, and that line ran in a background thread nobody
watched. Only launching the app surfaced it.

While fixing it, the exact same mistake was nearly reintroduced with `glob`.
Hence this test — it is cheap, it is static, and it would have caught both.
"""
import ast
import glob
import os

STDLIB = {
    "os", "sys", "re", "json", "glob", "time", "shutil", "subprocess", "threading",
    "logging", "datetime", "math", "random", "string", "winreg", "ctypes", "sqlite3",
    "socket", "traceback", "itertools", "collections", "functools", "pathlib",
    "urllib", "base64", "hashlib",
}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _missing_imports(path: str) -> list[str]:
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                imported.add(a.asname or a.name)
    # Names used as `mod.attr` where `mod` is a bare name.
    used = {
        n.value.id for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
    }
    return sorted((used & STDLIB) - imported)


def test_every_module_imports_the_stdlib_it_uses():
    offenders = {}
    for path in glob.glob(os.path.join(ROOT, "app", "**", "*.py"), recursive=True):
        miss = _missing_imports(path)
        if miss:
            offenders[os.path.relpath(path, ROOT)] = miss
    assert not offenders, (
        "module(s) use a stdlib name they never import — this raises NameError only "
        "when the line runs, so a background thread will die silently:\n"
        + "\n".join(f"  {k}: {v}" for k, v in offenders.items())
    )
