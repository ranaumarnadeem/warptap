// Reusable stimulus-file-driven testbench for a BSR-inserted `trivial` module
// (implementation_plan.md §7 Stage 3). Shared by the equivalence test (only needs `y`) and
// the cross-sim test (needs full TAP-internal state, reached via Verilog hierarchical
// references into warptap_tap_core's named internal wires -- legal for simulation/debug
// even though those wires aren't promoted to `trivial`'s own port list).
//
// clk and tck are deliberately driven from the SAME physical clock (lockstep) -- CDC between
// the functional clock and TCK is out of scope for Stage 3 (not mentioned anywhere in
// implementation_plan.md for this stage); this keeps the testbench single-clock-domain and
// straightforward to cross-simulate deterministically.
//
// Stimulus file: one line of six integers each: "<tms> <tdi> <trst_n> <rst_n> <a> <b>".
// Each line is one lockstep clk=tck cycle. Samples (pre-edge, matching tb_tap_core.v's
// established convention) then $displays a sentinel-prefixed trace line before applying the
// clock edge.
`timescale 1ns/1ps

module tb_bsr_trivial;
    reg clk = 0;
    reg tms = 0;
    reg tdi = 0;
    reg trst_n = 1;
    reg rst_n = 1;
    reg a = 0;
    reg b = 0;
    wire tdo;
    wire y;

    trivial dut (
        .clk(clk),
        .rst_n(rst_n),
        .a(a),
        .b(b),
        .y(y),
        .tck(clk),
        .tms(tms),
        .tdi(tdi),
        .trst_n(trst_n),
        .tdo(tdo)
    );

    integer fd;
    integer code;
    integer tms_in, tdi_in, trst_n_in, rst_n_in, a_in, b_in;
    reg done;

    initial begin
        done = 0;
        fd = $fopen("stimulus.txt", "r");
        if (fd == 0) begin
            $display("ERROR: could not open stimulus.txt");
            $finish;
        end

        while (!done) begin
            code = $fscanf(fd, "%d %d %d %d %d %d", tms_in, tdi_in, trst_n_in, rst_n_in, a_in, b_in);
            if (code != 6) begin
                done = 1;
            end else begin
                tms = tms_in[0];
                tdi = tdi_in[0];
                trst_n = trst_n_in[0];
                rst_n = rst_n_in[0];
                a = a_in[0];
                b = b_in[0];
                #1; // let combinational logic settle before sampling
                $display("TRACE,%0d,%0d,%0d,%0d,%0d,%0d,%0d",
                          dut.warptap_tap_state, dut.warptap_current_instruction,
                          dut.warptap_capture_dr, dut.warptap_shift_dr, dut.warptap_update_dr,
                          tdo, y);
                clk = 1;
                #1;
                clk = 0;
                #1;
            end
        end

        $fclose(fd);
        $finish;
    end
endmodule
