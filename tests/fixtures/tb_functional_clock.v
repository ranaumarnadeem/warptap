// Testbench for a SIB-network-inserted `functional_clock` module (implementation_plan.md §7
// Stage 14). Extends tb_real_signal.v's exact stimulus-file protocol with one more column:
// a pulse flag distinguishing an ordinary JTAG cycle (tck toggles, sysclk held) from a
// PulsePin cycle (sysclk toggles once, tck/tms/tdi held) -- the two independent clock
// domains warptap.tap_ir.PulsePin exists to keep separate.
//
// Stimulus file: one line of five integers each: "<tms> <tdi> <trst_n> <rst_n> <pulse>".
`timescale 1ns/1ps

module tb_functional_clock;
    reg clk = 0;
    reg tms = 0;
    reg tdi = 0;
    reg trst_n = 1;
    reg rst_n = 1;
    reg sysclk = 0;
    wire tdo;
    wire captured_out;

    functional_clock dut (
        .rst_n(rst_n),
        .sysclk(sysclk),
        .captured_out(captured_out),
        .tck(clk),
        .tms(tms),
        .tdi(tdi),
        .trst_n(trst_n),
        .tdo(tdo)
    );

    integer fd;
    integer code;
    integer tms_in, tdi_in, trst_n_in, rst_n_in, pulse_in;
    reg done;

    initial begin
        done = 0;
        fd = $fopen("stimulus.txt", "r");
        if (fd == 0) begin
            $display("ERROR: could not open stimulus.txt");
            $finish;
        end

        while (!done) begin
            code = $fscanf(fd, "%d %d %d %d %d", tms_in, tdi_in, trst_n_in, rst_n_in, pulse_in);
            if (code != 5) begin
                done = 1;
            end else begin
                trst_n = trst_n_in[0];
                rst_n = rst_n_in[0];
                if (pulse_in[0]) begin
                    // PulsePin cycle: tck/tms/tdi held static, sysclk pulses instead --
                    // the DUT's own functional capture edge, independent of tck.
                    #1;
                    $display("TRACE,%0d,%0d,%0d,%0d,%0d,%0d,%0d",
                              dut.warptap_tap_state, dut.warptap_current_instruction,
                              dut.warptap_capture_dr, dut.warptap_shift_dr, dut.warptap_update_dr,
                              tdo, captured_out);
                    sysclk = 1;
                    #1;
                    sysclk = 0;
                    #1;
                end else begin
                    tms = tms_in[0];
                    tdi = tdi_in[0];
                    #1; // let combinational logic settle before sampling
                    $display("TRACE,%0d,%0d,%0d,%0d,%0d,%0d,%0d",
                              dut.warptap_tap_state, dut.warptap_current_instruction,
                              dut.warptap_capture_dr, dut.warptap_shift_dr, dut.warptap_update_dr,
                              tdo, captured_out);
                    clk = 1;
                    #1;
                    clk = 0;
                    #1;
                end
            end
        end

        $fclose(fd);
        $finish;
    end
endmodule
