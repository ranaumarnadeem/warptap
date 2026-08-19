// Reusable stimulus-file-driven testbench for rtl/tap_core.v (implementation_plan.md §7
// Stage 2). Reads tests/fixtures/tb_tap_core.v's companion "stimulus.txt" (written per-test
// by tests/test_tap_fsm_cross_sim.py), one line of three integers each: "<is_reset> <tms>
// <tdi>". A reset line pulses trst_n and emits no trace; a normal line applies tms/tdi,
// samples the DUT's debug outputs (pre-edge, matching TapModel.tick()'s old-state-gated
// sampling), $displays a sentinel-prefixed CSV trace line, then applies one TCK edge.
`timescale 1ns/1ps

module tb_tap_core;
    localparam IR_WIDTH = 4;

    reg tck = 0;
    reg tms = 0;
    reg tdi = 0;
    reg trst_n = 1;
    wire tdo;
    wire [3:0] tap_state;
    wire [IR_WIDTH-1:0] current_instruction;
    wire capture_dr, shift_dr, update_dr;

    tap_core #(.IR_WIDTH(IR_WIDTH)) dut (
        .tck(tck),
        .tms(tms),
        .tdi(tdi),
        .trst_n(trst_n),
        .tdo(tdo),
        .tap_state(tap_state),
        .current_instruction(current_instruction),
        .capture_dr(capture_dr),
        .shift_dr(shift_dr),
        .update_dr(update_dr),
        .external_dr_tdo(1'b0)
    );

    integer fd;
    integer code;
    integer is_reset, tms_in, tdi_in;
    reg done;

    initial begin
        done = 0;
        fd = $fopen("stimulus.txt", "r");
        if (fd == 0) begin
            $display("ERROR: could not open stimulus.txt");
            $finish;
        end

        while (!done) begin
            code = $fscanf(fd, "%d %d %d", is_reset, tms_in, tdi_in);
            if (code != 3) begin
                done = 1;
            end else if (is_reset) begin
                trst_n = 0;
                #1;
                trst_n = 1;
                #1;
            end else begin
                tms = tms_in[0];
                tdi = tdi_in[0];
                #1; // let combinational logic settle before sampling
                $display("TRACE,%0d,%0d,%0d,%0d,%0d,%0d",
                          tap_state, current_instruction, capture_dr, shift_dr,
                          update_dr, tdo);
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
