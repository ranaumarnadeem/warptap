// Stimulus-file-driven testbench for rtl/tap_core.v used by tests/test_bsdl_emit_cross_sim.py to
// check every claim warptap.bsdl_emit.to_bsdl makes. Same conventions as tb_tap_core.v, extended
// for what BSDL claims need: a driven external_dr_tdo (the IJTAG network's tail) and TDO sampled
// at three points per cycle so "TDO changes on the rising edge of TCK" is checkable.
//
// Reads "stimulus.txt", one line of four integers: "<is_reset> <tms> <tdi> <external_dr_tdo>".
//   reset line : pulses trst_n low; prints "RESET,<state>,<instruction>" sampled while it is
//                still low (an asynchronous reset shows up immediately, no clock needed).
//   normal line: applies tms/tdi/external_dr_tdo, lets combinational logic settle, samples
//                tap_state/current_instruction/tdo (pre-edge -- the value a tester would read for
//                this shift cycle), then pulses TCK and prints
//                "TRACE,<state>,<instruction>,<tdo_pre>,<tdo_after_rise>,<tdo_after_fall>".
`timescale 1ns/1ps

module tb_tap_core_bsdl;
    localparam IR_WIDTH = 4;

    reg tck = 0;
    reg tms = 0;
    reg tdi = 0;
    reg trst_n = 1;
    reg external_dr_tdo = 0;
    wire tdo;
    wire [3:0] tap_state;
    wire [IR_WIDTH-1:0] current_instruction;
    wire capture_dr, shift_dr, update_dr;

    // No parameter override: the same testbench runs a tap_core specialized by Yosys (an
    // IJTAG_ACCESS insertion's), whose Verilog has no parameters left. IR_WIDTH must stay
    // tap_core's default.
    tap_core dut (
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
        .external_dr_tdo(external_dr_tdo)
    );

    integer fd;
    integer code;
    integer is_reset, tms_in, tdi_in, ext_in;
    reg done;
    reg [3:0] state_pre;
    reg [IR_WIDTH-1:0] instr_pre;
    reg tdo_pre, tdo_rise, tdo_fall;

    initial begin
        done = 0;
        fd = $fopen("stimulus.txt", "r");
        if (fd == 0) begin
            $display("ERROR: could not open stimulus.txt");
            $finish;
        end

        while (!done) begin
            code = $fscanf(fd, "%d %d %d %d", is_reset, tms_in, tdi_in, ext_in);
            if (code != 4) begin
                done = 1;
            end else if (is_reset) begin
                trst_n = 0;
                #1;
                $display("RESET,%0d,%0d", tap_state, current_instruction);
                trst_n = 1;
                #1;
            end else begin
                tms = tms_in[0];
                tdi = tdi_in[0];
                external_dr_tdo = ext_in[0];
                #1; // let combinational logic settle before sampling
                state_pre = tap_state;
                instr_pre = current_instruction;
                tdo_pre = tdo;
                tck = 1;
                #1;
                tdo_rise = tdo;
                tck = 0;
                #1;
                tdo_fall = tdo;
                $display("TRACE,%0d,%0d,%0d,%0d,%0d",
                          state_pre, instr_pre, tdo_pre, tdo_rise, tdo_fall);
            end
        end

        $fclose(fd);
        $finish;
    end
endmodule
