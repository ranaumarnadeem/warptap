// Testbench for a SIB-network-inserted `trivial` module (implementation_plan.md §7 Stage 4,
// testing layers 4/5). Mirrors tb_bsr_trivial.v's shape and stimulus-file protocol exactly --
// clk and tck driven from the SAME physical clock (lockstep), same single-clock-domain
// discipline Stage 3 established. sib_insert.py never touches `a`/`b`/`y` at all, so this
// testbench doubles as both the non-interference proof's driver (layer 4) and the network's
// own cross-sim driver (layer 5).
//
// Stimulus file: one line of six integers each: "<tms> <tdi> <trst_n> <rst_n> <a> <b>".
`timescale 1ns/1ps

module tb_sib_trivial;
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
