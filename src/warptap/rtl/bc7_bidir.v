// Boundary-scan cell, bidir function (implementation_plan.md §7 Stage 3): as bc1_full, plus a
// dedicated `pin_in` feedback port so Capture-DR always samples the TRUE pin state regardless
// of which direction is currently active -- the "always observe" property that defines BC_7,
// confirmed against real BC_7 RTL (freecores/jtag's BiDirectionalCell.v) during this stage's
// research: `ToCore = BiDirPin` unconditionally, not gated by the current drive direction.
//
// capture_dr/shift_dr/update_dr are tap_core's own per-state strobes -- this module never
// re-derives them. `extest_mode` is computed once at the top level and fanned out here exactly
// as it is to bc1_full instances.
module bc7_bidir (
    input  wire pin_in,       // fed back from the actual bidirectional pin (post-tribuf) --
                               // always the live electrical pin state, drive direction aside
    input  wire func_in,      // the design's own intended value to drive when enabled
    input  wire si,
    output wire so,
    output wire pin_out,      // extest_mode ? po : func_in; drive-enable is the paired
                               // control cell's job, identical to an output3 pin
    input  wire capture_dr,
    input  wire shift_dr,
    input  wire update_dr,
    input  wire extest_mode,
    input  wire tck,
    input  wire trst_n
);
    reg shift_ff;
    reg po;

    // Both reset to 0, matching bc1_full.v's own reset discipline and for the same
    // reason: safe_value=0 must be an actual hardware guarantee (driver disabled
    // until explicitly programmed), not just a procedural PDL-preload convention.
    always @(posedge tck or negedge trst_n) begin
        if (!trst_n) begin
            shift_ff <= 1'b0;
            po <= 1'b0;
        end else begin
            if (capture_dr)
                shift_ff <= pin_in;
            else if (shift_dr)
                shift_ff <= si;
            if (update_dr)
                po <= shift_ff;
        end
    end

    assign so = shift_ff;
    assign pin_out = extest_mode ? po : func_in;
endmodule
