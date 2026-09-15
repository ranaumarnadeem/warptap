// Testbench for a SIB-network-inserted alexforencich/verilog-uart `uart_tx` module (second
// real-external-design validation). Mirrors tb_functional_clock.v's exact stimulus-file
// protocol and PulsePin convention -- uart_tx's own `clk` is a genuinely separate functional
// clock from the TAP's `tck`, pulsed the same way `sysclk` is there -- but renamed to this
// design's own real port names, and with its own reset polarity: uart_tx's `rst` is
// active-HIGH and, unlike every prior fixture's async DUT reset, only takes effect
// SYNCHRONOUSLY on a `clk` edge (`always @(posedge clk) if (rst) ...`, no separate
// `posedge rst` sensitivity) -- so a real reset here needs at least one pulse row with rst=1,
// not just holding the level.
//
// Also traces `txd` on every row (not just JTAG ones): uart_tx.v only changes txd on its own
// clk edges (pulse rows), so filtering the Python-side decode to pulse rows alone recovers
// the exact bit-serial waveform, independent of and never observed through JTAG shifting --
// the strong end-to-end proof the validation scope calls for.
//
// Stimulus file: one line of five integers each: "<tms> <tdi> <rst> <pulse>" plus trst_n
// (JTAG's own separate, async reset) -- five columns total: "<tms> <tdi> <trst_n> <rst> <pulse>".
`timescale 1ns/1ps

module tb_uart_tx;
    reg tck = 0;
    reg tms = 0;
    reg tdi = 0;
    reg trst_n = 1;
    reg rst = 1;
    reg clk = 0;
    wire tdo;
    wire txd;
    wire busy;

    uart_tx dut (
        .rst(rst),
        .clk(clk),
        .txd(txd),
        .busy(busy),
        .tck(tck),
        .tms(tms),
        .tdi(tdi),
        .trst_n(trst_n),
        .tdo(tdo)
    );

    integer fd;
    integer code;
    integer tms_in, tdi_in, trst_n_in, rst_in, pulse_in;
    reg done;

    initial begin
        done = 0;
        fd = $fopen("stimulus.txt", "r");
        if (fd == 0) begin
            $display("ERROR: could not open stimulus.txt");
            $finish;
        end

        while (!done) begin
            code = $fscanf(fd, "%d %d %d %d %d", tms_in, tdi_in, trst_n_in, rst_in, pulse_in);
            if (code != 5) begin
                done = 1;
            end else begin
                trst_n = trst_n_in[0];
                rst = rst_in[0];
                if (pulse_in[0]) begin
                    // PulsePin cycle: tck/tms/tdi held static, clk pulses instead -- uart_tx's
                    // own functional edge, independent of tck.
                    #1;
                    $display("TRACE,%0d,%0d,%0d,%0d,%0d,%0d,%0d,%0d",
                              dut.warptap_tap_state, dut.warptap_current_instruction,
                              dut.warptap_capture_dr, dut.warptap_shift_dr, dut.warptap_update_dr,
                              tdo, txd, busy);
                    clk = 1;
                    #1;
                    clk = 0;
                    #1;
                end else begin
                    tms = tms_in[0];
                    tdi = tdi_in[0];
                    #1; // let combinational logic settle before sampling
                    $display("TRACE,%0d,%0d,%0d,%0d,%0d,%0d,%0d,%0d",
                              dut.warptap_tap_state, dut.warptap_current_instruction,
                              dut.warptap_capture_dr, dut.warptap_shift_dr, dut.warptap_update_dr,
                              tdo, txd, busy);
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
