Module blk_core {
    DataInPort a;
    DataInPort b;
    DataOutPort y { Source 1'b0; }
}

Module blk {
    Attribute warptap_format = "warptap-1500-icl-v1";
    Attribute warptap_ir_bits = 3;
    Attribute warptap_instr_0 = "WS_BYPASS,0,0,0,0";
    Attribute warptap_instr_1 = "WS_EXTEST,1,1,0,1";
    Attribute warptap_instr_2 = "WP_INTEST,2,0,1,0";
    Attribute warptap_instr_3 = "WP_EXTEST,3,0,0,1";
    Attribute warptap_instr_4 = "WS_INTEST,4,1,1,0";
    Attribute warptap_cell_0_0 = "input,a";
    Attribute warptap_cell_0_1 = "input,b";
    Attribute warptap_cell_1_0 = "output,y";
    Attribute warptap_ports = "wsi,wso,shiftwr,capturewr,updatewr,selectwir,wrck,wrstn,scan_en";
    Attribute warptap_chain_ports = "si0:so0,si1:so1";

    Enum blk_ops {
        WS_BYPASS = 3'b000;
        WS_EXTEST = 3'b100;
        WP_INTEST = 3'b010;
        WP_EXTEST = 3'b110;
        WS_INTEST = 3'b001;
    }
    ScanInPort wsi;
    ScanOutPort wso { Source WsoMux; }
    ShiftEnPort shiftwr;
    CaptureEnPort capturewr;
    UpdateEnPort updatewr;
    SelectPort selectwir;
    TCKPort wrck;
    ResetPort wrstn { ActivePolarity 0; }
    ScanInterface WSP { Port wsi; Port wso; Port shiftwr; Port capturewr; Port updatewr; Port selectwir; Port wrck; Port wrstn; }

    ScanInPort si0;
    ScanOutPort so0 { Source WBR0[0]; }
    ScanInPort si1;
    ScanOutPort so1 { Source WBR1; }
    ShiftEnPort scan_en;
    ScanInterface WPP { Chain c0 { Port si0; Port so0; } Chain c1 { Port si1; Port so1; } Port scan_en; Port wrck; Port wrstn; }

    DataInPort a;
    DataInPort b;
    DataOutPort y { Source Out_y; }
    Instance core Of blk_core { InputPort a = In_a; InputPort b = In_b; }

    ScanRegister WIR_SR[2:0] { ScanInSource wsi; }
    DataRegister WIR[2:0] {
        WriteDataSource WIR_SR;
        WriteEnSource selectwir;
        ResetValue 3'b000;
        RefEnum blk_ops;
    }
    ScanRegister WBY { ScanInSource wsi; CaptureSource 1'b0; ResetValue 1'b0; }
    LogicSignal serial { WIR == 3'b100 || WIR == 3'b001; }
    ScanMux WBR0_in SelectedBy serial {
        1'b1 : wsi;
        1'b0 : si0;
    }
    DataMux Cap0[1:0] SelectedBy WIR {
        3'b100 : a, b;
        3'b001 : WBR0[1], WBR0[0];
        3'b000 | 3'b010 | 3'b110 | 3'b101 | 3'b011 | 3'b111 : WBR0;
    }
    ScanRegister WBR0[1:0] { ScanInSource WBR0_in; CaptureSource Cap0; }
    ScanMux WBR1_in SelectedBy serial {
        1'b1 : WBR0[0];
        1'b0 : si1;
    }
    DataMux Cap1 SelectedBy WIR {
        3'b100 : WBR1;
        3'b001 : core.y;
        3'b000 | 3'b010 | 3'b110 | 3'b101 | 3'b011 | 3'b111 : WBR1;
    }
    ScanRegister WBR1 { ScanInSource WBR1_in; CaptureSource Cap1; }
    ScanMux WdrMux SelectedBy serial {
        1'b1 : WBR1;
        1'b0 : WBY;
    }
    ScanMux WsoMux SelectedBy selectwir {
        1'b1 : WIR_SR[0];
        1'b0 : WdrMux;
    }

    DataMux In_a SelectedBy WIR {
        3'b100 | 3'b110 : 1'b0;
        3'b010 | 3'b001 : WBR0[1];
        3'b000 | 3'b101 | 3'b011 | 3'b111 : a;
    }
    DataMux In_b SelectedBy WIR {
        3'b100 | 3'b110 : 1'b0;
        3'b010 | 3'b001 : WBR0[0];
        3'b000 | 3'b101 | 3'b011 | 3'b111 : b;
    }
    DataMux Out_y SelectedBy WIR {
        3'b010 | 3'b001 : 1'b0;
        3'b100 | 3'b110 : WBR1;
        3'b000 | 3'b101 | 3'b011 | 3'b111 : core.y;
    }
}

Module blk2_core {
    DataInPort d_0;
    DataOutPort q { Source 1'b0; }
}

Module blk2 {
    Attribute warptap_format = "warptap-1500-icl-v1";
    Attribute warptap_ir_bits = 3;
    Attribute warptap_instr_0 = "WS_BYPASS,0,0,0,0";
    Attribute warptap_instr_1 = "WS_EXTEST,1,1,0,1";
    Attribute warptap_instr_2 = "WP_INTEST,2,0,1,0";
    Attribute warptap_instr_3 = "WP_EXTEST,3,0,0,1";
    Attribute warptap_instr_4 = "WS_INTEST,4,1,1,0";
    Attribute warptap_cell_0_0 = "input,d[0]";
    Attribute warptap_cell_0_1 = "output,q";
    Attribute warptap_ports = "wsi,wso,shiftwr,capturewr,updatewr,selectwir,wrck,wrstn,scan_en";
    Attribute warptap_chain_ports = "si0:so0";

    Enum blk2_ops {
        WS_BYPASS = 3'b000;
        WS_EXTEST = 3'b100;
        WP_INTEST = 3'b010;
        WP_EXTEST = 3'b110;
        WS_INTEST = 3'b001;
    }
    ScanInPort wsi;
    ScanOutPort wso { Source WsoMux; }
    ShiftEnPort shiftwr;
    CaptureEnPort capturewr;
    UpdateEnPort updatewr;
    SelectPort selectwir;
    TCKPort wrck;
    ResetPort wrstn { ActivePolarity 0; }
    ScanInterface WSP { Port wsi; Port wso; Port shiftwr; Port capturewr; Port updatewr; Port selectwir; Port wrck; Port wrstn; }

    ScanInPort si0;
    ScanOutPort so0 { Source WBR0[0]; }
    ShiftEnPort scan_en;
    ScanInterface WPP { Chain c0 { Port si0; Port so0; } Port scan_en; Port wrck; Port wrstn; }

    DataInPort d_0;
    DataOutPort q { Source Out_q; }
    Instance core Of blk2_core { InputPort d_0 = In_d_0; }

    ScanRegister WIR_SR[2:0] { ScanInSource wsi; }
    DataRegister WIR[2:0] {
        WriteDataSource WIR_SR;
        WriteEnSource selectwir;
        ResetValue 3'b000;
        RefEnum blk2_ops;
    }
    ScanRegister WBY { ScanInSource wsi; CaptureSource 1'b0; ResetValue 1'b0; }
    LogicSignal serial { WIR == 3'b100 || WIR == 3'b001; }
    ScanMux WBR0_in SelectedBy serial {
        1'b1 : wsi;
        1'b0 : si0;
    }
    DataMux Cap0[1:0] SelectedBy WIR {
        3'b100 : d_0, WBR0[0];
        3'b001 : WBR0[1], core.q;
        3'b000 | 3'b010 | 3'b110 | 3'b101 | 3'b011 | 3'b111 : WBR0;
    }
    ScanRegister WBR0[1:0] { ScanInSource WBR0_in; CaptureSource Cap0; }
    ScanMux WdrMux SelectedBy serial {
        1'b1 : WBR0[0];
        1'b0 : WBY;
    }
    ScanMux WsoMux SelectedBy selectwir {
        1'b1 : WIR_SR[0];
        1'b0 : WdrMux;
    }

    DataMux In_d_0 SelectedBy WIR {
        3'b100 | 3'b110 : 1'b0;
        3'b010 | 3'b001 : WBR0[1];
        3'b000 | 3'b101 | 3'b011 | 3'b111 : d_0;
    }
    DataMux Out_q SelectedBy WIR {
        3'b010 | 3'b001 : 1'b0;
        3'b100 | 3'b110 : WBR0[0];
        3'b000 | 3'b101 | 3'b011 | 3'b111 : core.q;
    }
}

Module soc {
    Attribute warptap_format = "warptap-1500-icl-v1";
    Attribute warptap_chain = "u0:blk,u1:blk2";
    Attribute warptap_pin_wsi = "wsi,0";
    Attribute warptap_pin_wso = "wso,0";
    Attribute warptap_pin_shiftwr = "shiftwr,0";
    Attribute warptap_pin_capturewr = "capturewr,0";
    Attribute warptap_pin_updatewr = "updatewr,0";
    Attribute warptap_pin_selectwir = "selectwir,0";
    Attribute warptap_pin_wrck = "wrck,0";
    Attribute warptap_pin_wrstn = "wrst,1";
    ScanInPort wsi;
    ScanOutPort wso { Source u1.wso; }
    ShiftEnPort shiftwr;
    CaptureEnPort capturewr;
    UpdateEnPort updatewr;
    SelectPort selectwir;
    TCKPort wrck;
    ResetPort wrst { ActivePolarity 0; }
    ScanInterface WSP { Port wsi; Port wso; Port shiftwr; Port capturewr; Port updatewr; Port selectwir; Port wrck; Port wrst; }
    DataInPort u0_a;
    DataInPort u0_b;
    ShiftEnPort u0_scan_en;
    ScanInPort u0_si0;
    ScanOutPort u0_so0 { Source u0.so0; }
    ScanInPort u0_si1;
    ScanOutPort u0_so1 { Source u0.so1; }
    ScanInterface u0_WPP { Chain c0 { Port u0_si0; Port u0_so0; } Chain c1 { Port u0_si1; Port u0_so1; } Port u0_scan_en; Port wrck; Port wrst; }
    Instance u0 Of blk { InputPort a = u0_a; InputPort b = u0_b; InputPort wsi = wsi; InputPort shiftwr = shiftwr; InputPort capturewr = capturewr; InputPort updatewr = updatewr; InputPort selectwir = selectwir; InputPort wrck = wrck; InputPort wrstn = ~wrst; InputPort scan_en = u0_scan_en; InputPort si0 = u0_si0; InputPort si1 = u0_si1; }
    DataInPort u1_d_0;
    ShiftEnPort u1_scan_en;
    ScanInPort u1_si0;
    ScanOutPort u1_so0 { Source u1.so0; }
    ScanInterface u1_WPP { Chain c0 { Port u1_si0; Port u1_so0; } Port u1_scan_en; Port wrck; Port wrst; }
    Instance u1 Of blk2 { InputPort d_0 = u1_d_0; InputPort wsi = u0.wso; InputPort shiftwr = shiftwr; InputPort capturewr = capturewr; InputPort updatewr = updatewr; InputPort selectwir = selectwir; InputPort wrck = wrck; InputPort wrstn = ~wrst; InputPort scan_en = u1_scan_en; InputPort si0 = u1_si0; }
}
