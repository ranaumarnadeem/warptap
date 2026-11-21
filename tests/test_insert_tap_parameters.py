"""``insert_sib_network`` and ``insert_bsr`` import ``rtl/tap_core.v`` with its own 4-bit
instruction register and EXTEST at ``0000``, and never change either. Until they refused other
values, ``ir_width=5`` or ``opcode_extest=0b0101`` built a design whose decode didn't match its
TAP: ``tap_core``'s Update-IR turns an opcode it doesn't know into BYPASS, so a decode against
``0101`` never matches and the network or boundary register can never be selected. Both now
refuse such a value before touching the netlist, which is ``None`` here: anything that touched it
would raise ``AttributeError`` instead.
"""

from __future__ import annotations

import pytest

from warptap.bsr_insert import BsrInsertError, insert_bsr
from warptap.sib_insert import SibInsertError, insert_sib_network
from warptap.tap_model import OPCODE_IJTAG_ACCESS, tap_core_parameters_error

_REFUSED = [
    ({"ir_width": 5}, "ir_width 5 isn't supported"),
    ({"ir_width": 3}, "ir_width 3 isn't supported"),
    ({"opcode_extest": 0b0101}, "opcode_extest 5 isn't supported"),
    ({"opcode_extest": 0b1111}, "opcode_extest 15 isn't supported"),
]


def test_the_tap_cores_own_values_are_accepted():
    assert tap_core_parameters_error(4, 0b0000) is None


@pytest.mark.parametrize("ir_width", [2, 3, 5, 8, True, 4.0, "4"])
def test_any_other_ir_width_is_refused_with_a_reason(ir_width):
    problem = tap_core_parameters_error(ir_width, 0b0000)
    assert problem.startswith(f"ir_width {ir_width!r} isn't supported")
    assert "4-bit instruction register" in problem


@pytest.mark.parametrize("opcode", [0b0001, 0b0101, 0b1100, 0b1111, False, 0.0])
def test_any_other_extest_opcode_is_refused_with_a_reason(opcode):
    problem = tap_core_parameters_error(4, opcode)
    assert problem.startswith(f"opcode_extest {opcode!r} isn't supported")
    assert "EXTEST at 0000" in problem


@pytest.mark.parametrize(("options", "message"), _REFUSED)
@pytest.mark.parametrize("ijtag_access_opcode", [OPCODE_IJTAG_ACCESS, None], ids=["default", "extest"])
def test_insert_sib_network_refuses_it_before_touching_the_netlist(
    options, message, ijtag_access_opcode
):
    with pytest.raises(SibInsertError, match=f"^{message}"):
        insert_sib_network(None, "top", None, ijtag_access_opcode=ijtag_access_opcode, **options)


@pytest.mark.parametrize(("options", "message"), _REFUSED)
def test_insert_bsr_refuses_it_before_touching_the_netlist(options, message):
    with pytest.raises(BsrInsertError, match=f"^{message}"):
        insert_bsr(None, "top", None, **options)


def test_the_tap_cores_own_values_get_past_the_check():
    """Control: given explicitly, 4 and 0000 aren't refused; both functions go on to the
    netlist, which is ``None`` here."""
    with pytest.raises(AttributeError):
        insert_sib_network(None, "top", None, ir_width=4, opcode_extest=0b0000)
    with pytest.raises(AttributeError):
        insert_bsr(None, "top", None, ir_width=4, opcode_extest=0b0000)
