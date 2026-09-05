"""Shared exception base for warptap (library-quality pass, post-Stage 14). Every one of this
project's other error classes (``IclEmitError``, ``SibInsertError``, ``FaultflowRetargetError``,
etc. -- 17 in total, each named for exactly the failure mode it represents) subclasses
:class:`WarptapError` instead of ``RuntimeError`` directly, so a caller can catch "anything
warptap raises" without enumerating every specific subclass, while every existing
``except RuntimeError``/``except <SpecificError>`` call site keeps working unmodified --
``WarptapError`` IS a ``RuntimeError``, this is a purely additive common ancestor, not a
behavior change to any existing exception.
"""

from __future__ import annotations


class WarptapError(RuntimeError):
    """Base for every exception this library raises. Catch this to handle any warptap-raised
    error without enumerating each specific subclass; catch a specific subclass (e.g.
    :class:`~warptap.icl_emit.IclEmitError`) when only that one failure mode matters."""
