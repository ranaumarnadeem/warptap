// Testbench for a BSR-inserted `trivial_bidir` module (implementation_plan.md §7 Stage 3,
// bidir increment). Mirrors tb_bsr_trivial.v's shape and stimulus-file protocol exactly, just
// against trivial_bidir's port list (clk/rst_n/oe/val/io/sensed instead of clk/rst_n/a/b/y).
// `io` stays a genuine `wire` (inout at the DUT boundary) driven only by the DUT itself in
// this testbench -- no external contention is modeled, matching this stage's v1 scope.
//
// Stimulus file: one line of six integers each: "<tms> <tdi> <trst_n> <rst_n> <oe> <val>".
`timescale 1ns/1ps

module tb_bsr_trivial_bidir;
    reg clk = 0;
    reg tms = 0;
    reg tdi = 0;
    reg trst_n = 1;
    reg rst_n = 1;
    reg oe = 0;
    reg val = 0;
    wire tdo;
    wire io;
    wire sensed;

    trivial_bidir dut (
        .clk(clk),
        .rst_n(rst_n),
        .oe(oe),
        .val(val),
        .io(io),
        .sensed(sensed),
        .tck(clk),
        .tms(tms),
        .tdi(tdi),
        .trst_n(trst_n),
        .tdo(tdo)
    );

    integer fd;
    integer code;
    integer tms_in, tdi_in, trst_n_in, rst_n_in, oe_in, val_in;
    reg done;

    initial begin
        done = 0;
        fd = $fopen("stimulus.txt", "r");
        if (fd == 0) begin
            $display("ERROR: could not open stimulus.txt");
            $finish;
        end

        while (!done) begin
            code = $fscanf(fd, "%d %d %d %d %d %d", tms_in, tdi_in, trst_n_in, rst_n_in, oe_in, val_in);
            if (code != 6) begin
                done = 1;
            end else begin
                tms = tms_in[0];
                tdi = tdi_in[0];
                trst_n = trst_n_in[0];
                rst_n = rst_n_in[0];
                oe = oe_in[0];
                val = val_in[0];
                #1;
                $display("TRACE,%0d,%0d,%0d,%0d,%0d,%0d,%0d,%0d",
                          dut.warptap_tap_state, dut.warptap_current_instruction,
                          dut.warptap_capture_dr, dut.warptap_shift_dr, dut.warptap_update_dr,
                          tdo, io, sensed);
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
