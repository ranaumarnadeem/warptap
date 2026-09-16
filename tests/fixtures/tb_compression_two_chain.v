// Testbench for a SIB-network-inserted, faultflow-compression-composed `core_top_compressed`
// module (Stage 24 cross-sim tier). Mirrors tb_functional_clock.v's/
// tb_mem_subsystem_mbist_faultflow.v's exact two-clock-domain (tck vs clk), pulse-flag-column
// stimulus protocol -- the one difference is a `scan_en` column instead of an `rst_n` one,
// since this DUT has no reset at all (plain dfxtp scan-chain FFs) but genuinely needs an
// externally-driven scan_en (not wrapped as a warptap instrument -- only the K-bit tdi channel
// and the two raw scan_out_N chains are).
//
// Stimulus file: one line of five integers each: "<tms> <tdi> <trst_n> <scan_en> <pulse>".
//
// The DUT's own `ff_tdi_channel` port (the renamed compression channel bus, tdi->
// ff_tdi_channel to avoid colliding with warptap's own TAP tdi pin) is deliberately left
// UNCONNECTED here -- it is a WRITE-instrument-driven port (warptap's instrument_write cell
// drives the real internal net the core's compression RTL reads; the port's own bits are a
// disconnected vestige, see sib_insert.py's detach_port), never something a testbench should
// drive externally -- same convention tb_functional_clock.v already established for stim_in.
`timescale 1ns/1ps

module tb_compression_two_chain;
    reg clk = 0;
    reg tck = 0;
    reg tms = 0;
    reg tdi = 0;
    reg trst_n = 1;
    reg scan_en = 0;
    wire tdo;
    wire scan_out_0;
    wire scan_out_1;

    core_top_compressed dut (
        .clk(clk),
        .scan_en(scan_en),
        .scan_out_0(scan_out_0),
        .scan_out_1(scan_out_1),
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
                    // PulsePin cycle: tck/tms/tdi held static, clk pulses instead -- the
                    // compression ring generator's own reseed/natural-step edge, or the
                    // functional capture edge, independent of the inserted TAP's tck.
                    #1;
                    $display("TRACE,%0d,%0d,%0d,%0d,%0d,%0d,%0d,%0d",
                              dut.warptap_tap_state, dut.warptap_current_instruction,
                              dut.warptap_capture_dr, dut.warptap_shift_dr, dut.warptap_update_dr,
                              tdo, scan_out_0, scan_out_1);
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
                              tdo, scan_out_0, scan_out_1);
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
