// Testbench for a SIB-network-inserted, faultflow-compaction-composed `core_top_compacted`
// module (Stage 24 cross-sim tier). Mirrors tb_compression_two_chain.v's exact two-clock-
// domain, pulse-flag-column stimulus protocol -- compaction needs no scan_en GATING at all
// (its own wrapper is purely combinational, no clock), but scan_en is still driven as a real,
// unused DUT input for consistency with a real scan design's own port list.
//
// Stimulus file: one line of five integers each: "<tms> <tdi> <trst_n> <scan_en> <pulse>".
`timescale 1ns/1ps

module tb_compaction_three_chain;
    reg clk = 0;
    reg tck = 0;
    reg tms = 0;
    reg tdi = 0;
    reg trst_n = 1;
    reg scan_en = 0;
    wire tdo;

    // scan_out_0/1/2 are NOT top-level ports of core_top_compacted -- compaction's own
    // wrapper excludes exactly the ports it wraps (compactor_wrapper_verilog's
    // passthrough_names = every port EXCEPT scan_out_ports) from its passthrough list; they
    // become internal wires feeding the XOR tree, unlike compression's own scan_in_N (which
    // stays accessible -- compression wraps the LOAD side, compaction wraps the UNLOAD side).
    core_top_compacted dut (
        .clk(clk),
        .scan_en(scan_en),
        .tck(tck),
        .tms(tms),
        .tdi(tdi),
        .trst_n(trst_n),
        .tdo(tdo)
    );

    integer fd;
    integer code;
    integer tms_in, tdi_in, trst_n_in, scan_en_in, pulse_in;
    reg done;

    initial begin
        done = 0;
        fd = $fopen("stimulus.txt", "r");
        if (fd == 0) begin
            $display("ERROR: could not open stimulus.txt");
            $finish;
        end

        while (!done) begin
            code = $fscanf(fd, "%d %d %d %d %d", tms_in, tdi_in, trst_n_in, scan_en_in, pulse_in);
            if (code != 5) begin
                done = 1;
            end else begin
                trst_n = trst_n_in[0];
                scan_en = scan_en_in[0];
                if (pulse_in[0]) begin
                    #1;
                    $display("TRACE,%0d,%0d,%0d,%0d,%0d,%0d",
                              dut.warptap_tap_state, dut.warptap_current_instruction,
                              dut.warptap_capture_dr, dut.warptap_shift_dr, dut.warptap_update_dr,
                              tdo);
                    clk = 1;
                    #1;
                    clk = 0;
                    #1;
                end else begin
                    tms = tms_in[0];
                    tdi = tdi_in[0];
                    #1;
                    $display("TRACE,%0d,%0d,%0d,%0d,%0d,%0d",
                              dut.warptap_tap_state, dut.warptap_current_instruction,
                              dut.warptap_capture_dr, dut.warptap_shift_dr, dut.warptap_update_dr,
                              tdo);
                    tck = 1;
                    #1;
                    tck = 0;
                    #1;
                end
            end
        end

        $fclose(fd);
        $finish;
    end
endmodule
