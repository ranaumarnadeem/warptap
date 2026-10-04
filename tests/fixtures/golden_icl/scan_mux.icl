Module warptap_sib {
    ScanInPort SI;
    SelectPort SEL;
    ScanOutPort SO { Source SR; }
    ScanInterface client { Port SI; Port SEL; Port SO; }

    ScanInPort fromSO;
    ScanOutPort toSI { Source SI; }
    // Structural (topology-level) select signal only -- see module docstring's
    // "real, deliberate scope boundary" note: does not re-encode
    // rtl/sib_cell.v's real po & shift_ff & select update-latch-commit timing.
    LogicSignal toSelSignal { SR & SEL; }
    ToSelectPort toSEL { Source toSelSignal; }
    ScanInterface host { Port fromSO; Port toSI; Port toSEL; }

    ScanRegister SR { ScanInSource SIBmux; CaptureSource SR; ResetValue 1'b0; }
    ScanMux SIBmux SelectedBy SR { 1'b0 : SI; 1'b1 : fromSO; }
}

Module warptap_scan_mux_mux_outer {
    ScanInPort SI;
    SelectPort SEL;
    ScanOutPort SO { Source SELREG[0]; }
    ScanInterface client { Port SI; Port SEL; Port SO; }

    ScanInPort fromArm0;
    ScanInPort fromArm1;
    ScanOutPort toSI { Source SI; }
    ToSelectPort toSEL0 { Source toSelSignal0; }
    ToSelectPort toSEL1 { Source toSelSignal1; }
    ScanInterface host0 { Port fromArm0; Port toSI; Port toSEL0; }
    ScanInterface host1 { Port fromArm1; Port toSI; Port toSEL1; }

    LogicSignal toSelSignal0 { (SELREG == 2'd1) & SEL; }
    LogicSignal toSelSignal1 { (SELREG == 2'd2) & SEL; }

    ScanRegister SELREG[1:0] {
        ScanInSource MUX;
        CaptureSource SELREG[1:0];
        ResetValue 2'b0;
    }
    ScanMux MUX SelectedBy SELREG[1:0] { 2'd1 : fromArm0; 2'd2 : fromArm1; }
}

Module warptap_scan_mux_mux_inner {
    ScanInPort SI;
    SelectPort SEL;
    ScanOutPort SO { Source SELREG; }
    ScanInterface client { Port SI; Port SEL; Port SO; }

    ScanInPort fromArm0;
    ScanInPort fromArm1;
    ScanOutPort toSI { Source SI; }
    ToSelectPort toSEL0 { Source toSelSignal0; }
    ToSelectPort toSEL1 { Source toSelSignal1; }
    ScanInterface host0 { Port fromArm0; Port toSI; Port toSEL0; }
    ScanInterface host1 { Port fromArm1; Port toSI; Port toSEL1; }

    LogicSignal toSelSignal0 { (SELREG == 1'd0) & SEL; }
    LogicSignal toSelSignal1 { (SELREG == 1'd1) & SEL; }

    ScanRegister SELREG {
        ScanInSource MUX;
        CaptureSource SELREG;
        ResetValue 1'b0;
    }
    ScanMux MUX SelectedBy SELREG { 1'd0 : fromArm0; 1'd1 : fromArm1; }
}

Module warptap_instr_deep_a {
    ScanInPort SI;
    ScanOutPort SO { Source DR; }

    // warptap: fixed stub capture_value=0x0 -- no
    // real CaptureSource; ICL has no confirmed mechanism for a constant capture
    // source, so none is fabricated here (see module docstring).
    ScanRegister DR[0:0] {
        ScanInSource SI;
        ResetValue 1'b0;
    }
}

Module warptap_instr_deep_b {
    ScanInPort SI;
    ScanOutPort SO { Source DR[1]; }

    // warptap: fixed stub capture_value=0x1 -- no
    // real CaptureSource; ICL has no confirmed mechanism for a constant capture
    // source, so none is fabricated here (see module docstring).
    ScanRegister DR[1:0] {
        ScanInSource SI;
        ResetValue 2'b0;
    }
}

Module warptap_instr_arm_two {
    ScanInPort SI;
    ScanOutPort SO { Source DR[2]; }

    // warptap: fixed stub capture_value=0x6 -- no
    // real CaptureSource; ICL has no confirmed mechanism for a constant capture
    // source, so none is fabricated here (see module docstring).
    ScanRegister DR[2:0] {
        ScanInSource SI;
        ResetValue 3'b0;
    }
}

Module chip_mux {
    ScanInPort tdi;
    ScanOutPort tdo { Source warptap_scan_mux_mux_outer.SO; }
    TCKPort tck;
    TMSPort tms;
    TRSTPort trst_n;
    ScanInterface tap { Port tdi; Port tdo; Port tck; Port tms; Port trst_n; }

    Instance warptap_scan_mux_mux_outer Of warptap_scan_mux_mux_outer { InputPort SI = tdi; InputPort fromArm0 = warptap_scan_mux_mux_inner.SO; InputPort fromArm1 = warptap_instr_arm_two.SO; }
    Instance warptap_scan_mux_mux_inner Of warptap_scan_mux_mux_inner { InputPort SI = warptap_scan_mux_mux_outer.toSI; InputPort fromArm0 = warptap_instr_deep_a.SO; InputPort fromArm1 = warptap_instr_deep_b.SO; }
    Instance warptap_instr_deep_a Of warptap_instr_deep_a { InputPort SI = warptap_scan_mux_mux_inner.toSI; }
    Instance warptap_instr_deep_b Of warptap_instr_deep_b { InputPort SI = warptap_scan_mux_mux_inner.toSI; }
    Instance warptap_instr_arm_two Of warptap_instr_arm_two { InputPort SI = warptap_scan_mux_mux_outer.toSI; }
}
