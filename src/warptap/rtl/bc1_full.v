// Boundary-scan cell, output3/control function (implementation_plan.md §7 Stage 3): the full
// BC_1 shape -- capture-mux + shift FF + a second-rank update latch + an output-side mode mux.
// Shared body for two BSR roles that differ only in what `pin_out` connects to externally:
// an OUTPUT3 cell drives a real pin (through a paired tri-state buffer); a CONTROL cell drives
// a neighboring cell's tri-state enable instead. Neither role needs its own module.
//
// capture_dr/shift_dr/update_dr are tap_core's own per-state strobes -- this module never
// re-derives them. `extest_mode` is computed once at the top level (a single $eq comparator
// against tap_core's current_instruction) and fanned out to every bc1_full/bc7_bidir instance,
// rather than each cell decoding the instruction itself.
module bc1_full (
    input  wire func_in,      // live core-driven value: both the capture source and the
                               // normal-mode passthrough value
    input  wire si,
    output wire so,
    output wire pin_out,      // extest_mode ? po : func_in
    input  wire capture_dr,
    input  wire shift_dr,
    input  wire update_dr,
    input  wire extest_mode,  // 1 while EXTEST is the active instruction (drive test data).
                               // SAMPLE_PRELOAD updates `po` (its Update-DR path is real and
                               // testable) but never asserts extest_mode, so it never drives
                               // a pin -- this cell doesn't need to know which instruction is
                               // active beyond this one signal.
    input  wire tck,
    input  wire trst_n
);
    reg shift_ff;
    reg po;

    // Both reset to 0 (matching tap_core.v's own reset discipline). po's reset value
    // is exactly what makes safe_value=0 (plan §3.1's chosen default) an actual
    // hardware guarantee, not just a procedural PDL-preload convention: on reset,
    // po=0 -> pin_out=0 whenever extest_mode later asserts -> the paired tri-state
    // buffer's EN=0 -> disabled/Z, before any test software has preloaded anything.
    always @(posedge tck or negedge trst_n) begin
        if (!trst_n) begin
            shift_ff <= 1'b0;
            po <= 1'b0;
        end else begin
            if (capture_dr)
                shift_ff <= func_in;
            else if (shift_dr)
                shift_ff <= si;
            if (update_dr)
                po <= shift_ff;
        end
    end

    assign so = shift_ff;
    assign pin_out = extest_mode ? po : func_in;
endmodule
