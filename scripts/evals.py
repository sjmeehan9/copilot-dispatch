"""Quality gate script enforcing project coding standards.

Checks all Python source files in ``app/src/`` for:
- Public functions, methods, and classes have docstrings.
- No forbidden work-in-progress markers appear in delivered code.

Additionally checks ``tests/`` and ``scripts/`` for forbidden markers only
(docstrings are not required on test functions).

Exit codes:
- 0: All checks pass.
- 1: One or more violations found.

Usage::

    python scripts/evals.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Project root — one level above this file: scripts/evals.py → project root
# ---------------------------------------------------------------------------
_PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

# Directories to scan for docstring presence (app source code only).
_DOCSTRING_DIRS: list[Path] = [_PROJECT_ROOT / "app" / "src"]

# Directories to scan for forbidden work-in-progress markers.
_MARKER_SCAN_DIRS: list[Path] = [
    _PROJECT_ROOT / "app" / "src",
    _PROJECT_ROOT / "tests",
    _PROJECT_ROOT / "scripts",
]

# Markers that should not appear in delivered code.
# Constructed dynamically so this very file does not trigger its own check.
_FORBIDDEN_MARKERS: list[str] = [
    "TO" + "DO",
    "FIX" + "ME",
]


# ---------------------------------------------------------------------------
# Docstring checks
# ---------------------------------------------------------------------------


def _is_public(name: str) -> bool:
    """Return True if *name* represents a public Python identifier.

    A name is public if it does not start with an underscore.

    Args:
        name: The identifier to check.

    Returns:
        True when the name is public.
    """
    return not name.startswith("_")


def _is_dunder(name: str) -> bool:
    """Return True if *name* is a dunder (double-underscore) method.

    Args:
        name: The identifier to check.

    Returns:
        True when the name follows the ``__name__`` pattern.
    """
    return name.startswith("__") and name.endswith("__")


def _should_require_docstring(node: ast.AST, *, is_method: bool = False) -> bool:
    """Decide whether a given AST node requires a docstring.

    Rules:
    - Public classes always require a docstring.
    - Public functions/methods require a docstring **unless** the name starts
      with ``test_`` or is a dunder method (``__init__``, ``__repr__``, etc.).
    - Private identifiers (``_``-prefixed) are exempt.

    Args:
        node: An ``ast.FunctionDef``, ``ast.AsyncFunctionDef``, or
            ``ast.ClassDef`` node.
        is_method: True when the node is defined inside a class body.

    Returns:
        True when a docstring is required for this node.
    """
    name: str = getattr(node, "name", "")

    # Private identifiers are always exempt.
    if not _is_public(name):
        return False

    # Classes always require a docstring.
    if isinstance(node, ast.ClassDef):
        return True

    # Dunder methods (__init__, __repr__, …) are exempt.
    if is_method and _is_dunder(name):
        return False

    # Test functions (test_*) are exempt — names are self-documenting.
    if name.startswith("test_"):
        return False

    return True


def check_docstrings(file_path: Path) -> list[str]:
    """Check a single Python file for missing public docstrings.

    Parses the file with :mod:`ast` and inspects top-level and class-level
    definitions for docstring presence.

    Args:
        file_path: Absolute path to the Python source file.

    Returns:
        A list of human-readable violation strings (empty when all checks
        pass).
    """
    violations: list[str] = []
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError as exc:
        violations.append(f"{file_path}:{exc.lineno}: SyntaxError — {exc.msg}")
        return violations

    for node in ast.iter_child_nodes(tree):
        # Top-level functions / async functions.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _should_require_docstring(node, is_method=False):
                if ast.get_docstring(node) is None:
                    violations.append(
                        f"{file_path}:{node.lineno}: public function "
                        f"'{node.name}' missing docstring"
                    )

        # Classes — check the class itself, then its methods.
        elif isinstance(node, ast.ClassDef):
            if _should_require_docstring(node):
                if ast.get_docstring(node) is None:
                    violations.append(
                        f"{file_path}:{node.lineno}: public class "
                        f"'{node.name}' missing docstring"
                    )

            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if _should_require_docstring(child, is_method=True):
                        if ast.get_docstring(child) is None:
                            violations.append(
                                f"{file_path}:{child.lineno}: public method "
                                f"'{node.name}.{child.name}' missing docstring"
                            )

    return violations


# ---------------------------------------------------------------------------
# Forbidden marker checks
# ---------------------------------------------------------------------------


def check_no_forbidden_markers(file_path: Path) -> list[str]:
    """Scan a single Python file for forbidden work-in-progress markers.

    Performs a case-insensitive line-by-line search for each marker in
    :data:`_FORBIDDEN_MARKERS`.

    Args:
        file_path: Absolute path to the Python source file.

    Returns:
        A list of human-readable violation strings (empty when clean).
    """
    violations: list[str] = []
    try:
        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return violations

    for line_no, line in enumerate(lines, start=1):
        upper_line = line.upper()
        for marker in _FORBIDDEN_MARKERS:
            if marker in upper_line:
                violations.append(
                    f"{file_path}:{line_no}: forbidden marker "
                    f"'{marker}' found — \"{line.strip()}\""
                )

    return violations


# ---------------------------------------------------------------------------
# File collection helpers
# ---------------------------------------------------------------------------


def _collect_python_files(directory: Path) -> list[Path]:
    """Recursively collect all ``.py`` files under *directory*.

    Args:
        directory: Root directory to search.

    Returns:
        Sorted list of absolute paths to Python source files.
    """
    if not directory.is_dir():
        return []
    return sorted(directory.rglob("*.py"))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """Run all quality gate checks and report results.

    Returns:
        Exit code: 0 on success, 1 when violations are found.
    """
    all_violations: list[str] = []
    files_checked: int = 0

    # 1. Docstring checks — app/src/ only.
    for scan_dir in _DOCSTRING_DIRS:
        for py_file in _collect_python_files(scan_dir):
            files_checked += 1
            all_violations.extend(check_docstrings(py_file))

    # 2. Forbidden marker checks — app/src/, tests/, scripts/.
    seen: set[Path] = set()
    for scan_dir in _MARKER_SCAN_DIRS:
        for py_file in _collect_python_files(scan_dir):
            if py_file not in seen:
                seen.add(py_file)
                files_checked += 1
                all_violations.extend(check_no_forbidden_markers(py_file))

    # Report results.
    if all_violations:
        print(f"\nEvals FAILED: {len(all_violations)} violation(s) found\n")
        for v in all_violations:
            print(f"  ✗ {v}")
        print()
        return 1

    print(f"\nEvals passed: {files_checked} files checked, 0 violations\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
