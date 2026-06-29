"""Shared kernel — generic workflow/FSM building blocks (DDD shared kernel).

Domain-free: no bounded-context concept lives here. The kernel is the low layer;
domains import the kernel, never the reverse.
Enforced by tools/audit/import_layering.py.
"""
