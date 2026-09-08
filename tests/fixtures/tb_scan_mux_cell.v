// Standalone testbench for rtl/scan_mux_cell.v (multi-arm ScanMux plan, Phase 0): drives one
// scan_mux_cell instance directly -- no network, no real nested arms, each `arm_so[k]` a free
// stimulus input standing in for whatever leaf or nested structure would occupy that arm --
// to prove its self-select-field/decode/relay behavior in isolation, before any package
// source changes exist to build on. Mirrors tb_sib_cell.v's stimulus-file-driven,
// pre-edge-sampling shape exactly.
//
// Fixed at ARMS=3, SEL_WIDTH=2 (arm0=1, arm1=2, arm2=3; value 0 is bypass) -- a genuinely
// N>2-arm case with a select field wider than 1 bit, the two dimensions sib_cell.v alone
// never exercises.
//
// Stimulus file: one line of seven integers each:
// "<trst_n> <select> <si> <arm_so_packed> <capture_dr> <shift_dr> <update_dr>"
// (arm_so_packed's low 3 bits are arm_so[2:0], arm0 = bit 0).
`timescale 1ns/1ps

module tb_scan_mux_cell;
    localparam ARMS = 3;
    localparam SEL_WIDTH = 2;

    reg tck = 0;
    reg trst_n = 1;
    reg select = 0;
    reg si = 0;
    reg [ARMS-1:0] arm_so = 0;
    reg capture_dr = 0;
    reg shift_dr = 0;
    reg update_dr = 0;

    wire so, nested_si;
    wire [ARMS-1:0] arm_select, arm_active;

    scan_mux_cell #(
        .ARMS(ARMS),
        .SEL_WIDTH(SEL_WIDTH),
        .ARM_VALUES({2'd3, 2'd2, 2'd1})  // arm0=1, arm1=2, arm2=3 (arm0 = low bits)
    ) dut (
        .si(si),
        .so(so),
        .arm_so(arm_so),
        .nested_si(nested_si),
        .arm_select(arm_select),
        .arm_active(arm_active),
        .select(select),
        .capture_dr(capture_dr),
        .shift_dr(shift_dr),
        .update_dr(update_dr),
        .tck(tck),
        .trst_n(trst_n)
    );

    integer fd;
    integer code;
    integer trst_n_in, select_in, si_in, arm_so_in, capture_dr_in, shift_dr_in, update_dr_in;
    reg done;

    initial begin
        done = 0;
        fd = $fopen("stimulus.txt", "r");
        if (fd == 0) begin
            $display("ERROR: could not open stimulus.txt");
            $finish;
        end

        while (!done) begin
            code = $fscanf(fd, "%d %d %d %d %d %d %d",
                            trst_n_in, select_in, si_in, arm_so_in,
                            capture_dr_in, shift_dr_in, update_dr_in);
            if (code != 7) begin
                done = 1;
            end else begin
                trst_n = trst_n_in[0];
                select = select_in[0];
                si = si_in[0];
                arm_so = arm_so_in[ARMS-1:0];
                capture_dr = capture_dr_in[0];
                shift_dr = shift_dr_in[0];
                update_dr = update_dr_in[0];
                #1; // let combinational logic (including async reset) settle before sampling
                $display("TRACE,%0d,%0d,%0d", so, arm_active, arm_select);
                tck = 1;
                #1;
                tck = 0;
                #1;
            end
        end

        $fclose(fd);
        $finish;
    end
endmodule
