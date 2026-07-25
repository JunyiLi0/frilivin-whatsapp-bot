"""Drop a module here and its ``Handler`` subclasses are picked up automatically.

Conventions:

* one behaviour per file, named after what it does;
* ``priority`` low runs first (10 = commands, 50 = routing, 1000 = fallback);
* a module whose name starts with ``_`` is ignored by the registry.
"""
