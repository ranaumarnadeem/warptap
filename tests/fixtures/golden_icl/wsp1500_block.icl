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
