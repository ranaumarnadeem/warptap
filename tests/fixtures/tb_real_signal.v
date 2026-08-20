// Testbench for a SIB-network-inserted `real_signal` module (implementation_plan.md §7
// Stage 9 §5.1). Mirrors tb_sib_trivial.v's shape and stimulus-file protocol exactly -- clk
// and tck driven from the SAME physical clock (lockstep).
//
// Stimulus file: one line of four integers each: "<tms> <tdi> <trst_n> <rst_n>".
`timescale 1ns/1ps

module tb_real_signal;
    reg clk = 0;
    reg tms = 0;
    reg tdi = 0;
    reg trst_n = 1;
    reg rst_n = 1;
    wire tdo;
    wire status_out;

    real_signal dut (
        .clk(clk),
        .rst_n(rst_n),
        .status_out(status_out),
        .tck(clk),
        .tms(tms),
        .tdi(tdi),
        .trst_n(trst_n),
        .tdo(tdo)
    );

    integer fd;
    integer code;
    integer tms_in, tdi_in, trst_n_in, rst_n_in;
    reg done;

    initial begin
        done = 0;
        fd = $fopen("stimulus.txt", "r");
        if (fd == 0) begin
            $display("ERROR: could not open stimulus.txt");
            $finish;
        end

        while (!done) begin
            code = $fscanf(fd, "%d %d %d %d", tms_in, tdi_in, trst_n_in, rst_n_in);
            if (code != 4) begin
                done = 1;
            end else begin
                tms = tms_in[0];
                tdi = tdi_in[0];
                trst_n = trst_n_in[0];
                rst_n = rst_n_in[0];
                #1; // let combinational logic settle before sampling
                $display("TRACE,%0d,%0d,%0d,%0d,%0d,%0d,%0d",
                          dut.warptap_tap_state, dut.warptap_current_instruction,
                          dut.warptap_capture_dr, dut.warptap_shift_dr, dut.warptap_update_dr,
                          tdo, status_out);
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
