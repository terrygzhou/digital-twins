"""digital-twins-kb — transition metapackage (deprecated).

This package is an empty shim. It exists only so that
``pip install digital-twins-kb`` keeps resolving for one release cycle
after the PyPI distribution was renamed to ``digital-twins`` (0.11.0).
It carries no code of its own; installing it pulls in the real
``digital-twins`` distribution via its single hard dependency.

Migrate by installing the real distribution directly:

    pip install digital-twins
    pip uninstall digital-twins-kb
"""

__version__ = "0.11.0"
