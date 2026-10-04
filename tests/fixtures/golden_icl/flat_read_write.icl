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

Module warptap_instr_sensor {
    ScanInPort SI;
    ScanOutPort SO { Source DR[2]; }

    // warptap: fixed stub capture_value=0x5 -- no
    // real CaptureSource; ICL has no confirmed mechanism for a constant capture
    // source, so none is fabricated here (see module docstring).
    ScanRegister DR[2:0] {
        ScanInSource SI;
        ResetValue 3'b0;
    }
}

Module warptap_instr_ctrl {
    // DataOutPort DO drives real host port bit(s): start[0], mode[0]
    DataOutPort DO[1:0] { Source DR[1:0]; }
    ScanInPort SI;
    ScanOutPort SO { Source SR[1]; }

    ScanRegister SR[1:0] {
        ScanInSource SI;
        CaptureSource SR[1:0];  // self-capture: mirrors instrument_write.v's
                                   // own shift_ff <= po read-back-what-was-
                                   // last-committed behavior
        ResetValue 2'b0;
    }
    DataRegister DR[1:0] {
        WriteDataSource SR[1:0];
        WriteEnSource 1'b1;  // rtl/instrument_write.v commits unconditionally
                              // once selected -- see rtl/instrument_write.v's
                              // own module docstring for the real select gate.
        ResetValue 2'b0;
    }
}

Module warptap_instr_status {
    ScanInPort SI;
    ScanOutPort SO { Source DR; }

    // CaptureSource fans out from real host port bit(s): done[0]
    ScanRegister DR[0:0] {
        ScanInSource SI;
        CaptureSource DR[0:0];
        ResetValue 1'b0;
    }
}

Module chip {
    ScanInPort tdi;
    ScanOutPort tdo { Source warptap_sib_status.SO; }
    TCKPort tck;
    TMSPort tms;
    TRSTPort trst_n;
    ScanInterface tap { Port tdi; Port tdo; Port tck; Port tms; Port trst_n; }

    Instance warptap_sib_sensor Of warptap_sib { InputPort SI = tdi; InputPort fromSO = warptap_instr_sensor.SO; }
    Instance warptap_instr_sensor Of warptap_instr_sensor { InputPort SI = warptap_sib_sensor.toSI; }
    Instance warptap_sib_ctrl Of warptap_sib { InputPort SI = warptap_sib_sensor.SO; InputPort fromSO = warptap_instr_ctrl.SO; }
    Instance warptap_instr_ctrl Of warptap_instr_ctrl { InputPort SI = warptap_sib_ctrl.toSI; }
    Instance warptap_sib_status Of warptap_sib { InputPort SI = warptap_sib_ctrl.SO; InputPort fromSO = warptap_instr_status.SO; }
    Instance warptap_instr_status Of warptap_instr_status { InputPort SI = warptap_sib_status.toSI; }
}
