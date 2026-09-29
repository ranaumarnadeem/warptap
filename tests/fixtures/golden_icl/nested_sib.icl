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

Module warptap_instr_deep {
    ScanInPort SI;
    ScanOutPort SO { Source DR[1]; }

    // warptap: fixed stub capture_value=0x2 -- no
    // real CaptureSource; ICL has no confirmed mechanism for a constant capture
    // source, so none is fabricated here (see module docstring).
    ScanRegister DR[1:0] {
        ScanInSource SI;
        ResetValue 2'b0;
    }
}

Module warptap_instr_top_leaf {
    ScanInPort SI;
    ScanOutPort SO { Source DR; }

    // warptap: fixed stub capture_value=0x1 -- no
    // real CaptureSource; ICL has no confirmed mechanism for a constant capture
    // source, so none is fabricated here (see module docstring).
    ScanRegister DR[0:0] {
        ScanInSource SI;
        ResetValue 1'b0;
    }
}

Module chip_nested {
    ScanInPort tdi;
    ScanOutPort tdo { Source warptap_sib_leaf.SO; }
    TCKPort tck;
    TMSPort tms;
    TRSTPort trst_n;
    ScanInterface tap { Port tdi; Port tdo; Port tck; Port tms; Port trst_n; }

    Instance warptap_sib_outer Of warptap_sib { InputPort SI = tdi; InputPort fromSO = warptap_sib_inner.SO; }
    Instance warptap_sib_inner Of warptap_sib { InputPort SI = warptap_sib_outer.toSI; InputPort fromSO = warptap_instr_deep.SO; }
    Instance warptap_instr_deep Of warptap_instr_deep { InputPort SI = warptap_sib_inner.toSI; }
    Instance warptap_sib_leaf Of warptap_sib { InputPort SI = warptap_sib_outer.SO; InputPort fromSO = warptap_instr_top_leaf.SO; }
    Instance warptap_instr_top_leaf Of warptap_instr_top_leaf { InputPort SI = warptap_sib_leaf.toSI; }
}
